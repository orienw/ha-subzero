"""Download appliance capabilities without account or network identifiers."""

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from . import SubZeroConfigEntry
from .api import fault_record
from .const import DOMAIN, PRIVATE_KEYS
from .coordinator import SubZeroCoordinator, SubZeroFaultsCoordinator


def appliance_diagnostics(
    coordinator: SubZeroCoordinator, faults: SubZeroFaultsCoordinator
) -> dict:
    records = []
    for fault in faults.data or []:
        item = fault_record(fault)
        item["active"] = fault.active
        records.append(item)
    return {
        "available": coordinator.last_update_success,
        "temperature_unit": coordinator.device.get("temperature_unit"),
        # The zone the appliance's times without an offset are read in; None means
        # Home Assistant's, until a push reports the appliance clock.
        "clock_zone": str(coordinator.clock_zone) if coordinator.clock_zone else None,
        "push": {
            **coordinator.push_stats,
            "last_channel_message": coordinator.client.last_messages.get(coordinator.device_id),
        },
        "state_verification": dict(coordinator.verification_stats),
        "confirmed_at": coordinator.confirmed_at.isoformat() if coordinator.confirmed_at else None,
        "missing_state_keys": dict(coordinator.missing_state_keys),
        "unrecognized_state_keys": sorted(coordinator.unrecognized_keys),
        "state": async_redact_data(coordinator.data, PRIVATE_KEYS),
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
        "firebase": (
            {
                "connected": account.alerts.push is not None and account.alerts.push.is_started(),
                "subscriptions_healthy": account.alerts.subscription_healthy,
                "received": account.alerts.received,
                "last_received": account.alerts.last_received,
                "last_error": account.alerts.last_error,
                "last_sync": account.alerts.last_sync,
            }
            if account.alerts is not None
            else None
        ),
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
