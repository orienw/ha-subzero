"""Home Assistant's test helpers, from the package that matches its version."""

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

__all__ = ["MockConfigEntry", "async_capture_events", "async_fire_time_changed"]
