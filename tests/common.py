"""Home Assistant's test helpers, from the package that matches its version."""

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er

from custom_components.subzero.api import ApiError, ChannelOpened
from custom_components.subzero.const import DOMAIN

try:
    from pytest_homeassistant.common import (
        MockConfigEntry,
        async_capture_events,
        async_fire_time_changed,
    )
except ImportError:  # Home Assistant before 2026.11
    from pytest_homeassistant_custom_component.common import (
        MockConfigEntry,
        async_capture_events,
        async_fire_time_changed,
    )

__all__ = [
    "MockConfigEntry",
    "async_capture_events",
    "async_fire_time_changed",
    "offline_entities_still_available",
    "setup_channel_events",
]


def setup_channel_events(hass, device_ids) -> list[tuple[str, ChannelOpened]]:
    """The channels the client opens while an entry sets up.

    Tests drive the channels of later connections themselves.
    """
    if any(
        entry.state is ConfigEntryState.SETUP_IN_PROGRESS
        for entry in hass.config_entries.async_entries(DOMAIN)
    ):
        return [(device_id, ChannelOpened()) for device_id in device_ids]
    return []


async def offline_entities_still_available(hass, entry) -> list[str]:
    """Take every appliance offline and return the entities that do not go unavailable.

    The event entity and the Active faults sensor do not follow the appliance.
    """
    for coordinator in entry.runtime_data.coordinators.values():
        coordinator.async_set_update_error(ApiError("Offline"))
    await hass.async_block_till_done()
    entities = [
        entity
        for entity in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if entity.domain != "event" and not entity.unique_id.endswith("_active_faults")
    ]
    states = {entity.entity_id: hass.states.get(entity.entity_id) for entity in entities}
    assert sum(state is not None for state in states.values()) > 5
    return sorted(
        entity_id
        for entity_id, state in states.items()
        if state is not None and state.state != "unavailable"
    )
