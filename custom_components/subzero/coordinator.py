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

from .api import ApiError, RateLimited, StateUpdate, SubZeroClient
from .auth import InvalidAuth
from .const import (
    CONTROL_CONFIRM_TIMEOUT,
    DOMAIN,
    KITCHEN_TIMERS,
    MAX_RECONNECT_DELAY,
    RECONNECT_DELAY,
    STATE_KEYS,
    selected_devices,
)
from .controls import control_matches, is_dishwasher, is_oven, validate_control_properties

_LOGGER = logging.getLogger(__name__)


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
        self.entry = entry
        self.device_id = device_id
        self.device = dict(device)
        self.unrecognized_keys: set[str] = set()
        self._command_lock = asyncio.Lock()

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
        try:
            data = await self.client.state(self.device_id)
        except InvalidAuth as error:
            raise ConfigEntryAuthFailed(str(error)) from error
        except RateLimited as error:
            raise UpdateFailed(str(error), retry_after=error.retry_after) from error
        except ApiError as error:
            raise UpdateFailed(str(error)) from error
        self.unrecognized_keys.update(data.keys() - STATE_KEYS)
        return {key: value for key, value in data.items() if key in STATE_KEYS}

    @callback
    def apply_update(self, update: StateUpdate) -> None:
        self.unrecognized_keys.update(update.properties.keys() - STATE_KEYS)
        if not self.last_update_success and not update.full:
            return
        properties = {key: value for key, value in update.properties.items() if key in STATE_KEYS}
        updated = properties if update.full else {**self.data, **properties}
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

    async def async_setup(self) -> None:
        if not self.coordinators:
            return
        try:
            appliances = await self.client.appliances()
        except InvalidAuth as error:
            raise ConfigEntryAuthFailed(str(error)) from error
        except RateLimited as error:
            raise ConfigEntryNotReady(str(error)) from error
        except ApiError as error:
            _LOGGER.warning(
                "Could not refresh appliance units; using cached units where available: %s", error
            )
        else:
            units = {appliance.id: appliance.temperature_unit for appliance in appliances}
            for coordinator in self.coordinators.values():
                coordinator.device["temperature_unit"] = units.get(coordinator.device_id)
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
        errors = []
        for coordinator in self.coordinators.values():
            try:
                await coordinator.async_config_entry_first_refresh()
            except ConfigEntryNotReady as error:
                coordinator.data = {}
                errors.append(error)
        if errors and len(errors) == len(self.coordinators):
            raise errors[0]

    @callback
    def set_error(self, error: Exception) -> None:
        for coordinator in self.coordinators.values():
            coordinator.async_set_update_error(error)

    async def listen(self) -> None:
        backoff = RECONNECT_DELAY
        while True:
            started = time.monotonic()
            try:
                async for device_id, update in self.client.watch(list(self.coordinators)):
                    coordinator = self.coordinators[device_id]
                    if isinstance(update, ApiError):
                        coordinator.async_set_update_error(update)
                    else:
                        coordinator.apply_update(update)
                raise ApiError("Sub-Zero's notification stream ended.")
            except InvalidAuth as error:
                self.set_error(error)
                self.entry.async_start_reauth(self.hass)
                return
            except ApiError as error:
                self.set_error(error)
                if time.monotonic() - started >= 120:
                    backoff = RECONNECT_DELAY
                delay = (
                    max(backoff, error.retry_after) if isinstance(error, RateLimited) else backoff
                )
            await asyncio.sleep(delay + random.uniform(0, 5))
            backoff = min(backoff * 2, MAX_RECONNECT_DELAY)
