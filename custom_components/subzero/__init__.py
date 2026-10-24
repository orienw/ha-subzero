"""Sub-Zero, Wolf, and Cove appliance cloud integration."""

import asyncio
import logging
from functools import partial

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ApiError, SubZeroClient
from .app_config import SUBSCRIPTION_KEY
from .auth import InvalidAuth
from .const import DISHWASHER_SWITCHES, DOMAIN
from .controls import excluded_entity_keys
from .coordinator import (
    SubZeroAccount,
    SubZeroCoordinator,
    firebase_alerts_enabled,
    selected_devices,
)
from .fcm import FcmAlerts, async_remove_alert_subscriptions
from .services import async_setup_services

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.CLIMATE,
    Platform.BUTTON,
    Platform.FAN,
    Platform.LIGHT,
    Platform.EVENT,
]
type SubZeroConfigEntry = ConfigEntry[SubZeroAccount]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SubZeroConfigEntry) -> bool:
    async def save_tokens(tokens: dict) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, "tokens": tokens})

    try:
        client = SubZeroClient(
            async_get_clientsession(hass), SUBSCRIPTION_KEY, entry.data["tokens"], save_tokens
        )
    except InvalidAuth as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    account = SubZeroAccount(hass, entry, client)
    await account.async_setup()
    entry.runtime_data = account
    registry = dr.async_get(hass)

    @callback
    def update_device(coordinator: SubZeroCoordinator) -> None:
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            **coordinator.device_info,
        )

    for coordinator in account.coordinators.values():
        update_device(coordinator)
        entry.async_on_unload(coordinator.async_add_listener(partial(update_device, coordinator)))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    identifiers = {(DOMAIN, device_id) for device_id in account.coordinators}
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if not device.identifiers.intersection(identifiers):
            registry.async_remove_device(device.id)
    # Entities left from earlier versions for properties the app discards.
    excluded = {
        f"{coordinator.device_id}_{key}"
        for coordinator in account.coordinators.values()
        for key in excluded_entity_keys(coordinator.data)
    }
    entities = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(entities, entry.entry_id):
        if entity.unique_id in excluded:
            entities.async_remove(entity.entity_id)
    devices = selected_devices(entry)
    firebase_enabled = firebase_alerts_enabled(entry)

    async def update_options(hass: HomeAssistant, entry: SubZeroConfigEntry) -> None:
        nonlocal firebase_enabled
        async with account.options_lock:
            if entry.state is not ConfigEntryState.LOADED or entry.runtime_data is not account:
                return
            if selected_devices(entry) != devices:
                hass.config_entries.async_schedule_reload(entry.entry_id)
                return
            account.async_update_state_checks()
            if firebase_alerts_enabled(entry) == firebase_enabled:
                return
            if account.alerts is not None:
                await account.alerts.stop(graceful=True)
            if entry.state is not ConfigEntryState.LOADED or entry.runtime_data is not account:
                return
            firebase_enabled = firebase_alerts_enabled(entry)
            _async_start_alerts(hass, entry)

    entry.async_on_unload(entry.add_update_listener(update_options))
    _async_start_alerts(hass, entry)
    return True


@callback
def _async_start_alerts(hass: HomeAssistant, entry: SubZeroConfigEntry) -> None:
    account = entry.runtime_data
    account.alerts = None
    if (firebase_alerts_enabled(entry) and account.coordinators) or entry.data.get(
        "fcm_registration_ids"
    ):
        account.alerts = FcmAlerts(hass, entry, account.client, account.coordinators)
        if firebase_alerts_enabled(entry) and account.coordinators:
            account.alerts.start()
        else:
            account.alerts.start_cleanup()


async def async_unload_entry(hass: HomeAssistant, entry: SubZeroConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        account = entry.runtime_data
        async with account.options_lock:
            if account.alerts is not None:
                await account.alerts.stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove only this installation's saved appliance alert subscriptions."""
    psid = entry.data.get("fcm_psid")
    device_ids = entry.data.get("fcm_registration_ids") or {}
    if not isinstance(psid, str) or not psid or not device_ids:
        return

    def failed(device_id: str, error: ApiError) -> None:
        _LOGGER.warning("Could not remove appliance alert subscriptions: %s", error)

    try:
        client = SubZeroClient(
            async_get_clientsession(hass), SUBSCRIPTION_KEY, entry.data["tokens"]
        )
        async with asyncio.timeout(30):
            await async_remove_alert_subscriptions(client, psid, device_ids, failed=failed)
    except InvalidAuth, TimeoutError:
        _LOGGER.warning("Could not remove appliance alert subscriptions before entry removal")


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.version > 2:
        return False
    if entry.version == 1:
        devices = selected_devices(entry)
        previously_disabled = {
            f"{device_id}_{key}"
            for device_id in devices
            for key in (
                "night_ice_on",
                "sabbath_on",
                "high_use_on",
                "short_vacation_on",
                "long_vacation_on",
                "ap_rssi",
            )
        }
        registry = er.async_get(hass)
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
            if (
                entity.unique_id in previously_disabled
                and entity.disabled_by is er.RegistryEntryDisabler.INTEGRATION
            ):
                registry.async_update_entity(entity.entity_id, disabled_by=None)
        hass.config_entries.async_update_entry(
            entry,
            data={"tokens": entry.data["tokens"], "devices": devices},
            version=2,
        )
    if entry.minor_version < 2:
        removed_binary_ids = {
            f"{device_id}_{key}"
            for device_id in selected_devices(entry)
            for key in {"air_filter_on", "cav_light_on", "cav2_light_on", *DISHWASHER_SWITCHES}
        }
        removed_sensor_ids = {
            f"{device_id}_connection_mode" for device_id in selected_devices(entry)
        }
        registry = er.async_get(hass)
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
            if (
                entity.domain == "binary_sensor"
                and entity.unique_id in removed_binary_ids
                or entity.domain == "sensor"
                and entity.unique_id in removed_sensor_ids
            ):
                registry.async_remove(entity.entity_id)
        hass.config_entries.async_update_entry(entry, minor_version=2)
    if entry.minor_version < 3:
        removed_number_ids = {
            f"{device_id}_accent_light_level" for device_id in selected_devices(entry)
        }
        registry = er.async_get(hass)
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
            if entity.domain == "number" and entity.unique_id in removed_number_ids:
                registry.async_remove(entity.entity_id)
        hass.config_entries.async_update_entry(entry, minor_version=3)
    return True
