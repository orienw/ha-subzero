"""Download appliance capabilities without account or network identifiers."""

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from . import SubZeroConfigEntry
from .const import DOMAIN
from .coordinator import SubZeroCoordinator


def appliance_diagnostics(coordinator: SubZeroCoordinator) -> dict:
    return {
        "available": coordinator.last_update_success,
        "temperature_unit": coordinator.device.get("temperature_unit"),
        "state": async_redact_data(coordinator.data, {"ipv4_addr", "device_wlan_id"}),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SubZeroConfigEntry
) -> dict:
    return {
        "connection": "cloud_push",
        "push_connected": entry.runtime_data.client.push_connected,
        "appliances": [
            appliance_diagnostics(coordinator)
            for coordinator in entry.runtime_data.coordinators.values()
        ],
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: SubZeroConfigEntry, device: DeviceEntry
) -> dict:
    return next(
        (
            appliance_diagnostics(coordinator)
            for device_id, coordinator in entry.runtime_data.coordinators.items()
            if (DOMAIN, device_id) in device.identifiers
        ),
        {},
    )
