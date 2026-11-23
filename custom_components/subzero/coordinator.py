"""Per-appliance state coordinator, status reads, recovery, and commands.

What the coordinator guarantees, and what it does not:

- State is the latest observation. A status read replaces it, and live updates are
  laid over it as they arrive. Nothing orders a read against an update that arrives
  while the read is in flight, so the update stands. A reply that spans the stream
  ending, or the appliance's channel opening, is not published.
- Live updates are either established for an appliance or they are not. They stop
  being established when the stream is lost, the appliance's update channel is
  refused, or a status read completes before the channel has opened. They are
  established again only once the channel is open and a status read that started
  after it opened succeeds. A full update keeps the appliance available meanwhile,
  but does not establish them.
- While live updates are not established, a control reads status first. While they
  are, a control acts on cached state.
- The checks before a write explain a refusal early. They are not a lock: the
  appliance can change between the check and the write.
"""

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from datetime import datetime, tzinfo
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    ApiError,
    RateLimited,
    StateUpdate,
    SubZeroClient,
    notification_records,
    validate_ice_delay,
)
from .auth import InvalidAuth
from .const import (
    BINARY_KEYS,
    CONTROL_CONFIRM_TIMEOUT,
    CONTROL_PUSH_TIMEOUT,
    DOMAIN,
    ICE_DELAY_KEYS,
    KITCHEN_TIMERS,
    MAX_RECONNECT_DELAY,
    PRIVATE_KEYS,
    RECONNECT_DELAY,
    STATE_KEYS,
)
from .controls import (
    appliance_datetime,
    appliance_type,
    clock_zone,
    control_matches,
    held_for_start,
    ice_mode,
    ice_mode_properties,
    is_dishwasher,
    is_hood,
    is_ice_maker,
    is_oven,
    operating_mode_keys,
    start_properties,
    start_temperature,
    supports_air_filter_reset,
    validate_control_properties,
    wash_cancel_enabled,
)
from .state import (
    EventTracker,
    PendingRead,
    StateRead,
    discard_excluded,
    snapshot_is_older,
)

_LOGGER = logging.getLogger(__name__)
TRANSPORT_GRACE_SECONDS = 120
FOLLOWUP_DELAYS = (2, 60, 120)
FOLLOWUP_KEYS = {key for key in BINARY_KEYS if key.endswith(("door_ajar", "timer_active"))} | {
    "kitchen_timer2_active",
    "wash_cycle_on",
}


def _state_for_log(properties: dict) -> dict:
    return {
        key: value
        for key, value in properties.items()
        if key in STATE_KEYS and key not in PRIVATE_KEYS and not isinstance(value, dict | list)
    }


class PushLink:
    """Track whether live updates are established for one appliance.

    They are not after the shared stream is lost or the appliance's update channel
    is refused, until a status read on the current connection confirms state.
    """

    def __init__(
        self,
        *,
        schedule_grace: Callable[[Coroutine[Any, Any, None]], asyncio.Task],
        on_expire: Callable[[Exception], None],
        grace_seconds: float = TRANSPORT_GRACE_SECONDS,
    ) -> None:
        self._schedule_grace = schedule_grace
        self._on_expire = on_expire
        self._grace_seconds = grace_seconds
        self.error: Exception | None = None
        self.epoch = 0
        # The appliance's update channel is open on the current connection.
        self.channel_open = False
        # The channel was refused on a working stream.
        self.channel_refused = False
        self._grace_task: asyncio.Task | None = None

    @property
    def healthy(self) -> bool:
        return self.error is None

    @property
    def in_grace(self) -> bool:
        return self._grace_task is not None

    @property
    def expired(self) -> bool:
        return self.error is not None and self._grace_task is None

    def fail(self, error: Exception, *, lost: bool = True, can_grace: bool) -> bool:
        """Record that the stream ended and return whether the epoch advanced."""
        first = self.error is None
        self.error = error
        # Channels belong to the connection that ended.
        self.channel_open = False
        self.channel_refused = False
        advanced = first or lost
        if advanced:
            # A failed attempt that delivered nothing cannot have changed state.
            self.epoch += 1
        if first and can_grace:
            self._start_grace()
        return advanced

    def channel_failed(self, error: ApiError) -> None:
        """Record that the appliance's update channel was refused."""
        self.error = error
        self.channel_open = False
        self.channel_refused = True
        self.cancel_grace()

    def channel_opened(self) -> bool:
        """Record that the appliance's channel is open, and return whether it was not."""
        self.channel_refused = False
        if self.channel_open:
            return False
        self.channel_open = True
        # A read from before the channel opened cannot confirm what it delivers.
        self.epoch += 1
        return True

    def confirm(self, *, push_connected: bool) -> None:
        """Confirm state with a status read on the current connection."""
        if push_connected and self.channel_open:
            self.error = None
            self.cancel_grace()
            return
        if self.error is None:
            # A read before the channel has opened, as setup can make.
            self.error = ApiError("Sub-Zero's live updates have not started.")
        # A current read is useful without live updates, but its values still age.
        self._start_grace()

    def unconfirm(self, error: Exception) -> None:
        """Require a status read to confirm state again on the same connection."""
        if self.error is None:
            self.error = error

    def extend_grace(self) -> None:
        """Keep fresh state available while a status read has yet to confirm it."""
        if self.error is not None:
            self._start_grace()

    def cancel_grace(self) -> None:
        if self._grace_task is not None:
            self._grace_task.cancel()
            self._grace_task = None

    def _start_grace(self) -> None:
        self.cancel_grace()
        self._grace_task = self._schedule_grace(self._grace())

    async def _grace(self) -> None:
        await asyncio.sleep(self._grace_seconds)
        self._grace_task = None
        if self.error is not None:
            self._on_expire(self.error)


class SubZeroCoordinator(DataUpdateCoordinator[dict]):
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: SubZeroClient,
        device_id: str,
        device: dict,
    ):
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {device['name']}",
        )
        self.client = client
        self.data = {}
        self.entry = entry
        self.device_id = device_id
        self.device = dict(device)
        self.unrecognized_keys: set[str] = set()
        # The appliance clock's time zone, from the offset of its last pushed `time`.
        self.clock_zone: tzinfo | None = None
        self.push_stats: dict[str, int | str | None] = {
            "snapshots": 0,
            "updates": 0,
            # Snapshots that changed an on/off value no update had reported, and the
            # latest one's age on arrival by the appliance clock.
            "snapshot_flips": 0,
            "last_snapshot_flip_age_seconds": None,
            "last_received": None,
        }
        # The cooking mode and temperature chosen for a remote start, by property.
        self.start_settings: dict[str, int] = {}
        self._command_lock = asyncio.Lock()
        self._command_generation = 0
        self._unsettled = False
        self._read_requests = 0
        self._pending_read_error_requests: set[int] = set()
        self._read_task: asyncio.Task[StateRead] | None = None
        self._recovery_task: asyncio.Task | None = None
        self._canceling_recoveries: set[asyncio.Task] = set()
        self._active_read: PendingRead | None = None
        # Sign-in failed and no update has succeeded since. Kept apart from
        # last_exception, which a later transport error replaces.
        self._sign_in_failed = False
        self._push = PushLink(
            schedule_grace=lambda coro: self.entry.async_create_background_task(
                self.hass, coro, "Sub-Zero reconnect grace"
            ),
            on_expire=self.async_set_update_error,
        )
        self._events = EventTracker(dt_util.utcnow())
        self._event_listeners: list[Callable[[dict], None]] = []
        self._followup_task: asyncio.Task | None = None
        self._followup_alert = False
        self._last_followup = 0.0
        self.confirmed_at: datetime | None = None
        self.verification_stats: dict[str, int | str | None] = {
            "checks": 0,
            "status_reads": 0,
            "last_success": None,
        }

    @callback
    def _certify_state(self, properties: dict) -> bool:
        model = properties.get("appliance_model")
        if not isinstance(model, str) or not model:
            return False
        self.confirmed_at = dt_util.utcnow()
        return True

    @property
    def transport_healthy(self) -> bool:
        return self._push.healthy

    @property
    def receiving_live_updates(self) -> bool:
        """Whether the stream is connected and the appliance's channel is open on it."""
        return self.client.push_connected and self._push.channel_open

    @property
    def waiting_for_push(self) -> bool:
        return not self._push.healthy and not self._push.channel_refused

    @property
    def can_verify_state(self) -> bool:
        if self._sign_in_failed:
            return False
        return self.last_update_success or (
            not self._push.channel_refused
            and not isinstance(self.last_exception, ConfigEntryAuthFailed | InvalidAuth)
        )

    @callback
    def start_recovery(self) -> None:
        if self._recovery_task is None or self._recovery_task.done():
            self._recovery_task = self.entry.async_create_background_task(
                self.hass, self.async_recover(), "Sub-Zero state recovery"
            )

    @callback
    def start_recovery_if_needed(self) -> None:
        """Start recovery when a status read on the open channel could establish live updates."""
        if (
            (not self.last_update_success or not self._push.healthy)
            and self.receiving_live_updates
            and not self._sign_in_failed
        ):
            self.start_recovery()

    @callback
    def cancel_recovery(self) -> None:
        task = self._recovery_task
        if task is None:
            return
        self._recovery_task = None
        if not task.done():
            self._canceling_recoveries.add(task)
            task.add_done_callback(self._canceling_recoveries.discard)
            task.cancel()

    async def async_stop_recovery(self) -> None:
        self.cancel_recovery()
        if pending := list(self._canceling_recoveries):
            await asyncio.gather(*pending, return_exceptions=True)

    @callback
    def on_channel_opened(self, *, recover: bool = True) -> None:
        if self._push.channel_opened():
            self.async_update_listeners()
        if recover:
            self.start_recovery_if_needed()

    @callback
    def on_channel_error(self, error: ApiError) -> None:
        self._push.channel_failed(error)
        self.cancel_recovery()
        self.async_set_update_error(error)

    @callback
    def on_account_error(self, error: Exception) -> None:
        if isinstance(error, InvalidAuth) and self._active_read is not None:
            self._active_read.fail(error)
        # The stream ended, so a status read has to confirm state when it returns.
        self._push.fail(error, can_grace=False)
        self.async_set_update_error(error)

    @callback
    def cancel_followup(self) -> None:
        self._followup_alert = False
        if self._followup_task is not None:
            self._followup_task.cancel()
            self._followup_task = None

    async def async_verify_state(self) -> None:
        """Verify state without reopening a working update channel."""
        self.verification_stats["checks"] += 1
        self.verification_stats["status_reads"] += 1
        result = await self._async_read_result(report_error=False)
        if result.error is not None:
            return
        if self.last_update_success and result.confirmed_at is not None:
            self.verification_stats["last_success"] = result.confirmed_at.isoformat()

    @callback
    def async_transport_error(self, error: ApiError, *, lost: bool = True) -> None:
        """Keep established state briefly while the shared stream reconnects."""
        healthy = self.last_update_success and bool(self.data.get("appliance_model"))
        if self._push.fail(error, lost=lost, can_grace=healthy):
            # Remote Ready may end and return unseen while updates are missed.
            self.start_settings = {}
        if not self.data:
            # Before the first state nothing can go stale, and setup reads the state.
            return
        if healthy and self._push.in_grace:
            self.async_update_listeners()
        else:
            self.async_set_update_error(error)

    @callback
    def async_set_update_error(self, err: Exception) -> None:
        # An unavailable appliance misses updates, so a status read has to confirm
        # state before live updates count as established again.
        self._push.unconfirm(err)
        self._push.cancel_grace()
        self.start_settings = {}
        if isinstance(err, ConfigEntryAuthFailed | InvalidAuth):
            self._sign_in_failed = True
        super().async_set_update_error(err)

    def _active_conditions(self) -> set[str]:
        return {key for key in FOLLOWUP_KEYS if self.data.get(key) is True}

    @property
    def start_state(self) -> dict:
        """Reported state with the cooking mode and temperature kept for a remote start."""
        return {**self.data, **self.start_settings}

    @callback
    def async_set_updated_data(self, data: dict) -> None:
        # Kept only while the oven is seen to stay idle in Remote Ready, which state
        # published after a gap cannot show.
        self.start_settings = {
            key: value
            for key, value in self.start_settings.items()
            if self.last_update_success and held_for_start(data, key)
        }
        if self._push.expired:
            # A status read or a full update extends the grace window before it
            # publishes. Nothing has here, so the appliance stays unavailable.
            self.data = data
            self.async_update_listeners()
        else:
            self._sign_in_failed = False
            super().async_set_updated_data(data)
        _LOGGER.debug("%s state published: %s", self.device["name"], _state_for_log(self.data))
        if (
            not self._active_conditions()
            and not self._followup_alert
            and self._followup_task is not None
        ):
            self._followup_task.cancel()

    @callback
    def async_schedule_followup(self, *, alert: bool = False) -> None:
        """Check a new alert without extending an existing window."""
        if self.entry.pref_disable_polling:
            return
        if (
            self._followup_task is not None
            and not self._followup_task.done()
            and not self._followup_task.cancelling()
        ):
            self._followup_alert |= alert
            return
        self._followup_alert = alert
        self._followup_task = self.entry.async_create_background_task(
            self.hass, self._async_followup(), "Sub-Zero event follow-up"
        )

    async def _async_followup(self) -> None:
        try:
            for delay in FOLLOWUP_DELAYS:
                await asyncio.sleep(max(delay, self._last_followup + 60 - time.monotonic()))
                if self.entry.pref_disable_polling:
                    return
                self._followup_alert = False
                self._last_followup = time.monotonic()
                result = await self._async_read_result(report_error=False)
                if result.error is not None:
                    if not self.last_update_success or (
                        isinstance(result.error, UpdateFailed)
                        and result.error.retry_after is not None
                    ):
                        self._followup_alert = False
                        return
                    self._followup_alert = self._followup_alert and delay == FOLLOWUP_DELAYS[-1]
                    continue
                if not self._active_conditions() and not self._followup_alert:
                    return
        finally:
            if self._followup_task is asyncio.current_task():
                self._followup_task = None
        if self._followup_alert:
            # An alert that arrived during the last read starts its own window.
            self.async_schedule_followup(alert=True)

    @callback
    def async_add_event_listener(self, listener: Callable[[dict], None]) -> Callable[[], None]:
        self._event_listeners.append(listener)
        return lambda: self._event_listeners.remove(listener)

    @callback
    def _process_events(self, properties: dict, *, history: bool) -> None:
        deliver, records = self._events.extract_records(
            properties, self.clock_zone, history=history
        )
        source = "history" if history else "channel"
        for timestamp, sequence, code in records:
            self.async_receive_event(timestamp, sequence, code, deliver=deliver, source=source)

    @callback
    def async_receive_event(
        self,
        timestamp: datetime,
        sequence: int,
        code: int,
        *,
        deliver: bool = True,
        source: str = "channel",
    ) -> bool:
        """Deliver an event once across state history, stream and Firebase copies."""
        event = self._events.record(timestamp, sequence, code, deliver=deliver, source=source)
        if event is None:
            return False
        for listener in self._event_listeners:
            listener(event)
        self.async_schedule_followup(alert=True)
        return True

    @property
    def device_info(self) -> DeviceInfo:
        info = DeviceInfo(identifiers={(DOMAIN, self.device_id)}, name=self.device["name"])
        if not self.data.get("appliance_model"):
            # Until the appliance reports, keep the details the registry saved from
            # its last status rather than clearing them.
            return info
        version = self.data.get("version")
        serial = self.data.get("appliance_serial")
        info.update(
            {
                "manufacturer": (
                    "Cove"
                    if is_dishwasher(self.data)
                    else "Wolf"
                    if is_oven(self.data) or is_hood(self.data)
                    else "Sub-Zero"
                ),
                "model": self.data["appliance_model"],
                "serial_number": serial if isinstance(serial, str) else None,
                "sw_version": version.get("fw") if isinstance(version, dict) else None,
            }
        )
        return info

    @asynccontextmanager
    async def _command(self, action: str = "change settings") -> AsyncIterator[None]:
        """Serialize commands and report a request that fails as an error.

        A cancelled command can still change the appliance after the last read, so
        the next command reads state before deciding what to write.
        """
        async with self._command_lock:
            if self._unsettled or not self._push.healthy or not self.client.push_connected:
                try:
                    await self.async_read_status()
                except HomeAssistantError as error:
                    raise ServiceValidationError(str(error)) from error
                self._unsettled = False
            if not self.last_update_success:
                raise ServiceValidationError("The appliance is unavailable.")
            self._command_generation = self._push.epoch
            try:
                yield
            except asyncio.CancelledError:
                self._unsettled = True
                raise
            except InvalidAuth as error:
                self.entry.async_start_reauth(self.hass)
                raise HomeAssistantError(f"Sign in to Sub-Zero again to {action}.") from error
            except ApiError as error:
                raise HomeAssistantError(str(error)) from error

    async def _async_confirm_command_generation(self) -> None:
        if self._command_generation != self._push.epoch:
            await self.async_read_status()
            self._command_generation = self._push.epoch

    async def async_set_properties(self, properties: dict, *, force: bool = False) -> None:
        """Serialize writes and confirm their result from appliance state.

        Forced writes go out even when the appliance already reports the value,
        which is how the app starts ovens and cancels wash cycles. A value the
        appliance already reports is re-sent as is, without the checks a change needs.
        """
        async with self._command():
            await self._async_write(dict(properties), force=force)

    async def async_start(self, key: str, temperature: int | None = None) -> None:
        """Start with the app's writes, from the state left by earlier commands."""
        async with self._command():
            if self.data.get(key) is True:
                prefix = key.removesuffix("_unit_on")
                properties = {} if temperature is None else {f"{prefix}_set_temp": temperature}
                await self._async_write({**properties, key: True})
            else:
                await self._async_write(
                    start_properties(self.start_state, key, temperature), force=True, start=True
                )

    async def async_cancel_wash(self) -> None:
        async with self._command():
            if not wash_cancel_enabled(self.data):
                raise ServiceValidationError("There is no wash cycle to cancel.")
            # Like the app, cancel even when the cycle already reports off, as it
            # may during a delayed start.
            await self._async_write({"wash_cycle_on": False}, force=True)

    async def async_dismiss_timer(self, key: str, *, running: bool = False) -> None:
        """Clear a finished timer, or stop one that was running when asked.

        A timer that was finished when asked is not stopped if it restarts first.
        """
        async with self._command():
            prefix = KITCHEN_TIMERS[key]
            if self.data.get(f"{prefix}_complete") is not True and not (
                running and self.data.get(f"{prefix}_active") is True
            ):
                raise ServiceValidationError(
                    "The kitchen timer is not running."
                    if running
                    else "The kitchen timer has not finished."
                )
            # Like the app's Cancel and "Tap to Dismiss", writing 0 stops a running timer
            # or clears a finished one.
            await self._async_write({key: 0})

    async def _async_write(
        self, properties: dict, *, force: bool = False, start: bool = False
    ) -> None:
        unit = self.device.get("temperature_unit")
        if properties and not force and all(held_for_start(self.data, key) for key in properties):
            if not self._push.healthy or not self.client.push_connected:
                # Remote Ready could end and return unseen before the start.
                raise ServiceValidationError(
                    "Home Assistant is reconnecting to the appliance; try again."
                )
            validate_control_properties(self.start_state, unit, properties)
            held = {**self.start_settings, **properties}
            for key in properties:
                if key.endswith("_cook_mode"):
                    # The climate card offers a temperature only once there is one.
                    setpoint = key.replace("cook_mode", "set_temp")
                    held[setpoint] = start_temperature(
                        {**self.data, key: held[key]}, setpoint, held.get(setpoint)
                    )
            self.start_settings = {k: v for k, v in held.items() if v is not None}
            self.async_update_listeners()
            return
        now = dt_util.utcnow()
        changes = {
            key: value
            for key, value in properties.items()
            if not (force and control_matches(self.data, key, value, now, self.clock_zone))
        }
        # A start's cooking mode and temperature that are still to be sent. Its power
        # write is left out, so every check still reads whether the oven reports ready.
        pending = {
            key: value
            for key, value in properties.items()
            if start and key.endswith(("_cook_mode", "_set_temp"))
        }
        if changes or not force:
            validate_control_properties(self.data, unit, changes, pending)
        requested_at = {}
        for key, value in properties.items():
            pending.pop(key, None)
            if not self.last_update_success:
                raise ServiceValidationError("The appliance is unavailable.")
            requested_at[key] = dt_util.utcnow()
            if (
                force
                or (
                    key in KITCHEN_TIMERS
                    # Setting a finished timer to 0 clears it, as the app does.
                    and (value > 0 or self.data.get(f"{KITCHEN_TIMERS[key]}_complete") is True)
                )
                or not control_matches(self.data, key, value, requested_at[key], self.clock_zone)
            ):
                requested_at[key] = await self._async_set_property(
                    key, value, resend=key not in changes, pending=pending
                )
        if not self.last_update_success or any(
            not control_matches(self.data, key, value, requested_at[key], self.clock_zone)
            for key, value in properties.items()
        ):
            raise HomeAssistantError("The appliance did not confirm the requested setting.")

    async def async_reset_air_filter(self) -> None:
        async with self._command("reset the air filter"):
            if not supports_air_filter_reset(self.data):
                raise ServiceValidationError("The appliance does not report an air filter.")
            await self.client.reset_air_filter(self.device_id)
            await self.async_refresh()

    async def async_set_operating_mode(self, selected: str | None) -> None:
        """Turn on the selected mode and the others off, or all off for none."""
        async with self._command():
            # Decided here, from the state the command starts with, which a status
            # read has just refreshed when live updates are not established.
            keys = operating_mode_keys(self.data)
            if not keys or (selected is not None and selected not in keys):
                raise ServiceValidationError("The appliance does not support that option.")
            # The other modes are cleared before the selected one is turned on.
            properties = {key: False for key in keys if key != selected}
            if selected is not None:
                properties[selected] = True
            await self._async_write(properties)

    async def async_set_ice_mode(self, mode: str) -> None:
        async with self._command():
            for key, value in ice_mode_properties(self.data, mode).items():
                await self._async_set_property(key, value)
            if ice_mode(self.data) != mode:
                raise HomeAssistantError("The appliance did not confirm the requested ice mode.")

    async def _async_attempt_set_property(
        self, key: str, value: bool | int, *, resend: bool, pending: dict | None
    ) -> tuple[datetime | None, ApiError | None]:
        requested_at = dt_util.utcnow()
        matched = control_matches(self.data, key, value, requested_at, self.clock_zone)
        if not resend or not matched:
            validate_control_properties(
                self.data, self.device.get("temperature_unit"), {key: value}, pending
            )
        confirmed = asyncio.Event()
        acknowledged = False
        command_error: ApiError | None = None

        @callback
        def confirm() -> None:
            if self.last_update_success and control_matches(
                self.data, key, value, requested_at, self.clock_zone
            ):
                confirmed.set()
            else:
                confirmed.clear()

        remove_listener = self.async_add_listener(confirm)
        try:
            try:
                async with asyncio.timeout(CONTROL_CONFIRM_TIMEOUT):
                    try:
                        await self.client.set_property(self.device_id, key, value)
                    except RateLimited:
                        raise
                    except ApiError as error:
                        command_error = error
                    else:
                        acknowledged = True
                        if key not in KITCHEN_TIMERS:
                            confirm()
                        await asyncio.wait_for(confirmed.wait(), CONTROL_PUSH_TIMEOUT)
            except TimeoutError:
                pass
            await self._async_confirm_command_generation()
            if matched and not acknowledged:
                # A value that already matched cannot confirm a write that
                # failed or went unanswered.
                return None, command_error
            if not confirmed.is_set():
                # A status read cancelled by the deadline would leave the
                # appliance marked as failed, so it runs afterwards.
                await self.async_refresh()
                confirm()
            return (requested_at if confirmed.is_set() else None), command_error
        finally:
            remove_listener()

    async def _async_set_property(
        self, key: str, value: bool | int, *, resend: bool = False, pending: dict | None = None
    ) -> datetime:
        last_error: ApiError | None = None
        for attempt in range(3):
            await self._async_confirm_command_generation()
            if not self.last_update_success:
                if attempt:
                    break
                raise ServiceValidationError("The appliance is unavailable.")
            confirmed_at, error = await self._async_attempt_set_property(
                key, value, resend=resend, pending=pending
            )
            if error is not None:
                last_error = error
            if confirmed_at is not None:
                return confirmed_at
        message = "The appliance did not confirm the requested setting."
        if last_error is not None:
            message += f" Last command error: {last_error}"
        raise HomeAssistantError(message) from last_error

    async def async_set_ice_delay(
        self,
        duration: int = 0,
        start_offset: int = 0,
        recurring: bool = False,
        *,
        end_current: bool = False,
    ) -> None:
        async with self._command():
            if not is_ice_maker(self.data) or not ICE_DELAY_KEYS.issubset(self.data):
                raise ServiceValidationError("The appliance does not report ice delay settings.")
            try:
                validate_ice_delay(duration, start_offset, recurring)
            except ValueError as error:
                raise ServiceValidationError(str(error)) from error
            if end_current and self.data.get("delay_active") is not True:
                raise ServiceValidationError("There is no active ice delay to end.")
            try:
                if end_current:
                    await self.client.exit_ice_delay(self.device_id)
                else:
                    await self.client.set_ice_delay(
                        self.device_id, duration, start_offset, recurring
                    )
            except RateLimited:
                raise
            except ApiError:
                await self.async_refresh()
                raise
            await self.async_refresh()

    @callback
    def _notify_read_error(self, error: Exception | None) -> None:
        if error is None:
            return
        if isinstance(error, ConfigEntryAuthFailed):
            self.entry.async_start_reauth(self.hass)
        elif self.entry.state is ConfigEntryState.LOADED:
            self.start_recovery_if_needed()

    @callback
    def _async_read_finished(self, task: asyncio.Task[StateRead]) -> None:
        if task.cancelled() or task.exception() is not None:
            return
        self._notify_read_error(task.result().error)

    async def async_refresh(self) -> None:
        """Read status once and publish its outcome."""
        result = await self._async_read_result()
        self._notify_read_error(result.error)

    async def async_request_refresh(self) -> None:
        """Wait for a completed read rather than a debounced request."""
        await self.async_read_status()

    async def async_config_entry_first_refresh(self) -> None:
        """Read initial state during setup, raising auth or not-ready errors."""
        result = await self._async_read_result()
        if result.error is None and result.epoch != self._push.epoch:
            # The connection changed around the read, so read again on it.
            result = await self._async_read_result()
        if isinstance(result.error, ConfigEntryAuthFailed):
            raise result.error
        if self.last_update_success and self.confirmed_at is not None and not self._push.expired:
            return
        cause = (
            result.error
            or self.last_exception
            or UpdateFailed("The appliance state was not reconfirmed after reconnecting.")
        )
        if self.last_update_success:
            self.last_exception = cause
            self.last_update_success = False
        # Without initial state, a status read has to confirm what arrives later.
        self._push.unconfirm(cause)
        error = ConfigEntryNotReady(str(cause))
        error.__cause__ = cause
        raise error

    async def async_read_status(self) -> StateRead:
        """Confirm state with a read that starts after this call."""
        result = await self._async_read_result()
        if error := result.error:
            self._notify_read_error(error)
            raise HomeAssistantError(str(error)) from error
        if result.epoch != self._push.epoch:
            raise HomeAssistantError(
                "The push connection changed during the status read; try again."
            )
        if result.confirmed_at is None:
            raise HomeAssistantError("The appliance did not report a usable state.")
        return result

    async def _async_read_result(self, *, report_error: bool = True) -> StateRead:
        self._read_requests += 1
        request = self._read_requests
        if report_error:
            self._pending_read_error_requests.add(request)
        try:
            while True:
                task = self._read_task
                if task is None or (
                    task.done()
                    and not task.cancelled()
                    and (task.exception() is not None or task.result().request < request)
                ):
                    task = self._read_task = self.entry.async_create_background_task(
                        self.hass, self._async_read_state(), "Sub-Zero state read"
                    )
                    task.add_done_callback(self._async_read_finished)
                result = await asyncio.shield(task)
                if result.request >= request:
                    return result
        finally:
            self._pending_read_error_requests.discard(request)

    async def _async_read_state(self) -> StateRead:
        request = self._read_requests
        report_error = bool(self._pending_read_error_requests)
        self._pending_read_error_requests.clear()
        confirmed_before = self.confirmed_at
        try:
            reply, pending = await self._async_fetch_state()
        except Exception as error:
            if not isinstance(error, UpdateFailed | ConfigEntryAuthFailed):
                _LOGGER.exception("Unexpected error fetching %s data", self.name)
            result = StateRead(request, None, error)
            deferred = isinstance(error, UpdateFailed) and error.retry_after is not None
            snapshot_arrived = (
                isinstance(error, UpdateFailed)
                and self.last_update_success
                and self.confirmed_at != confirmed_before
            )
            background_failure = (
                not report_error
                and isinstance(error, UpdateFailed)
                and self.last_update_success
                and (self.client.push_connected or self._push.in_grace)
            )
            if not deferred and not background_failure and not snapshot_arrived:
                self.async_set_update_error(error)
            return result
        self.unrecognized_keys.update(reply.keys() - STATE_KEYS)
        if pending.epoch != self._push.epoch:
            # The connection changed during the read, so the reply may be older than
            # what arrived since. The events in its history still happened.
            self._process_events(reply, history=True)
            return StateRead(request, None, epoch=pending.epoch)
        data = pending.merge(reply, self.data)
        confirmed_at = None
        if self._certify_state(data):
            confirmed_at = self.confirmed_at
            self._push.confirm(push_connected=self.client.push_connected)
        self.async_set_updated_data(data)
        self._process_events(data, history=True)
        return StateRead(request, confirmed_at, epoch=pending.epoch)

    async def _async_fetch_state(self) -> tuple[dict, PendingRead]:
        pending = PendingRead(self._push.epoch)
        self._active_read = pending
        try:
            started = time.monotonic()
            _LOGGER.debug("Reading status for %s", self.device["name"])
            try:
                reply = await self.client.state(self.device_id)
            except ApiError as error:
                _LOGGER.debug(
                    "%s status request failed after %.3fs: %s",
                    self.device["name"],
                    time.monotonic() - started,
                    type(error).__name__,
                )
                raise
            _LOGGER.debug(
                "%s status response after %.3fs: %s",
                self.device["name"],
                time.monotonic() - started,
                _state_for_log(reply),
            )
            if pending.error is not None:
                raise pending.error
            return reply, pending
        except InvalidAuth as error:
            raise ConfigEntryAuthFailed(str(error)) from error
        except RateLimited as error:
            raise UpdateFailed(str(error), retry_after=error.retry_after) from error
        except ApiError as error:
            raise UpdateFailed(str(error)) from error
        finally:
            self._active_read = None

    async def async_recover(self) -> None:
        """Restore unavailable state while the appliance is reporting again."""
        backoff = RECONNECT_DELAY
        try:
            while not self.last_update_success or not self._push.healthy:
                _LOGGER.debug("Refreshing %s to recover appliance state", self.device["name"])
                try:
                    result = await self._async_read_result(report_error=False)
                    if result.error is not None:
                        raise result.error
                    if result.epoch != self._push.epoch:
                        # The connection changed around the read, so read again on it.
                        continue
                    if not self._push.healthy and result.confirmed_at is None:
                        raise UpdateFailed(
                            "The appliance state was not reconfirmed after reconnecting."
                        )
                except ConfigEntryAuthFailed as error:
                    self.async_set_update_error(error)
                    self.entry.async_start_reauth(self.hass)
                    return
                except Exception as error:
                    if self.last_update_success and self._push.healthy:
                        return
                    if not self._push.in_grace:
                        self.async_set_update_error(error)
                    retry_after = (error.retry_after or 0) if isinstance(error, UpdateFailed) else 0
                else:
                    return
                await asyncio.sleep(max(backoff, retry_after) + random.uniform(0, 5))
                backoff = min(backoff * 2, MAX_RECONNECT_DELAY)
        finally:
            if self._recovery_task is asyncio.current_task():
                self._recovery_task = None

    def _count_snapshot_flips(self, update: StateUpdate, properties: dict, published: dict) -> None:
        # Either an update went missing, and the snapshot is seconds old, or the
        # snapshot arrived late, minutes old, and may restore a stale value. The age
        # is by the appliance clock, so a wrong clock or time zone skews it.
        flipped = sorted(
            key
            for key in properties.keys() & published.keys()
            if type(published[key]) is bool
            and type(self.data.get(key)) is bool
            and published[key] is not self.data[key]
        )
        if not flipped:
            return
        observed = appliance_datetime(update.properties.get("time"), self.clock_zone)
        age = round((dt_util.utcnow() - observed).total_seconds()) if observed else None
        self.push_stats["snapshot_flips"] += 1
        self.push_stats["last_snapshot_flip_age_seconds"] = age
        _LOGGER.debug(
            "%s snapshot changed %s without an update, snapshot age %s s",
            self.device["name"],
            ", ".join(flipped),
            age,
        )

    @callback
    def apply_update(self, update: StateUpdate, *, recover: bool = False) -> bool:
        self.push_stats["snapshots" if update.full else "updates"] += 1
        self.push_stats["last_received"] = dt_util.utcnow().isoformat()
        if update.full and snapshot_is_older(update.properties, self.data, self.clock_zone):
            _LOGGER.debug("Ignoring an older snapshot from %s", self.device["name"])
            # It is not state, but it did arrive on the appliance's channel.
            self.on_channel_opened(recover=recover)
            return False
        self.unrecognized_keys.update(update.properties.keys() - STATE_KEYS)
        if zone := clock_zone(update.properties.get("time")):
            self.clock_zone = zone
        properties = {key: value for key, value in update.properties.items() if key in STATE_KEYS}
        confirmed_snapshot = update.full and self._certify_state(properties)
        # Like a read, a snapshot after an interruption replaces cached state, so
        # values only pushed as partial updates do not outlive it.
        replace = update.full and (
            not self.last_update_success
            or properties.get("appliance_model") != self.data.get("appliance_model")
            or (confirmed_snapshot and not self._push.healthy)
        )
        if confirmed_snapshot and not self._sign_in_failed:
            # It keeps the appliance available with what it carries. A status read
            # still has to confirm state before live updates count as established.
            self._push.extend_grace()
        if "appliance_type" in properties and appliance_type(properties) is None:
            # Like the app, an unusable type does not replace the known one.
            del properties["appliance_type"]
        if "notifs" in properties:
            properties["notifs"] = notification_records(properties)
        # An update shows the appliance's channel works.
        opened = bool(properties) and self._push.channel_opened()
        _LOGGER.debug(
            "%s %s: %s",
            self.device["name"],
            "snapshot" if update.full else "update",
            _state_for_log(properties),
        )
        recorded = properties
        if self.last_update_success or update.full:
            updated = discard_excluded(
                properties if replace else {**self.data, **properties}, self.data
            )
            if replace:
                # A read in flight takes over what the snapshot left as state, which
                # includes a type the snapshot omitted and state already knew.
                recorded = updated
        if properties and self._active_read is not None:
            self._active_read.record_update(recorded, replace=replace)
        if self.last_update_success or update.full:
            if update.full and not replace:
                self._count_snapshot_flips(update, properties, updated)
            if (
                update.full
                or updated != self.data
                or any(
                    type(value) is not type(self.data.get(key)) for key, value in updated.items()
                )
                or not self.last_update_success
                or opened
            ):
                self.async_set_updated_data(updated)
        if update.full or "notifs" in properties:
            self._process_events(properties, history=update.full)
        if recover and properties:
            if update.full and self._sign_in_failed:
                # Sign-in may work again, and only a status read can show that.
                self.start_recovery()
            else:
                self.start_recovery_if_needed()
        return True
