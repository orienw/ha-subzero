"""Appliance events from live notifications and snapshot history."""

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SubZeroConfigEntry
from .const import NOTIFICATION_TYPES
from .entity import SubZeroEntity, async_setup_entities


async def async_setup_entry(
    hass: HomeAssistant, entry: SubZeroConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_entities(
        entry,
        async_add_entities,
        (
            EventEntityDescription(
                key="appliance_event",
                name="Appliance event",
                event_types=list(NOTIFICATION_TYPES.values()),
            ),
        ),
        SubZeroEvent,
        lambda coordinator, description: True,
    )


class SubZeroEvent(SubZeroEntity, EventEntity):
    @property
    def available(self) -> bool:
        return True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_event_listener(self._handle_event))

    @callback
    def _handle_event(self, event: dict) -> None:
        self._trigger_event(NOTIFICATION_TYPES.get(event["code"], "unknown"), event)
        self.async_write_ha_state()
