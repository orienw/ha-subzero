"""Push updates with bounded reconnect attempts."""

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
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
    MAX_EVENT_HISTORY,
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
    ice_mode,
    ice_mode_properties,
    is_dishwasher,
    is_hood,
    is_ice_maker,
    is_oven,
    start_properties,
    supports_air_filter_reset,
    uptime_seconds,
    validate_control_properties,
    wash_cancel_enabled,
)

_LOGGER = logging.getLogger(__name__)
INITIAL_STATE_TIMEOUT = 16
TRANSPORT_GRACE_SECONDS = 120
FOLLOWUP_DELAYS = (2, 60, 120)
FOLLOWUP_KEYS = {key for key in BINARY_KEYS if key.endswith(("door_ajar", "timer_active"))} | {
    "kitchen_timer2_active",
    "wash_cycle_on",
}


@dataclass(frozen=True)
class StateRead:
    request: int
    confirmed_at: datetime | None
    error: Exception | None = None
    published: int = 0
    conflicted: bool = False


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
            "last_received": None,
        }
        self._command_lock = asyncio.Lock()
        self._command_generation = 0
        self._unsettled = False
        self._state_lock = asyncio.Lock()
        self._read_requests = 0
        self._pending_read_error_requests: set[int] = set()
        self._read_task: asyncio.Task[StateRead] | None = None
        self._completed_read: StateRead | None = None
        # Updates during a status read, each with whether it was a complete snapshot
        # and whether it replaced cached state.
        self._read_updates: list[tuple[StateUpdate, bool, bool]] | None = None
        self._read_snapshot: dict | None = None
        self._read_error: Exception | None = None
        self._channel_error: ApiError | None = None
        self._transport_error: ApiError | None = None
        self._transport_generation = 0
        self._transport_grace_task: asyncio.Task | None = None
        self._event_cutoff = dt_util.utcnow()
        self._event_ids: dict[tuple[datetime, int, int], str] = {}
        self._paired_events: set[tuple[datetime, int, int]] = set()
        self._event_listeners: list[Callable[[dict], None]] = []
        self._history_seen = False
        self._followup_task: asyncio.Task | None = None
        self._followup_alert = False
        self._last_followup = 0.0
        self.confirmed_at: datetime | None = None
        self._confirmation = 0
        self._confirmation_transport_generation = 0
        self._confirmation_model: str | None = None
        # The state keys each source has reported for the model. Status reads and
        # pushed snapshots can report different sets, so each is held to its own.
        self._required_state_keys: dict[str, set[str]] = {"snapshot": set(), "read": set()}
        self._published = 0
        self.missing_state_keys: dict[str, list[str]] = {"snapshot": [], "read": []}
        self.verification_stats: dict[str, int | str | None] = {
            "checks": 0,
            "complete_snapshots": 0,
            "status_reads": 0,
            "last_success": None,
        }

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
        if first or lost:
            # A failed attempt that delivered nothing cannot have changed state.
            self._transport_generation += 1
        healthy = (
            self.last_update_success
            and bool(self.data.get("appliance_model"))
            and self._channel_error is None
        )
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
        super().async_set_update_error(err)

    @callback
    def _confirm_state(self, properties: dict, source: str, *, certify: bool = True) -> bool:
        """Record what the source reports and certify the state when it is complete."""
        model = properties.get("appliance_model")
        if not isinstance(model, str) or not model:
            self.missing_state_keys[source] = ["appliance_model"]
            return False
        if model != self._confirmation_model:
            if not certify:
                # A reply that concurrent updates contradict does not switch models.
                return False
            self._confirmation_model = model
            for reported in self._required_state_keys.values():
                reported.clear()
        # Like _discard_excluded, an observation without a usable type keeps the
        # known one; properties the app discards for it are not state.
        typed = properties if appliance_type(properties) is not None else self.data
        excluded = excluded_properties(typed) if typed.get("appliance_model") == model else set()
        # A read replaces state, so it must cover what reads report. A snapshot
        # merges into that state, so it must also cover what snapshots report. A
        # key only ever pushed as a partial update is never required, since no
        # read or snapshot can refresh it.
        required = set(self._required_state_keys["read"])
        if source == "snapshot":
            required |= self._required_state_keys["snapshot"]
        missing = self.missing_state_keys[source] = sorted(required - properties.keys() - excluded)
        reported = properties.keys() & (STATE_KEYS - {"notifs"} - excluded)
        if appliance_type(properties) is None:
            # An unusable type never reaches state.
            reported.discard("appliance_type")
        self._required_state_keys[source].update(reported)
        if missing or not certify:
            return False
        self.confirmed_at = dt_util.utcnow()
        self._confirmation += 1
        self._confirmation_transport_generation = self._transport_generation
        return True

    def _active_conditions(self) -> set[str]:
        return {key for key in FOLLOWUP_KEYS if self.data.get(key) is True}

    @callback
    def async_set_updated_data(self, data: dict) -> None:
        self._published += 1
        if self._transport_error is not None and self._transport_grace_task is None:
            self.data = data
            self.async_update_listeners()
        else:
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
        # The first snapshot or status read sets the baseline, even without history.
        # Later history still passes through the startup timestamp cutoff.
        deliver = self._history_seen or not history
        self._history_seen |= history
        events = {}
        for record in notification_records(properties):
            timestamp = appliance_datetime(record["timestamp"], self.clock_zone)
            if timestamp is not None:
                # Identify by instant: a local time in a repeated hour never equals a UTC time.
                identity = (timestamp.astimezone(UTC), record["notif_seq"], record["notif_type"])
                events[identity] = timestamp
        for identity in sorted(events):
            _, sequence, code = identity
            self.async_receive_event(
                events[identity],
                sequence,
                code,
                deliver=deliver,
                source="history" if history else "channel",
            )

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
        identity = (timestamp.astimezone(UTC), sequence, code)
        if identity[0] < self._event_cutoff:
            return False
        if (known := self._event_ids.get(identity)) is not None:
            if (known == "firebase") != (source == "firebase"):
                self._paired_events.add(identity)
            return False
        # Firebase rounds event time to whole seconds; stream and history keep
        # milliseconds. Pair each event with at most one copy from the other path.
        counterpart = min(
            (
                old
                for old, origin in self._event_ids.items()
                if (origin == "firebase") != (source == "firebase")
                and old not in self._paired_events
                and old[1:] == (sequence, code)
                and abs(old[0] - identity[0]) < timedelta(seconds=1)
            ),
            key=lambda old: abs(old[0] - identity[0]),
            default=None,
        )
        self._event_ids[identity] = source
        if counterpart is not None:
            self._paired_events.update((identity, counterpart))
        if len(self._event_ids) > MAX_EVENT_HISTORY:
            oldest = min(self._event_ids)
            del self._event_ids[oldest]
            self._paired_events.discard(oldest)
            self._event_cutoff = max(self._event_cutoff, oldest[0] + timedelta(microseconds=1))
        if counterpart is not None or not deliver:
            return False
        event = {
            "code": code,
            "sequence": sequence,
            "appliance_timestamp": timestamp.isoformat(),
            "received_at": dt_util.utcnow().isoformat(),
            "source": source,
        }
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
            manufacturer=(
                "Cove"
                if is_dishwasher(self.data)
                else "Wolf"
                if is_oven(self.data) or is_hood(self.data)
                else "Sub-Zero"
            ),
            model=self.data["appliance_model"],
            serial_number=serial if isinstance(serial, str) else None,
            sw_version=version.get("fw") if isinstance(version, dict) else None,
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
            if self._transport_error is not None or not self.client.push_connected:
                try:
                    await self.async_read_status()
                except HomeAssistantError as error:
                    raise ServiceValidationError(str(error)) from error
            self._command_generation = self._transport_generation
            try:
                yield
            except asyncio.CancelledError:
                self._unsettled = True
                raise

    async def _async_confirm_command_generation(self) -> None:
        if self._command_generation != self._transport_generation:
            await self.async_read_status()
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
                await self._async_write(start_properties(self.data, key, temperature), force=True)

    async def async_cancel_wash(self) -> None:
        async with self._command():
            if not self.last_update_success:
                raise ServiceValidationError("The appliance is unavailable.")
            if not wash_cancel_enabled(self.data):
                raise ServiceValidationError("There is no wash cycle to cancel.")
            # Like the app, cancel even when the cycle already reports off, as it
            # may during a delayed start.
            await self._async_write({"wash_cycle_on": False}, force=True)

    async def async_dismiss_timer(self, key: str) -> None:
        async with self._command():
            if not self.last_update_success:
                raise ServiceValidationError("The appliance is unavailable.")
            if self.data.get(f"{KITCHEN_TIMERS[key]}_complete") is not True:
                raise ServiceValidationError("The kitchen timer has not finished.")
            # Like the app's "Tap to Dismiss", writing 0 clears a finished timer.
            await self._async_write({key: 0})

    async def _async_write(self, properties: dict, *, force: bool = False) -> None:
        if not self.last_update_success:
            raise ServiceValidationError("The appliance is unavailable.")
        now = dt_util.utcnow()
        changes = {
            key: value
            for key, value in properties.items()
            if not (force and control_matches(self.data, key, value, now, self.clock_zone))
        }
        if changes or not force:
            validate_control_properties(self.data, self.device.get("temperature_unit"), changes)
        requested_at = {}
        try:
            for key, value in properties.items():
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
                        key, value, resend=key not in changes
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

    async def _async_set_property(
        self, key: str, value: bool | int, *, resend: bool = False
    ) -> datetime:
        last_error = None
        for attempt in range(3):
            await self._async_confirm_command_generation()
            if not self.last_update_success:
                if attempt:
                    break
                raise ServiceValidationError("The appliance is unavailable.")
            requested_at = dt_util.utcnow()
            matched = control_matches(self.data, key, value, requested_at, self.clock_zone)
            if not resend or not matched:
                validate_control_properties(
                    self.data, self.device.get("temperature_unit"), {key: value}
                )
            confirmed = asyncio.Event()
            acknowledged = False

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
                            last_error = error
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
                    continue
                if not confirmed.is_set():
                    # A status read cancelled by the deadline would leave the
                    # appliance marked as failed, so it runs afterwards.
                    await self.async_refresh()
                    confirm()
                if confirmed.is_set():
                    return requested_at
            finally:
                remove_listener()
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

    async def async_refresh(self) -> None:
        """Finish reading status even if the caller is cancelled."""
        await asyncio.shield(
            self.entry.async_create_background_task(
                self.hass, super().async_refresh(), "Sub-Zero status read"
            )
        )

    async def async_request_refresh(self) -> None:
        """Wait for a completed read rather than a debounced request."""
        await self.async_read_status()

    async def async_read_status(self) -> datetime:
        """Confirm state with a read that starts after this call."""
        result = await self._async_read_result()
        if error := result.error:
            if isinstance(error, ConfigEntryAuthFailed):
                self.entry.async_start_reauth(self.hass)
            else:
                self._read_failed(self)
            raise HomeAssistantError(str(error)) from error
        if result.conflicted:
            raise HomeAssistantError("Appliance state changed during the status read; try again.")
        if (
            result.confirmed_at is None
            or self._confirmation_transport_generation != self._transport_generation
        ):
            raise HomeAssistantError("The appliance did not return a complete state snapshot.")
        return result.confirmed_at

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
        confirmation = self._confirmation
        try:
            data, conflicted, complete = await self._async_fetch_state()
        except Exception as error:
            result = StateRead(request, None, error, self._published)
            deferred = isinstance(error, UpdateFailed) and error.retry_after is not None
            background_failure = (
                not report_error
                and isinstance(error, UpdateFailed)
                and self.last_update_success
                and (self.client.push_connected or self._transport_grace_task is not None)
                and self._channel_error is None
            )
            # Unpublished failures must not be adopted by an older refresh.
            if not deferred and not background_failure:
                self._completed_read = result
                self.async_set_update_error(error)
            return result
        self.async_set_updated_data(data)
        self._process_events(data, history=True)
        confirmed = (
            self.confirmed_at
            if complete
            and not conflicted
            and self._confirmation > confirmation
            and self._confirmation_transport_generation == self._transport_generation
            else None
        )
        result = self._completed_read = StateRead(
            request, confirmed, published=self._published, conflicted=conflicted
        )
        return result

    @callback
    def _async_read_finished(self, task: asyncio.Task[StateRead]) -> None:
        if (
            task.cancelled()
            or task.exception() is not None
            or (error := task.result().error) is None
        ):
            return
        if isinstance(error, ConfigEntryAuthFailed):
            self.entry.async_start_reauth(self.hass)
        else:
            self._read_failed(self)

    @callback
    def _async_refresh_finished(self) -> None:
        if not self.last_update_success:
            self._read_failed(self)

    async def _async_update_data(self) -> dict:
        result = await self._async_read_result()
        if (latest := self._completed_read) is not None and latest.request > result.request:
            result = latest
        if result.error is not None and (
            isinstance(result.error, ConfigEntryAuthFailed) or self._published <= result.published
        ):
            raise result.error
        if self._transport_error is not None and self._transport_grace_task is None:
            raise UpdateFailed("The appliance did not return a complete state after reconnecting.")
        return self.data

    async def _async_fetch_state(self) -> tuple[dict, bool]:
        async with self._state_lock:
            transport_generation = self._transport_generation
            self._read_updates = []
            self._read_snapshot = None
            self._read_error = None
            try:
                received = True
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
                    updates, _, _, _ = self._pending_read_properties(self.data)
                    model = updates.get("appliance_model")
                    if not isinstance(model, str) or not model:
                        raise
                    data = self.data
                    received = False
                else:
                    _LOGGER.debug(
                        "%s status response after %.3fs: %s",
                        self.device["name"],
                        time.monotonic() - started,
                        _state_for_log(data),
                    )
                if error := self._read_error:
                    raise error
                updates, partial_keys, dropped, restarted = self._pending_read_properties(data)
                current = transport_generation == self._transport_generation
                if received and restarted and not current:
                    # The reply predates a snapshot that replaced state after a reconnect.
                    # Like the updates before that snapshot it does not survive, and the
                    # snapshot rescues the read as if the request had failed.
                    data, received = self.data, False
                # Arrival during a GET does not establish when a partial change
                # happened. Publish it immediately, but do not certify the merged
                # state when it contradicts the status reply or a complete pushed
                # snapshot that rescued a failed GET. Against a reply, dropped values
                # stand in for keys it supplies; a rescue must keep every key.
                reference = data if received else self._read_snapshot
                observed_keys = partial_keys if received else updates.keys() | partial_keys
                observed = {**dropped, **updates} if received else updates
                # Clock and signal readings differ between any two observations, and
                # an observation without a usable type keeps the known one.
                conflicted = reference is not None and any(
                    key not in reference
                    or key not in observed
                    or observed[key] != reference[key]
                    or type(observed[key]) is not type(reference[key])
                    for key in observed_keys
                    - {"notifs", "time", "uptime", "ap_rssi", "appliance_type"}
                )
                model = updates.get("appliance_model")
                if received and (not model or model == data.get("appliance_model")):
                    # A reply published after a conflict or a reconnect still shows
                    # what reads report, so it is recorded without certifying state.
                    complete = self._confirm_state(data, "read", certify=current and not conflicted)
                    # With push back, the reply and newer updates replace cached state,
                    # so a key it lacks or a change during it cannot leave the
                    # appliance waiting on reconnect forever.
                    if current and (complete or self.client.push_connected):
                        self._async_confirm_transport_state()
                self.unrecognized_keys.update(data.keys() - STATE_KEYS)
                if "notifs" in data:
                    data = {**data, "notifs": notification_records(data)}
                model = updates.get("appliance_model")
                if isinstance(model, str) and model and model != data.get("appliance_model"):
                    data = {}
                history = data.get("notifs")
                data = {
                    **{key: value for key, value in data.items() if key in STATE_KEYS},
                    **updates,
                }
                if history is not None:
                    data["notifs"] = [*history, *updates.get("notifs", [])]
                data = self._discard_excluded(data)
                # A snapshot during the read may have certified state that the published
                # result no longer carries, so the result must cover what reads report.
                complete = self._required_state_keys["read"] <= (
                    data.keys() | excluded_properties(data)
                )
                return data, conflicted, complete
            except InvalidAuth as error:
                raise ConfigEntryAuthFailed(str(error)) from error
            except RateLimited as error:
                raise UpdateFailed(str(error), retry_after=error.retry_after) from error
            except ApiError as error:
                raise UpdateFailed(str(error)) from error
            finally:
                self._read_updates = None
                self._read_snapshot = None
                self._read_error = None

    def _snapshot_is_older(self, properties: dict, reference: dict) -> bool:
        if properties.get("appliance_model") != reference.get("appliance_model"):
            return False
        observed = appliance_datetime(properties.get("time"), self.clock_zone)
        previous = appliance_datetime(reference.get("time"), self.clock_zone)
        uptime = properties.get("uptime")
        previous_uptime = reference.get("uptime")
        if (
            observed is None
            or previous is None
            or not isinstance(uptime, str)
            or not isinstance(previous_uptime, str)
        ):
            return False
        uptime = uptime_seconds(uptime)
        previous_uptime = uptime_seconds(previous_uptime)
        # Truncated appliance uptime can have minute precision. Requiring both
        # clocks to regress also allows clock adjustments and ordinary reboots.
        return (
            uptime is not None
            and previous_uptime is not None
            and uptime <= previous_uptime - 60
            and observed <= previous - timedelta(minutes=1)
        )

    def _pending_read_properties(self, reference: dict) -> tuple[dict, set[str], dict, bool]:
        """Replay the updates received during a read over its reference state.

        Returns the properties to publish, the keys whose timing against the
        reference is unknown, the values a replacing snapshot dropped, and whether
        such a snapshot restarted the replay.
        """
        properties = {}
        partial_keys = set()
        dropped = {}
        restarted = False
        for update, complete_snapshot, replaced in self._read_updates or ():
            if update.full:
                if self._snapshot_is_older(update.properties, reference):
                    continue
                if update.properties["appliance_model"] != reference.get("appliance_model"):
                    properties.clear()
                    partial_keys.clear()
                    dropped.clear()
                    reference = {}
                elif replaced:
                    # The replay starts over from a snapshot that replaced state. What it
                    # drops was still observed, so it stays recorded for the conflict
                    # check, and the reference keeps its clocks to order later snapshots.
                    partial_keys.update(properties)
                    dropped.update(properties)
                    properties.clear()
                    restarted = True
            if complete_snapshot:
                partial_keys.difference_update(update.properties)
            else:
                partial_keys.update(update.properties)
            properties.update(update.properties)
            reference = {**reference, **update.properties}
        return properties, partial_keys, dropped, restarted

    def _discard_excluded(self, data: dict) -> dict:
        """Drop the properties the app discards for the appliance type.

        Like the app, a response without a usable type keeps the type already
        known for the same appliance model.
        """
        if (
            appliance_type(data) is None
            and appliance_type(self.data) is not None
            and data.get("appliance_model") == self.data.get("appliance_model")
        ):
            data = {**data, "appliance_type": self.data["appliance_type"]}
        excluded = excluded_properties(data)
        return {key: value for key, value in data.items() if key not in excluded}

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
                        "The appliance did not return a complete state after reconnecting."
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

    @callback
    def apply_update(self, update: StateUpdate) -> bool:
        self.push_stats["snapshots" if update.full else "updates"] += 1
        self.push_stats["last_received"] = dt_util.utcnow().isoformat()
        if update.full and self._snapshot_is_older(update.properties, self.data):
            _LOGGER.debug("Ignoring an older snapshot from %s", self.device["name"])
            return False
        self.unrecognized_keys.update(update.properties.keys() - STATE_KEYS)
        if zone := clock_zone(update.properties.get("time")):
            self.clock_zone = zone
        properties = {key: value for key, value in update.properties.items() if key in STATE_KEYS}
        complete_snapshot = update.full and self._confirm_state(properties, "snapshot")
        # Like a read, the snapshot that ends a push interruption replaces cached
        # state, so values only pushed as partial updates do not outlive it.
        replace = update.full and (
            not self.last_update_success
            or properties.get("appliance_model") != self.data.get("appliance_model")
            or (complete_snapshot and self._transport_error is not None)
        )
        if complete_snapshot:
            self._async_confirm_transport_state()
        if "appliance_type" in properties and appliance_type(properties) is None:
            # Like the app, an unusable type does not replace the known one.
            del properties["appliance_type"]
        if "notifs" in properties:
            properties["notifs"] = notification_records(properties)
        if properties:
            self._read_error = None
            self._channel_error = None
            if self._read_updates is not None:
                self._read_updates.append(
                    (StateUpdate(properties, full=update.full), complete_snapshot, replace)
                )
                if complete_snapshot:
                    self._read_snapshot = properties
        if complete_snapshot:
            self.verification_stats["complete_snapshots"] += 1
        _LOGGER.debug(
            "%s %s: %s",
            self.device["name"],
            "snapshot" if update.full else "update",
            _state_for_log(properties),
        )
        if self.last_update_success or update.full:
            updated = self._discard_excluded(properties if replace else {**self.data, **properties})
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
                    errors.append(error)
            if errors and len(errors) == len(self.coordinators):
                raise errors[0]
        finally:
            self._initial_states.clear()
        for coordinator in self.coordinators.values():
            if (
                (not coordinator.last_update_success or coordinator._transport_error is not None)
                and coordinator._channel_error is None
                and self.client.push_connected
            ):
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
                coordinator._followup_alert = False
                if coordinator._followup_task is not None:
                    coordinator._followup_task.cancel()
                    coordinator._followup_task = None
        interval = 0 if self.entry.pref_disable_polling else status_poll_interval(self.entry)
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

    @callback
    def _async_cancel_state_checks(self) -> None:
        if self._cancel_state_checks is not None:
            self._cancel_state_checks()
            self._cancel_state_checks = None

    @callback
    def _verify_next_appliance(self, _now: datetime) -> None:
        coordinator = next(self._verification_order)
        pending = self._verifications.get(coordinator.device_id)
        if (
            self.entry.pref_disable_polling
            or not status_poll_interval(self.entry)
            or (
                not coordinator.last_update_success
                and (
                    coordinator._channel_error is not None
                    or isinstance(coordinator.last_exception, ConfigEntryAuthFailed | InvalidAuth)
                )
            )
            or pending is not None
            and not pending.done()
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
            if isinstance(error, InvalidAuth):
                coordinator._read_error = error
            coordinator.async_set_update_error(error)
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
            and (not coordinator.last_update_success or coordinator._transport_error is not None)
            and coordinator._channel_error is None
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
                        coordinator._channel_error = update
                        coordinator.async_set_update_error(update)
                    elif isinstance(update, ChannelOpened):
                        coordinator._read_error = None
                        coordinator._channel_error = None
                    else:
                        accepted = coordinator.apply_update(update)
                    if isinstance(update, ApiError) or (
                        isinstance(update, StateUpdate)
                        and update.full
                        and accepted
                        and coordinator._transport_error is None
                    ):
                        if recovery := recoveries.pop(device_id, None):
                            canceled.add(recovery)
                            recovery.add_done_callback(canceled.discard)
                            recovery.cancel()
                        if ready := self._initial_states.get(device_id):
                            ready.set()
                    if (
                        (
                            not coordinator.last_update_success
                            or coordinator._transport_error is not None
                        )
                        and device_id not in self._initial_states
                        and not isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
                        and (
                            isinstance(update, ChannelOpened)
                            or isinstance(update, StateUpdate)
                            and not update.properties.keys().isdisjoint(STATE_KEYS)
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
                retry_after = error.retry_after if isinstance(error, RateLimited) else 0
            except Exception as error:
                _LOGGER.exception("Sub-Zero's notification stream failed unexpectedly")
                self.set_error(error)
                retry_after = 0
            finally:
                pending = {*recoveries.values(), *canceled}
                for recovery in pending:
                    recovery.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                recoveries.clear()
            if time.monotonic() - started >= 120:
                backoff = RECONNECT_DELAY
            await asyncio.sleep(max(backoff, retry_after) + random.uniform(0, 5))
            backoff = min(backoff * 2, MAX_RECONNECT_DELAY)
