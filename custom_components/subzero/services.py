"""Appliance state verification and ice-maker delay scheduling."""

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN


def device_coordinators(hass: HomeAssistant, registry_id: str) -> list:
    device = dr.async_get(hass).async_get(registry_id)
    identifiers = device.identifiers if device is not None else set()
    return sorted(
        (
            coordinator
            for entry in hass.config_entries.async_loaded_entries(DOMAIN)
            for device_id, coordinator in entry.runtime_data.coordinators.items()
            if (DOMAIN, device_id) in identifiers
        ),
        key=lambda coordinator: coordinator.entry.entry_id != device.config_entry_id,
    )


def _whole_number(value) -> int:
    """Accept whole numbers from templates and the API, but not booleans."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise vol.Invalid("expected a whole number")
    number = float(value)
    if not number.is_integer():
        raise vol.Invalid("expected a whole number")
    return int(number)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    async def read_status(call: ServiceCall) -> ServiceResponse:
        coordinators = device_coordinators(hass, call.data["device_id"])
        if not coordinators:
            raise ServiceValidationError("Select an appliance from a loaded Sub-Zero integration.")
        result = await coordinators[0].async_read_status()
        return {
            "confirmed_at": result.confirmed_at.isoformat(),
            "missing_keys": list(result.missing_keys),
        }

    hass.services.async_register(
        DOMAIN,
        "read_status",
        read_status,
        schema=vol.Schema({vol.Required("device_id"): cv.string}),
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def schedule_ice_delay(call: ServiceCall) -> None:
        # A device id from before Home Assistant 2026.8 can stand for one appliance
        # shared by several accounts, so look in every loaded account, starting with
        # the device's own.
        coordinators = device_coordinators(hass, call.data["device_id"])
        if not coordinators:
            raise ServiceValidationError("Select a connected Sub-Zero ice maker.")
        error = None
        for coordinator in coordinators:
            try:
                await coordinator.async_set_ice_delay(
                    call.data["duration"] * 3600, call.data["start_in"] * 60, call.data["repeat"]
                )
            except ServiceValidationError as refused:
                # Refused before sending, so another account may still reach the appliance.
                error = error or refused
            else:
                return
        raise error

    hass.services.async_register(
        DOMAIN,
        "schedule_ice_delay",
        schedule_ice_delay,
        schema=vol.Schema(
            {
                vol.Required("device_id"): cv.string,
                vol.Required("duration"): vol.All(_whole_number, vol.Range(min=1, max=12)),
                vol.Optional("start_in", default=0): vol.All(
                    _whole_number, vol.Range(min=0, max=1439)
                ),
                vol.Optional("repeat", default=False): cv.boolean,
            }
        ),
    )
