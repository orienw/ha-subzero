"""Fridge air purification control."""

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SubZeroConfigEntry
from .controls import is_fridge
from .entity import SubZeroEntity

DESCRIPTION = SwitchEntityDescription(
    key="air_filter_on", name="Air purification", icon="mdi:air-filter"
)


async def async_setup_entry(
    hass: HomeAssistant, entry: SubZeroConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    discovered: set[str] = set()

    @callback
    def discover_entities() -> None:
        entities = []
        for device_id, coordinator in entry.runtime_data.coordinators.items():
            if (
                device_id not in discovered
                and is_fridge(coordinator.data)
                and DESCRIPTION.key in coordinator.data
            ):
                discovered.add(device_id)
                entities.append(SubZeroSwitch(coordinator, DESCRIPTION))
        async_add_entities(entities)

    discover_entities()
    for coordinator in entry.runtime_data.coordinators.values():
        entry.async_on_unload(coordinator.async_add_listener(discover_entities))


class SubZeroSwitch(SubZeroEntity, SwitchEntity):
    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.data.get(self.entity_description.key)
        return value if type(value) is bool else None

    @property
    def available(self) -> bool:
        return super().available and is_fridge(self.coordinator.data) and self.is_on is not None

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_properties({self.entity_description.key: True})

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_properties({self.entity_description.key: False})
