"""Ice-maker delay scheduling."""

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    async def schedule_ice_delay(call: ServiceCall) -> None:
        if type(call.data["duration"]) is not int or type(call.data["start_in"]) is not int:
            raise ServiceValidationError("Enter a whole number of hours and minutes.")
        device = dr.async_get(hass).async_get(call.data["device_id"])
        if device is not None:
            for entry_id in device.config_entries:
                entry = hass.config_entries.async_get_entry(entry_id)
                if (
                    entry is None
                    or entry.domain != DOMAIN
                    or entry.state is not ConfigEntryState.LOADED
                ):
                    continue
                for device_id, coordinator in entry.runtime_data.coordinators.items():
                    if (DOMAIN, device_id) in device.identifiers:
                        await coordinator.async_set_ice_delay(
                            call.data["duration"] * 3600,
                            call.data["start_in"] * 60,
                            call.data["repeat"],
                        )
                        return
        raise ServiceValidationError("Select a connected Sub-Zero ice maker.")

    hass.services.async_register(
        DOMAIN,
        "schedule_ice_delay",
        schedule_ice_delay,
        schema=vol.Schema(
            {
                vol.Required("device_id"): cv.string,
                vol.Required("duration"): vol.All(int, vol.Range(min=1, max=12)),
                vol.Optional("start_in", default=0): vol.All(int, vol.Range(min=0, max=1439)),
                vol.Optional("repeat", default=False): cv.boolean,
            }
        ),
    )
