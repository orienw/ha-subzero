"""Push updates with bounded reconnect attempts."""

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, tzinfo
from itertools import cycle

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    ApiError,
    ApplianceFault,
    ChannelOpened,
    ConnectionLimitReached,
    RateLimited,
    StateUpdate,
    SubZeroClient,
    notification_records,
    validate_ice_delay,
)
from .auth import InvalidAuth
from .const import (
    BINARY_KEYS,
    CONF_FIREBASE_ALERTS,
    CONTROL_CONFIRM_TIMEOUT,
    CONTROL_PUSH_TIMEOUT,
    DEFAULT_STATUS_POLL_INTERVAL,
    DOMAIN,
    FAULT_METADATA_APPLIES_TO_BY_SERIES,
    ICE_DELAY_KEYS,
    KITCHEN_TIMERS,
    MAX_RECONNECT_DELAY,
    PRIVATE_KEYS,
    RECONNECT_DELAY,
    STATE_KEYS,
    STATUS_POLL_INTERVALS,
)
from .controls import (
    appliance_datetime,
    appliance_type,
    clock_zone,
    control_matches,
    excluded_properties,
    held_for_start,
    ice_mode,
    ice_mode_properties,
    is_dishwasher,
    is_hood,
    is_ice_maker,
    is_oven,
    start_properties,
    start_temperature,
    supports_air_filter_reset,
    validate_control_properties,
    wash_cancel_enabled,
)
from .state import (
    EventTracker,
    PendingRead,
    StateConfirmation,
    StateRead,
    discard_excluded,
    snapshot_is_older,
    with_known_type,
)

_LOGGER = logging.getLogger(__name__)
INITIAL_STATE_TIMEOUT = 16
TRANSPORT_GRACE_SECONDS = 120
# The state-check interval while push is down, within TRANSPORT_GRACE_SECONDS.
OUTAGE_STATUS_INTERVAL = 60
FOLLOWUP_DELAYS = (2, 60, 120)
FOLLOWUP_KEYS = {key for key in BINARY_KEYS if key.endswith(("door_ajar", "timer_active"))} | {
    "kitchen_timer2_active",
    "wash_cycle_on",
}


def status_poll_interval(entry: ConfigEntry) -> int:
    interval = entry.options.get("status_poll_interval", DEFAULT_STATUS_POLL_INTERVAL)
    return interval if interval in STATUS_POLL_INTERVALS else DEFAULT_STATUS_POLL_INTERVAL


def firebase_alerts_enabled(entry: ConfigEntry) -> bool:
    return entry.options.get(CONF_FIREBASE_ALERTS, False) is True


def selected_devices(entry: ConfigEntry) -> dict[str, dict]:
    """Return the appliance selection, including entries awaiting migration."""
    if "device_id" in entry.data:
        return {
            entry.data["device_id"]: {
                "name": entry.title,
                "temperature_unit": entry.data.get("temperature_unit"),
            }
        }
    return entry.options.get("devices", entry.data["devices"])


def _state_for_log(properties: dict) -> dict:
    return {
        key: value
        for key, value in properties.items()
        if key in STATE_KEYS and key not in PRIVATE_KEYS and not isinstance(value, dict | list)
    }


class SubZeroCoordinator(DataUpdateCoordinator[dict]):
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: SubZeroClient,
        device_id: str,
        device: dict,
        read_failed: Callable[[SubZeroCoordinator], None],
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
        self._read_failed = read_failed
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
        self._state_lock = asyncio.Lock()
        self._read_requests = 0
        self._pending_read_error_requests: set[int] = set()
        self._read_task: asyncio.Task[StateRead] | None = None
        self._active_read: PendingRead | None = None
        self._channel_error: ApiError | None = None
        # Sign-in failed and no update has succeeded since. Kept apart from
        # last_exception, which a later transport error replaces.
        self._sign_in_failed = False
        self._transport_error: ApiError | None = None
        self._transport_generation = 0
        self._transport_grace_task: asyncio.Task | None = None
        self._events = EventTracker(dt_util.utcnow())
        self._event_listeners: list[Callable[[dict], None]] = []
        self._followup_task: asyncio.Task | None = None
        self._followup_alert = False
        self._last_followup = 0.0
        self._confirmation = StateConfirmation()
        self.verification_stats: dict[str, int | str | None] = {
            "checks": 0,
            "complete_snapshots": 0,
            "status_reads": 0,
            "last_success": None,
        }

    @property
    def confirmed_at(self) -> datetime | None:
        return self._confirmation.confirmed_at

    @property
    def missing_state_keys(self) -> dict[str, list[str]]:
        return self._confirmation.missing_keys

    @property
    def confirmed_missing_keys(self) -> list[str]:
        return self._confirmation.confirmed_missing_keys

    @property
    def transport_healthy(self) -> bool:
        return self._transport_error is None

    @property
    def waiting_for_push(self) -> bool:
        return self._transport_error is not None and self._channel_error is None

    @property
    def needs_recovery(self) -> bool:
        return (
            not self.last_update_success or self._transport_error is not None
        ) and self._channel_error is None

    @property
    def can_verify_state(self) -> bool:
        if self._sign_in_failed:
            return False
        return self.last_update_success or (
            self._channel_error is None
            and not isinstance(self.last_exception, ConfigEntryAuthFailed | InvalidAuth)
        )

    @callback
    def on_channel_opened(self) -> None:
        if self._active_read is not None:
            self._active_read.clear_error()
        self._channel_error = None

    @callback
    def on_channel_error(self, error: ApiError) -> None:
        self._channel_error = error
        self.async_set_update_error(error)

    @callback
    def on_account_error(self, error: Exception) -> None:
        if isinstance(error, InvalidAuth) and self._active_read is not None:
            self._active_read.fail(error)
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
        first = self._transport_error is None
        self._transport_error = error
        # A channel error belongs to the connection that ended, so status reads cover
        # every appliance until the stream returns.
        self._channel_error = None
        if first or lost:
            # A failed attempt that delivered nothing cannot have changed state.
            self._transport_generation += 1
            # Remote Ready may end and return unseen while updates are missed.
            self.start_settings = {}
        if not self.data:
            # Before the first state nothing can go stale, and setup reads the state.
            return
        healthy = self.last_update_success and bool(self.data.get("appliance_model"))
        if first and healthy:
            self._async_start_transport_grace()
        if healthy and self._transport_grace_task is not None:
            self.async_update_listeners()
        else:
            self.async_set_update_error(error)

    @callback
    def _async_cancel_transport_grace(self) -> None:
        if self._transport_grace_task is not None:
            self._transport_grace_task.cancel()
            self._transport_grace_task = None

    @callback
    def _async_start_transport_grace(self) -> None:
        self._async_cancel_transport_grace()
        self._transport_grace_task = self.entry.async_create_background_task(
            self.hass, self._async_transport_grace(), "Sub-Zero reconnect grace"
        )

    async def _async_transport_grace(self) -> None:
        await asyncio.sleep(TRANSPORT_GRACE_SECONDS)
        self._transport_grace_task = None
        if self._transport_error is not None:
            self.async_set_update_error(self._transport_error)

    @callback
    def _async_confirm_transport_state(self) -> None:
        if self._transport_error is None:
            return
        if self.client.push_connected:
            self._transport_error = None
            self._async_cancel_transport_grace()
        else:
            # A current read is useful while disconnected, but its values still age.
            self._async_start_transport_grace()

    @callback
    def async_set_update_error(self, err: Exception) -> None:
        self._async_cancel_transport_grace()
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
        if self._transport_error is not None and self._transport_grace_task is None:
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
    async def _command(self) -> AsyncIterator[None]:
        """Serialize commands.

        A cancelled command can still change the appliance after the last read, so
        the next command reads state before deciding what to write.
        """
        async with self._command_lock:
            if self._unsettled:
                await self.async_refresh()
                self._unsettled = False
            if (
                self._transport_error is not None
                or not self.client.push_connected
                or self._confirmation.missing_read_keys(self.data)
            ):
                try:
                    await self._async_read_for_control()
                except HomeAssistantError as error:
                    raise ServiceValidationError(str(error)) from error
            self._command_generation = self._transport_generation
            try:
                yield
            except asyncio.CancelledError:
                self._unsettled = True
                raise

    async def _async_read_for_control(self) -> None:
        """Refresh state before a write that would otherwise check reduced state.

        A write's interlock checks skip keys the state does not carry, so when the
        state lacks a key status reads have reported, whether a reduced reply or a
        reduced snapshot that replaced state left it that way, a read restores it
        first. The write then proceeds on what the read returned: a reply that still
        lacks the key does not block it, since the appliance enforces its own
        interlocks and refusing would refuse every control for as long as replies
        lack that key. A reply discarded for a snapshot that replaced state is
        another matter: the snapshot may lack what the reply carried, so the write
        waits for a read that stands.
        """
        result = await self.async_read_status()
        for _ in range(2):
            if not (result.superseded and result.missing_keys):
                return
            result = await self.async_read_status()
        if result.superseded and result.missing_keys:
            raise HomeAssistantError(
                "The push connection kept changing during the status read; try again."
            )

    async def _async_confirm_command_generation(self) -> None:
        if self._command_generation != self._transport_generation:
            await self._async_read_for_control()
            self._command_generation = self._transport_generation

    async def async_set_properties(self, properties: dict, *, force: bool = False) -> None:
        """Serialize writes and confirm their result from appliance state.

        Forced writes go out even when the appliance already reports the value,
        which is how the app starts ovens and cancels wash cycles. A value the appliance already reports
        is re-sent as is, without the checks a change needs.
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
            if not self.last_update_success:
                raise ServiceValidationError("The appliance is unavailable.")
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
            if not self.last_update_success:
                raise ServiceValidationError("The appliance is unavailable.")
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
        if not self.last_update_success:
            raise ServiceValidationError("The appliance is unavailable.")
        unit = self.device.get("temperature_unit")
        if properties and not force and all(held_for_start(self.data, key) for key in properties):
            if (
                self._transport_error is not None
                or self._channel_error is not None
                or not self.client.push_connected
            ):
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
        try:
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
                    or not control_matches(
                        self.data, key, value, requested_at[key], self.clock_zone
                    )
                ):
                    requested_at[key] = await self._async_set_property(
                        key, value, resend=key not in changes, pending=pending
                    )
            if not self.last_update_success or any(
                not control_matches(self.data, key, value, requested_at[key], self.clock_zone)
                for key, value in properties.items()
            ):
                raise HomeAssistantError("The appliance did not confirm the requested setting.")
        except InvalidAuth as error:
            self.entry.async_start_reauth(self.hass)
            raise HomeAssistantError("Sign in to Sub-Zero again to change settings.") from error
        except ApiError as error:
            raise HomeAssistantError(str(error)) from error

    async def async_reset_air_filter(self) -> None:
        async with self._command():
            if not self.last_update_success:
                raise ServiceValidationError("The appliance is unavailable.")
            if not supports_air_filter_reset(self.data):
                raise ServiceValidationError("The appliance does not report an air filter.")
            try:
                await self.client.reset_air_filter(self.device_id)
            except InvalidAuth as error:
                self.entry.async_start_reauth(self.hass)
                raise HomeAssistantError(
                    "Sign in to Sub-Zero again to reset the air filter."
                ) from error
            except ApiError as error:
                raise HomeAssistantError(str(error)) from error
            await self.async_refresh()

    async def async_set_ice_mode(self, mode: str) -> None:
        async with self._command():
            if not self.last_update_success:
                raise ServiceValidationError("The appliance is unavailable.")
            properties = ice_mode_properties(self.data, mode)
            try:
                for key, value in properties.items():
                    await self._async_set_property(key, value)
            except InvalidAuth as error:
                self.entry.async_start_reauth(self.hass)
                raise HomeAssistantError("Sign in to Sub-Zero again to change settings.") from error
            except ApiError as error:
                raise HomeAssistantError(str(error)) from error
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
            if not self.last_update_success:
                raise ServiceValidationError("The appliance is unavailable.")
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
            except InvalidAuth as error:
                self.entry.async_start_reauth(self.hass)
                raise HomeAssistantError("Sign in to Sub-Zero again to change settings.") from error
            except RateLimited as error:
                raise HomeAssistantError(str(error)) from error
            except ApiError as error:
                await self.async_refresh()
                raise HomeAssistantError(str(error)) from error
            await self.async_refresh()

    @callback
    def _notify_read_error(self, error: Exception | None) -> None:
        if error is None:
            return
        if isinstance(error, ConfigEntryAuthFailed):
            self.entry.async_start_reauth(self.hass)
        else:
            self._read_failed(self)

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
        if isinstance(result.error, ConfigEntryAuthFailed):
            raise result.error
        if result.error is not None:
            if self.last_update_success:
                self.last_exception = result.error
                self.last_update_success = False
            error = ConfigEntryNotReady(str(result.error))
            error.__cause__ = result.error
            raise error
        if not self.last_update_success or (
            self._transport_error is not None and self._transport_grace_task is None
        ):
            cause = self.last_exception or UpdateFailed(
                "The appliance state was not reconfirmed after reconnecting."
            )
            error = ConfigEntryNotReady(str(cause))
            error.__cause__ = cause
            raise error

    async def async_read_status(self) -> StateRead:
        """Confirm state with a read that starts after this call."""
        result = await self._async_read_result()
        if error := result.error:
            self._notify_read_error(error)
            raise HomeAssistantError(str(error)) from error
        if result.conflicted:
            raise HomeAssistantError("Appliance state changed during the status read; try again.")
        if self._confirmation.transport_generation != self._transport_generation:
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
        confirmation = self._confirmation.count
        try:
            data, conflicted, missing, superseded = await self._async_fetch_state()
        except Exception as error:
            result = StateRead(request, None, error)
            deferred = isinstance(error, UpdateFailed) and error.retry_after is not None
            background_failure = (
                not report_error
                and isinstance(error, UpdateFailed)
                and self.last_update_success
                and (self.client.push_connected or self._transport_grace_task is not None)
                and self._channel_error is None
            )
            if not deferred and not background_failure:
                self.async_set_update_error(error)
            return result
        self.async_set_updated_data(data)
        self._process_events(data, history=True)
        confirmed = (
            self.confirmed_at
            if not conflicted
            and self._confirmation.count > confirmation
            and self._confirmation.transport_generation == self._transport_generation
            else None
        )
        result = StateRead(
            request,
            confirmed,
            conflicted=conflicted,
            missing_keys=tuple(missing),
            superseded=superseded,
        )
        if superseded and missing:
            # The snapshot that replaced state superseded the reply, and lacks keys
            # the reply would have supplied, so a fresh read fills them in.
            self.entry.async_create_background_task(
                self.hass, self._async_read_result(report_error=False), "Sub-Zero gap read"
            )
        return result

    async def _async_fetch_state(self) -> tuple[dict, bool, list[str], bool]:
        async with self._state_lock:
            pending = PendingRead(self._transport_generation)
            self._active_read = pending
            try:
                data, received = await self._async_request_state(pending)
                if pending.error is not None:
                    raise pending.error
                return self._resolve_read(pending, data, received)
            except InvalidAuth as error:
                raise ConfigEntryAuthFailed(str(error)) from error
            except RateLimited as error:
                raise UpdateFailed(str(error), retry_after=error.retry_after) from error
            except ApiError as error:
                raise UpdateFailed(str(error)) from error
            finally:
                self._active_read = None

    async def _async_request_state(self, pending: PendingRead) -> tuple[dict, bool]:
        started = time.monotonic()
        _LOGGER.debug("Reading status for %s", self.device["name"])
        try:
            data = await self.client.state(self.device_id)
        except ApiError as error:
            _LOGGER.debug(
                "%s status request failed after %.3fs: %s",
                self.device["name"],
                time.monotonic() - started,
                type(error).__name__,
            )
            updates, _, _, _ = pending.replay(self.data, self.clock_zone)
            model = updates.get("appliance_model")
            # Only a complete snapshot rescues a failed request: the rescue
            # merges into cached state, so a snapshot lacking a key would
            # confirm whatever that key last held.
            if not isinstance(model, str) or not model or pending.snapshot is None:
                raise
            return self.data, False
        _LOGGER.debug(
            "%s status response after %.3fs: %s",
            self.device["name"],
            time.monotonic() - started,
            _state_for_log(data),
        )
        return data, True

    def _resolve_read(
        self, pending: PendingRead, data: dict, received: bool
    ) -> tuple[dict, bool, list[str], bool]:
        superseded = False
        updates, partial_keys, dropped, restarted = pending.replay(data, self.clock_zone)
        current = pending.transport_generation == self._transport_generation
        if received and restarted and not current:
            # The reply predates a snapshot that replaced state after a reconnect.
            # Like the updates before that snapshot it does not survive, and the
            # snapshot rescues the read as if the request had failed.
            data, received, superseded = self.data, False, True
        # Arrival during a GET does not establish when a partial change
        # happened. Publish it immediately, but do not certify the merged
        # state when it contradicts the status reply or a complete pushed
        # snapshot that rescued a failed GET. Against a reply, dropped values
        # stand in for keys it supplies; a rescue must keep every key.
        reference = data if received else pending.snapshot
        observed_keys = partial_keys if received else updates.keys() | partial_keys
        observed = {**dropped, **updates} if received else updates
        model = updates.get("appliance_model")
        other_model = isinstance(model, str) and model and model != data.get("appliance_model")
        # Clock and signal readings differ between any two observations, an
        # observation without a usable type keeps the known one, and a
        # property the app discards for the type the published state will
        # carry never reaches it.
        published = with_known_type({**({} if other_model else data), **updates}, self.data)
        conflicted = reference is not None and any(
            key not in reference
            or key not in observed
            or observed[key] != reference[key]
            or type(observed[key]) is not type(reference[key])
            for key in observed_keys
            - {"notifs", "time", "uptime", "ap_rssi", "appliance_type"}
            - excluded_properties(published)
        )
        if received and (not model or model == data.get("appliance_model")):
            # A reply published after a conflict or a reconnect still shows
            # what reads report, so it is recorded without certifying state.
            self._confirmation.confirm(
                data,
                self.data,
                "read",
                self._transport_generation,
                certify=current and not conflicted,
            )
            # The reply and newer updates replace cached state, so a current
            # reply ends the wait on reconnect whatever it lacks.
            if current:
                self._async_confirm_transport_state()
        self.unrecognized_keys.update(data.keys() - STATE_KEYS)
        if "notifs" in data:
            data = {**data, "notifs": notification_records(data)}
        if other_model:
            data = {}
        history = data.get("notifs")
        data = {
            **{key: value for key, value in data.items() if key in STATE_KEYS},
            **updates,
        }
        if history is not None and received:
            # A rescued or superseded read starts from cached state, which
            # already holds the records pushed during the read.
            data["notifs"] = [*history, *updates.get("notifs", [])]
        data = discard_excluded(data, self.data)
        # What the published state lacks of the keys reads report, for the
        # read_status response. A snapshot during the read may have confirmed
        # state the result no longer carries.
        missing = self._confirmation.missing_read_keys(data)
        return data, conflicted, missing, superseded

    async def async_recover(self) -> None:
        """Restore unavailable state while the appliance is reporting again."""
        backoff = RECONNECT_DELAY
        while not self.last_update_success or self._transport_error is not None:
            _LOGGER.debug("Refreshing %s to recover appliance state", self.device["name"])
            try:
                result = await self._async_read_result(report_error=False)
                if result.error is not None:
                    raise result.error
                if self._transport_error is not None and result.confirmed_at is None:
                    raise UpdateFailed(
                        "The appliance state was not reconfirmed after reconnecting."
                    )
            except ConfigEntryAuthFailed as error:
                self.async_set_update_error(error)
                self.entry.async_start_reauth(self.hass)
                return
            except Exception as error:
                if self.last_update_success and self._transport_error is None:
                    return
                if not isinstance(error, UpdateFailed):
                    _LOGGER.exception("Sub-Zero state recovery failed unexpectedly")
                if self._transport_grace_task is None:
                    self.async_set_update_error(error)
                retry_after = (error.retry_after or 0) if isinstance(error, UpdateFailed) else 0
            else:
                if not self.last_update_success:
                    self.async_set_updated_data(self.data)
                return
            await asyncio.sleep(max(backoff, retry_after) + random.uniform(0, 5))
            backoff = min(backoff * 2, MAX_RECONNECT_DELAY)

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
    def apply_update(self, update: StateUpdate) -> bool:
        self.push_stats["snapshots" if update.full else "updates"] += 1
        self.push_stats["last_received"] = dt_util.utcnow().isoformat()
        if update.full and snapshot_is_older(update.properties, self.data, self.clock_zone):
            _LOGGER.debug("Ignoring an older snapshot from %s", self.device["name"])
            return False
        self.unrecognized_keys.update(update.properties.keys() - STATE_KEYS)
        if zone := clock_zone(update.properties.get("time")):
            self.clock_zone = zone
        properties = {key: value for key, value in update.properties.items() if key in STATE_KEYS}
        confirmed_snapshot = update.full and self._confirmation.confirm(
            properties, self.data, "snapshot", self._transport_generation
        )
        # Like a read, the snapshot that ends a push interruption replaces cached
        # state, so values only pushed as partial updates do not outlive it.
        replace = update.full and (
            not self.last_update_success
            or properties.get("appliance_model") != self.data.get("appliance_model")
            or (confirmed_snapshot and self._transport_error is not None)
        )
        if confirmed_snapshot:
            self._async_confirm_transport_state()
        # The snapshot restores availability with what it carries; a read fills in
        # the keys it lacked instead of holding the appliance back. A read already
        # running publishes its own view of the state.
        fill_gaps = (
            confirmed_snapshot
            and replace
            and bool(self.missing_state_keys["snapshot"])
            and self._active_read is None
        )
        if "appliance_type" in properties and appliance_type(properties) is None:
            # Like the app, an unusable type does not replace the known one.
            del properties["appliance_type"]
        if "notifs" in properties:
            properties["notifs"] = notification_records(properties)
        if properties:
            self._channel_error = None
            if self._active_read is not None:
                self._active_read.record_update(
                    properties,
                    full=update.full,
                    confirmed_snapshot=confirmed_snapshot,
                    replace=replace,
                    complete=not self.missing_state_keys["snapshot"],
                )
        if confirmed_snapshot and not self.missing_state_keys["snapshot"]:
            self.verification_stats["complete_snapshots"] += 1
        _LOGGER.debug(
            "%s %s: %s",
            self.device["name"],
            "snapshot" if update.full else "update",
            _state_for_log(properties),
        )
        if self.last_update_success or update.full:
            updated = discard_excluded(
                properties if replace else {**self.data, **properties}, self.data
            )
            if update.full and not replace:
                self._count_snapshot_flips(update, properties, updated)
            if (
                update.full
                or updated != self.data
                or any(
                    type(value) is not type(self.data.get(key)) for key, value in updated.items()
                )
                or not self.last_update_success
            ):
                self.async_set_updated_data(updated)
        if update.full or "notifs" in properties:
            self._process_events(properties, history=update.full)
        if fill_gaps:
            # Started after the snapshot is published, so the read replaces it.
            self.entry.async_create_background_task(
                self.hass, self._async_read_result(report_error=False), "Sub-Zero gap read"
            )
        return True


class SubZeroFaultsCoordinator(DataUpdateCoordinator[list[ApplianceFault]]):
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: SubZeroClient,
        appliance: SubZeroCoordinator,
        metadata: dict[tuple[str, str], dict | None],
    ):
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {appliance.device['name']} faults",
            update_interval=timedelta(minutes=30),
        )
        self.client = client
        self.appliance = appliance
        self._metadata = metadata

    def series_name(self) -> str | None:
        parts = appliance_type(self.appliance.data)
        if parts is None:
            return None
        return FAULT_METADATA_APPLIES_TO_BY_SERIES.get(parts[1])

    def metadata(self, code: str | None) -> dict | None:
        series = self.series_name()
        if not code or not series:
            return None
        return self._metadata.get((code, series))

    async def _async_update_data(self) -> list[ApplianceFault]:
        try:
            faults = await self.client.appliance_faults(self.appliance.device_id)
        except InvalidAuth as error:
            raise ConfigEntryAuthFailed(str(error)) from error
        except RateLimited as error:
            raise UpdateFailed(str(error), retry_after=error.retry_after) from error
        except ApiError as error:
            raise UpdateFailed(str(error)) from error
        series = self.series_name()
        if series is not None:
            for fault in faults:
                key = (fault.code, series)
                if fault.active and fault.code and key not in self._metadata:
                    metadata = await self.client.fault_metadata(fault.code, series)
                    if isinstance(metadata, dict):
                        self._metadata[key] = metadata
        return faults


class SubZeroAccount:
    """Share account tokens and one notification stream across selected appliances."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: SubZeroClient):
        self.hass = hass
        self.entry = entry
        self.client = client
        self.alerts = None
        self.options_lock = asyncio.Lock()
        self.coordinators = {
            device_id: SubZeroCoordinator(hass, entry, client, device_id, device, self._read_failed)
            for device_id, device in selected_devices(entry).items()
        }
        self._fault_metadata: dict[tuple[str, str], dict] = {}
        self.fault_coordinators = {
            device_id: SubZeroFaultsCoordinator(
                hass, entry, client, coordinator, self._fault_metadata
            )
            for device_id, coordinator in self.coordinators.items()
        }
        self._initial_states = {device_id: asyncio.Event() for device_id in self.coordinators}
        self._recoveries: dict[str, asyncio.Task] = {}
        self._verification_order = cycle(self.coordinators.values())
        self._verifications: dict[str, asyncio.Task] = {}
        self._state_check_interval: int | None = None
        self._outage_checks = False
        self._cancel_state_checks: Callable[[], None] | None = None
        self.entry.async_on_unload(self._async_cancel_state_checks)

    async def async_setup(self) -> None:
        if not self.coordinators:
            return
        selected = selected_devices(self.entry)
        metadata_error: ApiError | None = None
        try:
            appliances = await self.client.appliances()
        except InvalidAuth as error:
            raise ConfigEntryAuthFailed(str(error)) from error
        except RateLimited as error:
            raise ConfigEntryNotReady(str(error)) from error
        except ApiError as error:
            metadata_error = error
        else:
            units = {appliance.id: appliance.temperature_unit for appliance in appliances}
            for coordinator in self.coordinators.values():
                if coordinator.device_id in units:
                    coordinator.device["temperature_unit"] = units[coordinator.device_id]
            devices = {
                device_id: dict(coordinator.device)
                for device_id, coordinator in self.coordinators.items()
            }
            if selected_devices(self.entry) == selected and devices != selected:
                if "devices" in self.entry.options:
                    self.hass.config_entries.async_update_entry(
                        self.entry, options={**self.entry.options, "devices": devices}
                    )
                else:
                    self.hass.config_entries.async_update_entry(
                        self.entry, data={**self.entry.data, "devices": devices}
                    )
        self.entry.async_create_background_task(self.hass, self.listen(), "Sub-Zero notifications")
        try:
            try:
                async with asyncio.timeout(INITIAL_STATE_TIMEOUT):
                    await asyncio.gather(*(ready.wait() for ready in self._initial_states.values()))
            except TimeoutError:
                pass
            errors = []
            for coordinator in self.coordinators.values():
                if coordinator.last_update_success and coordinator.data.get("appliance_model"):
                    continue
                if isinstance(coordinator.last_exception, InvalidAuth):
                    raise ConfigEntryAuthFailed(str(coordinator.last_exception))
                try:
                    await coordinator.async_config_entry_first_refresh()
                except ConfigEntryNotReady as error:
                    coordinator.data = {}
                    errors.append((coordinator, error))
            errors = [
                (coordinator, error)
                for coordinator, error in errors
                # Push may have delivered its state while setup read the others.
                if not (coordinator.last_update_success and coordinator.data.get("appliance_model"))
            ]
            if errors and len(errors) == len(self.coordinators):
                raise errors[0][1]
            for coordinator, error in errors:
                # Setup goes on for the others, so Home Assistant does not report this one.
                _LOGGER.warning(
                    "Could not read %s from Sub-Zero during setup, so it stays unavailable "
                    "until its state arrives: %s",
                    coordinator.device["name"],
                    error.__cause__,
                )
        finally:
            self._initial_states.clear()
        for coordinator in self.coordinators.values():
            if coordinator.needs_recovery and self.client.push_connected:
                self._start_recovery(coordinator)
        if metadata_error is not None:
            _LOGGER.warning(
                "Could not refresh appliance units; using cached units where available: %s",
                metadata_error,
            )
        for coordinator in self.fault_coordinators.values():
            self.entry.async_create_background_task(
                self.hass, coordinator.async_refresh(), "Sub-Zero faults"
            )
        self.async_update_state_checks()

    @callback
    def async_update_state_checks(self) -> None:
        """Apply the state-check interval without interrupting appliance updates."""
        if self.entry.pref_disable_polling:
            for coordinator in self.coordinators.values():
                coordinator.cancel_followup()
        interval = 0 if self.entry.pref_disable_polling else status_poll_interval(self.entry)
        self._outage_checks = not self.entry.pref_disable_polling and self._push_interrupted()
        if self._outage_checks:
            # Each current read keeps the appliance available for another
            # TRANSPORT_GRACE_SECONDS until push returns.
            interval = min(interval or OUTAGE_STATUS_INTERVAL, OUTAGE_STATUS_INTERVAL)
        if interval == self._state_check_interval:
            return
        self._state_check_interval = interval
        self._async_cancel_state_checks()
        if interval and self.coordinators:
            self._cancel_state_checks = async_track_time_interval(
                self.hass,
                self._verify_next_appliance,
                timedelta(seconds=interval / len(self.coordinators)),
                cancel_on_shutdown=True,
            )

    def _push_interrupted(self) -> bool:
        """Whether a reachable appliance waits for push to return and reconfirm its state."""
        return any(coordinator.waiting_for_push for coordinator in self.coordinators.values())

    @callback
    def _async_cancel_state_checks(self) -> None:
        if self._cancel_state_checks is not None:
            self._cancel_state_checks()
            self._cancel_state_checks = None

    @callback
    def _verify_next_appliance(self, _now: datetime) -> None:
        if self._outage_checks and not self._push_interrupted():
            # Push returned and reconfirmed state; go back to the configured interval.
            self.async_update_state_checks()
            return
        coordinator = next(self._verification_order)
        pending = self._verifications.get(coordinator.device_id)
        if (
            self.entry.pref_disable_polling
            or not self._state_check_interval
            or not coordinator.can_verify_state
            or (pending is not None and not pending.done())
        ):
            return
        self._verifications[coordinator.device_id] = self.entry.async_create_background_task(
            self.hass,
            coordinator.async_verify_state(),
            "Sub-Zero state verification",
        )

    @callback
    def set_error(self, error: Exception) -> None:
        for coordinator in self.coordinators.values():
            coordinator.on_account_error(error)
        for ready in self._initial_states.values():
            ready.set()

    @callback
    def _start_recovery(self, coordinator: SubZeroCoordinator) -> None:
        recovery = self._recoveries.get(coordinator.device_id)
        if recovery is None or recovery.done():
            self._recoveries[coordinator.device_id] = self.entry.async_create_background_task(
                self.hass, coordinator.async_recover(), "Sub-Zero state recovery"
            )

    @callback
    def _read_failed(self, coordinator: SubZeroCoordinator) -> None:
        if (
            self.entry.state is ConfigEntryState.LOADED
            and coordinator.needs_recovery
            and self.client.push_connected
            and not isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
        ):
            self._start_recovery(coordinator)

    async def listen(self) -> None:
        backoff = RECONNECT_DELAY
        while True:
            started = time.monotonic()
            recoveries = self._recoveries
            canceled: set[asyncio.Task] = set()
            delivered = False
            try:
                async for device_id, update in self.client.watch(list(self.coordinators)):
                    delivered = True
                    coordinator = self.coordinators[device_id]
                    accepted = True
                    if isinstance(update, ApiError):
                        coordinator.on_channel_error(update)
                    elif isinstance(update, ChannelOpened):
                        coordinator.on_channel_opened()
                    else:
                        accepted = coordinator.apply_update(update)
                    if isinstance(update, ApiError) or (
                        isinstance(update, StateUpdate)
                        and update.full
                        and accepted
                        and coordinator.transport_healthy
                    ):
                        if recovery := recoveries.pop(device_id, None):
                            canceled.add(recovery)
                            recovery.add_done_callback(canceled.discard)
                            recovery.cancel()
                        if ready := self._initial_states.get(device_id):
                            ready.set()
                    if (
                        (not coordinator.last_update_success or not coordinator.transport_healthy)
                        and device_id not in self._initial_states
                        and not isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
                        and (
                            isinstance(update, ChannelOpened)
                            or (
                                isinstance(update, StateUpdate)
                                and not update.properties.keys().isdisjoint(STATE_KEYS)
                            )
                        )
                    ):
                        self._start_recovery(coordinator)
                raise ApiError("Sub-Zero's notification stream ended.")
            except InvalidAuth as error:
                self.set_error(error)
                self.entry.async_start_reauth(self.hass)
                for recovery in recoveries.values():
                    recovery.cancel()
                    canceled.add(recovery)
                return
            except ApiError as error:
                for coordinator in self.coordinators.values():
                    coordinator.async_transport_error(error, lost=delivered)
                for ready in self._initial_states.values():
                    ready.set()
                # Status reads keep appliances available while push is down.
                self.async_update_state_checks()
                retry_after = error.retry_after if isinstance(error, RateLimited) else 0
                full = isinstance(error, ConnectionLimitReached)
            except Exception as error:
                _LOGGER.exception("Sub-Zero's notification stream failed unexpectedly")
                self.set_error(error)
                retry_after = 0
                full = False
            finally:
                pending = {*recoveries.values(), *canceled}
                for recovery in pending:
                    recovery.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                recoveries.clear()
            if full:
                # A full service admits a connection when another client leaves, so
                # the delay does not grow; it is already randomized.
                await asyncio.sleep(retry_after)
                continue
            if time.monotonic() - started >= 120:
                # A connection that worked for a while is tried again at once, then with
                # increasing delays.
                delay = retry_after
                backoff = RECONNECT_DELAY
            else:
                delay = max(backoff, retry_after)
                backoff = min(backoff * 2, MAX_RECONNECT_DELAY)
            await asyncio.sleep(delay + random.uniform(0, 5))
