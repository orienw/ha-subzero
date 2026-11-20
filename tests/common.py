"""Home Assistant's test helpers, from the package that matches its version."""

from homeassistant.config_entries import ConfigEntryState

from custom_components.subzero.api import ChannelOpened
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
