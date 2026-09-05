"""Sub-Zero refrigerator cloud integration."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SubZeroClient
from .app_config import SUBSCRIPTION_KEY
from .coordinator import SubZeroCoordinator

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]
type SubZeroConfigEntry = ConfigEntry[SubZeroCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: SubZeroConfigEntry) -> bool:
    async def save_tokens(tokens: dict) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, "tokens": tokens})

    client = SubZeroClient(
        async_get_clientsession(hass), SUBSCRIPTION_KEY, entry.data["tokens"], save_tokens
    )
    coordinator = SubZeroCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_create_background_task(hass, coordinator.listen(), "Sub-Zero notifications")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SubZeroConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
