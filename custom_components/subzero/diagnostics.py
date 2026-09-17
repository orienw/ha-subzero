"""Download appliance capabilities without account or network identifiers."""

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from . import SubZeroConfigEntry
from .const import DOMAIN, FAULT_SEVERITIES, NETWORK_KEYS
from .coordinator import SubZeroCoordinator, SubZeroFaultsCoordinator


def appliance_diagnostics(
    coordinator: SubZeroCoordinator, faults: SubZeroFaultsCoordinator
) -> dict:
    records = []
    for fault in faults.data or []:
        item = {}
        if fault.code is not None:
            item["code"] = fault.code
        item["severity"] = FAULT_SEVERITIES.get(fault.severity, "unknown")
        item["active"] = fault.active
        item["created"] = fault.created
        if fault.description is not None:
            item["description"] = fault.description
        records.append(item)
    return {
        "available": coordinator.last_update_success,
        "temperature_unit": coordinator.device.get("temperature_unit"),
        "push": dict(coordinator.push_stats),
        "unrecognized_state_keys": sorted(coordinator.unrecognized_keys),
        "state": async_redact_data(coordinator.data, NETWORK_KEYS),
        "faults": records,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SubZeroConfigEntry
) -> dict:
    account = entry.runtime_data
    return {
        "connection": "cloud_push",
        "push_connected": account.client.push_connected,
        "notifications": dict(account.client.notification_stats),
        "appliances": [
            appliance_diagnostics(coordinator, account.fault_coordinators[coordinator.device_id])
            for coordinator in account.coordinators.values()
        ],
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: SubZeroConfigEntry, device: DeviceEntry
) -> dict:
    account = entry.runtime_data
    return next(
        (
            appliance_diagnostics(coordinator, account.fault_coordinators[device_id])
            for device_id, coordinator in account.coordinators.items()
            if (DOMAIN, device_id) in device.identifiers
        ),
        {},
    )
