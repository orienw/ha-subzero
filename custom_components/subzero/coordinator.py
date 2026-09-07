"""Push updates with bounded reconnect attempts."""

import asyncio
import logging
import random
import time
from datetime import datetime

from homeassistant.config_entries import ConfigEntry
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

from .api import ApiError, ChannelOpened, RateLimited, StateUpdate, SubZeroClient
from .auth import InvalidAuth
from .const import (
    CONTROL_CONFIRM_TIMEOUT,
    DOMAIN,
    KITCHEN_TIMERS,
    MAX_RECONNECT_DELAY,
    NETWORK_KEYS,
    RECONNECT_DELAY,
    STATE_KEYS,
)
from .controls import control_matches, is_dishwasher, is_oven, validate_control_properties

_LOGGER = logging.getLogger(__name__)
INITIAL_STATE_TIMEOUT = 16


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
    ):
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
        )
        self.client = client
        self.data = {}
        self.entry = entry
        self.device_id = device_id
        self.device = dict(device)
        self.unrecognized_keys: set[str] = set()
        self.push_stats: dict[str, int | str | None] = {
            "snapshots": 0,
            "updates": 0,
            "last_received": None,
        }
        self._command_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._read_updates: dict | None = None
        self._read_error: Exception | None = None
        self._channel_error: ApiError | None = None

    @property
    def device_info(self) -> DeviceInfo:
        version = self.data.get("version")
        return DeviceInfo(
            identifiers={(DOMAIN, self.device_id)},
            name=self.device["name"],
            manufacturer=(
                "Cove" if is_dishwasher(self.data) else "Wolf" if is_oven(self.data) else "Sub-Zero"
            ),
            model=self.data.get("appliance_model"),
            sw_version=version.get("fw") if isinstance(version, dict) else None,
        )

    async def async_set_properties(self, properties: dict) -> None:
        """Serialize writes and confirm their result from appliance state."""
        properties = dict(properties)
        async with self._command_lock:
            if not self.last_update_success:
                raise ServiceValidationError("The appliance is unavailable.")
            validate_control_properties(self.data, self.device.get("temperature_unit"), properties)
            requested_at = {}
            try:
                for key, value in properties.items():
                    if not self.last_update_success:
                        raise ServiceValidationError("The appliance is unavailable.")
                    validate_control_properties(
                        self.data, self.device.get("temperature_unit"), {key: value}
                    )
                    requested_at[key] = dt_util.utcnow()
                    if key in KITCHEN_TIMERS or not control_matches(
                        self.data, key, value, requested_at[key]
                    ):
                        await self._async_set_property(key, value, requested_at[key])
                if not self.last_update_success or any(
                    not control_matches(self.data, key, value, requested_at[key])
                    for key, value in properties.items()
                ):
                    raise HomeAssistantError("The appliance did not confirm the requested setting.")
            except InvalidAuth as error:
                self.entry.async_start_reauth(self.hass)
                raise HomeAssistantError("Sign in to Sub-Zero again to change settings.") from error
            except ApiError as error:
                raise HomeAssistantError(str(error)) from error

    async def _async_set_property(
        self, key: str, value: bool | int, requested_at: datetime
    ) -> None:
        confirmed = asyncio.Event()

        @callback
        def confirm() -> None:
            if self.last_update_success and control_matches(self.data, key, value, requested_at):
                confirmed.set()
            else:
                confirmed.clear()

        remove_listener = self.async_add_listener(confirm)
        try:
            await self.client.set_property(self.device_id, key, value)
            try:
                await asyncio.wait_for(confirmed.wait(), CONTROL_CONFIRM_TIMEOUT)
            except TimeoutError:
                await self.async_refresh()
            confirm()
            if not confirmed.is_set():
                raise HomeAssistantError("The appliance did not confirm the requested setting.")
        finally:
            remove_listener()

    async def _async_update_data(self) -> dict:
        async with self._state_lock:
            self._read_updates = {}
            self._read_error = None
            try:
                try:
                    data = await self.client.state(self.device_id)
                except ApiError:
                    model = self._read_updates.get("appliance_model")
                    if not isinstance(model, str) or not model:
                        raise
                    data = self.data
                if error := self._read_error or self._channel_error:
                    raise error
                self.unrecognized_keys.update(data.keys() - STATE_KEYS)
                model = self._read_updates.get("appliance_model")
                if isinstance(model, str) and model and model != data.get("appliance_model"):
                    data = {}
                return {
                    **{key: value for key, value in data.items() if key in STATE_KEYS},
                    **self._read_updates,
                }
            except InvalidAuth as error:
                raise ConfigEntryAuthFailed(str(error)) from error
            except RateLimited as error:
                raise UpdateFailed(str(error), retry_after=error.retry_after) from error
            except ApiError as error:
                raise UpdateFailed(str(error)) from error
            finally:
                self._read_updates = None
                self._read_error = None

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
        properties = {key: value for key, value in update.properties.items() if key in STATE_KEYS}
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
        _LOGGER.debug(
            "%s %s: %s",
            self.device["name"],
            "snapshot" if update.full else "update",
            {
                key: value
                for key, value in properties.items()
                if key not in NETWORK_KEYS and not isinstance(value, dict | list)
            },
        )
        if not self.last_update_success and not update.full:
            return
        replace = update.full and (
            not self.last_update_success
            or properties.get("appliance_model") != self.data.get("appliance_model")
        )
        updated = properties if replace else {**self.data, **properties}
        if (
            update.full
            or updated != self.data
            or any(type(value) is not type(self.data.get(key)) for key, value in updated.items())
            or not self.last_update_success
        ):
            self.async_set_updated_data(updated)


class SubZeroAccount:
    """Share account tokens and one notification stream across selected appliances."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: SubZeroClient):
        self.hass = hass
        self.entry = entry
        self.client = client
        self.coordinators = {
            device_id: SubZeroCoordinator(hass, entry, client, device_id, device)
            for device_id, device in selected_devices(entry).items()
        }
        self._initial_states = {device_id: asyncio.Event() for device_id in self.coordinators}
        self._recoveries: dict[str, asyncio.Task] = {}

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

    @callback
    def set_error(self, error: Exception) -> None:
        for coordinator in self.coordinators.values():
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
                        coordinator._read_error = update
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
                return
            except ApiError as error:
                self.set_error(error)
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
