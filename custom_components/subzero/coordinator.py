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
    validate_control_properties,
    wash_cancel_enabled,
)

_LOGGER = logging.getLogger(__name__)
INITIAL_STATE_TIMEOUT = 16
SNAPSHOT_TIMEOUT = 16
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


def status_poll_interval(entry: ConfigEntry) -> int:
    interval = entry.options.get("status_poll_interval", DEFAULT_STATUS_POLL_INTERVAL)
    return interval if interval in STATUS_POLL_INTERVALS else DEFAULT_STATUS_POLL_INTERVAL


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
        self._unsettled = False
        self._state_lock = asyncio.Lock()
        self._read_requests = 0
        self._read_task: asyncio.Task[StateRead] | None = None
        self._read_updates: dict | None = None
        self._read_error: Exception | None = None
        self._channel_error: ApiError | None = None
        self._event_cutoff = dt_util.utcnow()
        self._event_ids: set[tuple[datetime, int, int]] = set()
        self._event_listeners: list[Callable[[dict], None]] = []
        self._history_seen = False
        self._channel_snapshot: asyncio.Future[None] | None = None
        self._renewed = False
        self._followup_task: asyncio.Task | None = None
        self._followup_alert = False
        self._last_followup = 0.0
        self.confirmed_at: datetime | None = None
        self._confirmation = 0
        self.missing_state_keys: dict[str, list[str]] = {"snapshot": [], "read": []}
        self.verification_stats: dict[str, int | str | None] = {
            "checks": 0,
            "complete_snapshots": 0,
            "fallback_reads": 0,
            "connection_renewals": 0,
            "last_success": None,
        }

    async def async_verify_state(
        self, renew_connection: Callable[[SubZeroCoordinator], bool]
    ) -> None:
        """Request a full push snapshot, falling back to a status read."""
        self.verification_stats["checks"] += 1
        confirmation = self._confirmation
        snapshots = self.verification_stats["complete_snapshots"]
        snapshot = self._channel_snapshot = self.hass.loop.create_future()
        opened = False
        silent = False
        try:
            if self.client.push_connected:
                await self.client.open_channel(self.device_id)
                opened = True
                async with asyncio.timeout(SNAPSHOT_TIMEOUT):
                    await snapshot
        except RateLimited:
            # The client also holds off other requests until Retry-After expires.
            return
        except InvalidAuth as error:
            self.async_set_update_error(ConfigEntryAuthFailed(str(error)))
            self.entry.async_start_reauth(self.hass)
            return
        except TimeoutError:
            silent = opened
        except ApiError:
            _LOGGER.debug("No verified push snapshot from %s; reading status", self.device["name"])
        finally:
            answered = snapshot.done() and not snapshot.cancelled()
            snapshot.cancel()
            self._channel_snapshot = None
        if not answered:
            self.verification_stats["fallback_reads"] += 1
            try:
                data = await self._async_update_data()
            except ConfigEntryAuthFailed as error:
                self.async_set_update_error(error)
                self.entry.async_start_reauth(self.hass)
                return
            except Exception as error:
                if isinstance(error, UpdateFailed) and error.retry_after is not None:
                    return
                if not isinstance(error, UpdateFailed):
                    _LOGGER.exception("Sub-Zero state verification failed unexpectedly")
                self.async_set_update_error(error)
                self._read_failed(self)
                return
            self.async_set_updated_data(data)
        if self.last_update_success and self._confirmation > confirmation:
            self.verification_stats["last_success"] = self.confirmed_at.isoformat()
            if (
                silent
                and self.verification_stats["complete_snapshots"] == snapshots
                and not self._renewed
            ):
                # A working status read with no push snapshot warrants one renewal
                # until a full snapshot shows that this appliance is reporting again.
                self._renewed = renew_connection(self)

    @callback
    def _confirm_state(self, properties: dict, source: str) -> bool:
        model = properties.get("appliance_model")
        required = (
            self.data.keys() if model == self.data.get("appliance_model") else {"appliance_model"}
        )
        missing = self.missing_state_keys[source] = sorted(required - properties.keys())
        if missing or not isinstance(model, str) or not model:
            return False
        self.confirmed_at = dt_util.utcnow()
        self._confirmation += 1
        return True

    def _active_conditions(self) -> set[str]:
        return {key for key in FOLLOWUP_KEYS if self.data.get(key) is True}

    @callback
    def async_set_updated_data(self, data: dict) -> None:
        active = self._active_conditions()
        super().async_set_updated_data(data)
        updated = self._active_conditions()
        if updated - active and self.entry.state is ConfigEntryState.LOADED:
            self.async_schedule_followup()
        elif not updated and not self._followup_alert and self._followup_task is not None:
            self._followup_task.cancel()

    @callback
    def async_schedule_followup(self, *, alert: bool = False) -> None:
        """Check a new alert or active condition without extending an existing window."""
        if self.entry.pref_disable_polling or not status_poll_interval(self.entry):
            return
        self._followup_alert |= alert
        if (
            self._followup_task is not None
            and not self._followup_task.done()
            and not self._followup_task.cancelling()
        ):
            return
        self._followup_task = self.entry.async_create_background_task(
            self.hass, self._async_followup(), "Sub-Zero event follow-up"
        )

    async def _async_followup(self) -> None:
        try:
            for delay in FOLLOWUP_DELAYS:
                await asyncio.sleep(max(delay, self._last_followup + 60 - time.monotonic()))
                if self.entry.pref_disable_polling or not status_poll_interval(self.entry):
                    return
                self._followup_alert = False
                self._last_followup = time.monotonic()
                result = await self._async_read_result()
                if result.error is not None or (
                    not self._active_conditions() and not self._followup_alert
                ):
                    return
        finally:
            if self._followup_task is asyncio.current_task():
                self._followup_task = None

    @callback
    def async_add_event_listener(self, listener: Callable[[dict], None]) -> Callable[[], None]:
        self._event_listeners.append(listener)
        return lambda: self._event_listeners.remove(listener)

    @callback
    def _process_events(self, properties: dict, *, history: bool) -> None:
        # The first snapshot or status read only sets the baseline, so an
        # appliance clock running ahead cannot replay events from before loading.
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
            instant, sequence, code = identity
            if instant < self._event_cutoff or identity in self._event_ids:
                continue
            self._event_ids.add(identity)
            if len(self._event_ids) > MAX_EVENT_HISTORY:
                oldest = min(self._event_ids)
                self._event_ids.remove(oldest)
                self._event_cutoff = max(self._event_cutoff, oldest[0] + timedelta(microseconds=1))
            if not deliver:
                continue
            event = {
                "code": code,
                "sequence": sequence,
                "appliance_timestamp": events[identity].isoformat(),
            }
            for listener in self._event_listeners:
                listener(event)
            self.async_schedule_followup(alert=True)

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
            try:
                yield
            except asyncio.CancelledError:
                self._unsettled = True
                raise

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
        if result.confirmed_at is None:
            raise HomeAssistantError("The appliance did not return a complete state snapshot.")
        return result.confirmed_at

    async def _async_read_result(self) -> StateRead:
        self._read_requests += 1
        request = self._read_requests
        while True:
            task = self._read_task
            if task is None or task.done() and task.result().request < request:
                task = self._read_task = self.entry.async_create_background_task(
                    self.hass, self._async_read_state(), "Sub-Zero state read"
                )
                task.add_done_callback(self._async_read_finished)
            result = await asyncio.shield(task)
            if result.request >= request:
                return result

    async def _async_read_state(self) -> StateRead:
        request = self._read_requests
        confirmation = self._confirmation
        try:
            data = await self._async_fetch_state()
        except Exception as error:
            if not isinstance(error, UpdateFailed) or error.retry_after is None:
                self.async_set_update_error(error)
            return StateRead(request, None, error)
        self.async_set_updated_data(data)
        if "notifs" in data:
            self._process_events(data, history=True)
        confirmed = self.confirmed_at if self._confirmation > confirmation else None
        return StateRead(request, confirmed)

    @callback
    def _async_read_finished(self, task: asyncio.Task[StateRead]) -> None:
        if task.cancelled() or (error := task.result().error) is None:
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
        if result.error is not None:
            raise result.error
        return self.data

    async def _async_fetch_state(self) -> dict:
        async with self._state_lock:
            self._read_updates = {}
            self._read_error = None
            try:
                received = True
                try:
                    data = await self.client.state(self.device_id)
                except ApiError:
                    model = self._read_updates.get("appliance_model")
                    if not isinstance(model, str) or not model:
                        raise
                    data = self.data
                    received = False
                if error := self._read_error:
                    raise error
                model = self._read_updates.get("appliance_model")
                if received and (not model or model == data.get("appliance_model")):
                    self._confirm_state(data, "read")
                self.unrecognized_keys.update(data.keys() - STATE_KEYS)
                if "notifs" in data:
                    data = {**data, "notifs": notification_records(data)}
                model = self._read_updates.get("appliance_model")
                if isinstance(model, str) and model and model != data.get("appliance_model"):
                    data = {}
                data = {
                    **{key: value for key, value in data.items() if key in STATE_KEYS},
                    **self._read_updates,
                }
                return self._discard_excluded(data)
            except InvalidAuth as error:
                raise ConfigEntryAuthFailed(str(error)) from error
            except RateLimited as error:
                raise UpdateFailed(str(error), retry_after=error.retry_after) from error
            except ApiError as error:
                raise UpdateFailed(str(error)) from error
            finally:
                self._read_updates = None
                self._read_error = None

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
        while not self.last_update_success:
            _LOGGER.debug("Refreshing %s to recover appliance state", self.device["name"])
            try:
                data = await self._async_update_data()
            except ConfigEntryAuthFailed as error:
                self.async_set_update_error(error)
                self.entry.async_start_reauth(self.hass)
                return
            except Exception as error:
                if self.last_update_success:
                    return
                if not isinstance(error, UpdateFailed):
                    _LOGGER.exception("Sub-Zero state recovery failed unexpectedly")
                self.async_set_update_error(error)
                retry_after = (error.retry_after or 0) if isinstance(error, UpdateFailed) else 0
            else:
                if not self.last_update_success:
                    self.async_set_updated_data(data)
                return
            await asyncio.sleep(max(backoff, retry_after) + random.uniform(0, 5))
            backoff = min(backoff * 2, MAX_RECONNECT_DELAY)

    @callback
    def apply_update(self, update: StateUpdate) -> None:
        self.unrecognized_keys.update(update.properties.keys() - STATE_KEYS)
        if zone := clock_zone(update.properties.get("time")):
            self.clock_zone = zone
        properties = {key: value for key, value in update.properties.items() if key in STATE_KEYS}
        complete_snapshot = update.full and self._confirm_state(properties, "snapshot")
        if "appliance_type" in properties and appliance_type(properties) is None:
            # Like the app, an unusable type does not replace the known one.
            del properties["appliance_type"]
        if "notifs" in properties:
            properties["notifs"] = notification_records(properties)
        if properties:
            self._read_error = None
            self._channel_error = None
            if self._read_updates is not None:
                if update.full and properties["appliance_model"] != self.data.get(
                    "appliance_model"
                ):
                    self._read_updates.clear()
                self._read_updates.update(properties)
        self.push_stats["snapshots" if update.full else "updates"] += 1
        self.push_stats["last_received"] = dt_util.utcnow().isoformat()
        if complete_snapshot:
            self.verification_stats["complete_snapshots"] += 1
            self._renewed = False
            if self._channel_snapshot is not None and not self._channel_snapshot.done():
                self._channel_snapshot.set_result(None)
        _LOGGER.debug(
            "%s %s: %s",
            self.device["name"],
            "snapshot" if update.full else "update",
            {
                key: value
                for key, value in properties.items()
                if key not in PRIVATE_KEYS and not isinstance(value, dict | list)
            },
        )
        if self.last_update_success or update.full:
            replace = update.full and (
                not self.last_update_success
                or properties.get("appliance_model") != self.data.get("appliance_model")
            )
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
        if "notifs" in properties:
            self._process_events(properties, history=update.full)


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
        self._last_renewal: float | None = None

    async def async_setup(self) -> None:
        if not self.coordinators:
            return
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
            if devices != selected_devices(self.entry):
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
                not coordinator.last_update_success
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
        if interval := status_poll_interval(self.entry):
            self.entry.async_on_unload(
                async_track_time_interval(
                    self.hass,
                    self._verify_next_appliance,
                    timedelta(seconds=interval / len(self.coordinators)),
                    cancel_on_shutdown=True,
                )
            )

    @callback
    def _verify_next_appliance(self, _now: datetime) -> None:
        coordinator = next(self._verification_order)
        pending = self._verifications.get(coordinator.device_id)
        if (
            self.entry.pref_disable_polling
            or isinstance(coordinator.last_exception, ConfigEntryAuthFailed | InvalidAuth)
            or pending is not None
            and not pending.done()
        ):
            return
        self._verifications[coordinator.device_id] = self.entry.async_create_background_task(
            self.hass,
            coordinator.async_verify_state(self._renew_connection),
            "Sub-Zero state verification",
        )

    @callback
    def _renew_connection(self, coordinator: SubZeroCoordinator) -> bool:
        now = time.monotonic()
        if not self.client.push_connected or (
            self._last_renewal is not None
            and now - self._last_renewal < status_poll_interval(self.entry)
        ):
            return False
        self._last_renewal = now
        coordinator.verification_stats["connection_renewals"] += 1
        self.client.renew_connection()
        return True

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
            and not coordinator.last_update_success
            and not isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
        ):
            self._start_recovery(coordinator)

    async def listen(self) -> None:
        backoff = RECONNECT_DELAY
        while True:
            started = time.monotonic()
            recoveries = self._recoveries
            canceled: set[asyncio.Task] = set()
            try:
                async for device_id, update in self.client.watch(list(self.coordinators)):
                    coordinator = self.coordinators[device_id]
                    if isinstance(update, ApiError) or (
                        isinstance(update, StateUpdate) and update.full
                    ):
                        if recovery := recoveries.pop(device_id, None):
                            canceled.add(recovery)
                            recovery.add_done_callback(canceled.discard)
                            recovery.cancel()
                        if ready := self._initial_states.get(device_id):
                            ready.set()
                    if isinstance(update, ApiError):
                        coordinator._channel_error = update
                        coordinator.async_set_update_error(update)
                    elif isinstance(update, ChannelOpened):
                        coordinator._read_error = None
                        coordinator._channel_error = None
                    else:
                        coordinator.apply_update(update)
                    if (
                        not coordinator.last_update_success
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
                self.set_error(error)
                retry_after = error.retry_after if isinstance(error, RateLimited) else 0
            except Exception as error:
                _LOGGER.exception("Sub-Zero's notification stream failed unexpectedly")
                self.set_error(error)
                retry_after = 0
            finally:
                if canceled:
                    await asyncio.gather(*canceled, return_exceptions=True)
            if time.monotonic() - started >= 120:
                backoff = RECONNECT_DELAY
            await asyncio.sleep(max(backoff, retry_after) + random.uniform(0, 5))
            backoff = min(backoff * 2, MAX_RECONNECT_DELAY)
