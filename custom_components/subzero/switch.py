"""Air purification, oven lights, and dishwasher options."""

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SubZeroConfigEntry
from .const import DISHWASHER_SWITCHES
from .controls import supports_control
from .entity import SubZeroEntity

DESCRIPTIONS = (
    SwitchEntityDescription(key="air_filter_on", name="Air purification", icon="mdi:air-filter"),
    SwitchEntityDescription(key="cav_light_on", name="Oven light", icon="mdi:lightbulb"),
    SwitchEntityDescription(key="cav2_light_on", name="Lower oven light", icon="mdi:lightbulb"),
    *(SwitchEntityDescription(key=key, name=name) for key, name in DISHWASHER_SWITCHES.items()),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: SubZeroConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    discovered: set[tuple[str, str]] = set()

    @callback
    def discover_entities() -> None:
        entities = []
        for device_id, coordinator in entry.runtime_data.coordinators.items():
            for description in DESCRIPTIONS:
                key = (device_id, description.key)
                if key not in discovered and supports_control(coordinator.data, description.key):
                    discovered.add(key)
                    entities.append(SubZeroSwitch(coordinator, description))
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
        return (
            super().available
            and supports_control(self.coordinator.data, self.entity_description.key)
            and self.is_on is not None
        )

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_properties({self.entity_description.key: True})

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_properties({self.entity_description.key: False})
