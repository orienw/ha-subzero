"""Sub-Zero, Wolf, and Cove appliance cloud integration."""

from functools import partial

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SubZeroClient
from .app_config import SUBSCRIPTION_KEY
from .const import DISHWASHER_SWITCHES, DOMAIN, selected_devices
from .coordinator import SubZeroAccount, SubZeroCoordinator

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.CLIMATE,
    Platform.BUTTON,
]
type SubZeroConfigEntry = ConfigEntry[SubZeroAccount]


async def async_setup_entry(hass: HomeAssistant, entry: SubZeroConfigEntry) -> bool:
    async def save_tokens(tokens: dict) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, "tokens": tokens})

    client = SubZeroClient(
        async_get_clientsession(hass), SUBSCRIPTION_KEY, entry.data["tokens"], save_tokens
    )
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
    if account.coordinators:
        entry.async_create_background_task(hass, account.listen(), "Sub-Zero notifications")
    identifiers = {(DOMAIN, device_id) for device_id in account.coordinators}
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if not device.identifiers.intersection(identifiers):
            registry.async_remove_device(device.id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SubZeroConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


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
    return True
