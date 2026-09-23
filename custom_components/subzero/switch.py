"""Air purification, oven lights, and dishwasher options."""

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SubZeroConfigEntry
from .const import DISHWASHER_SWITCHES, HOOD_SWITCHES
from .controls import supports_control
from .entity import SubZeroEntity, async_setup_entities

DESCRIPTIONS = (
    *(SwitchEntityDescription(key=key, name=name) for key, name in HOOD_SWITCHES.items()),
    SwitchEntityDescription(key="air_filter_on", name="Air purification", icon="mdi:air-filter"),
    SwitchEntityDescription(
        key="internal_dispenser_enabled", name="Internal water dispenser", icon="mdi:water"
    ),
    SwitchEntityDescription(key="cav_light_on", name="Oven light", icon="mdi:lightbulb"),
    SwitchEntityDescription(key="cav2_light_on", name="Lower oven light", icon="mdi:lightbulb"),
    *(SwitchEntityDescription(key=key, name=name) for key, name in DISHWASHER_SWITCHES.items()),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: SubZeroConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_entities(
        entry,
        async_add_entities,
        DESCRIPTIONS,
        SubZeroSwitch,
        lambda coordinator, description: supports_control(coordinator.data, description.key),
    )
    async_setup_entities(
        entry,
        async_add_entities,
        (
            SwitchEntityDescription(
                key="halo_max_percent", name="Halo light", icon="mdi:lightbulb-outline"
            ),
        ),
        SubZeroHaloSwitch,
        lambda coordinator, description: supports_control(coordinator.data, description.key),
    )


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


class SubZeroHaloSwitch(SubZeroSwitch):
    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.data.get("halo_max_percent")
        return value != 0 if type(value) is int else None

    async def async_turn_on(self, **kwargs) -> None:
        # The app writes its on level only when the halo is off.
        if not self.is_on:
            await self.coordinator.async_set_properties({"halo_max_percent": 30})

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_properties({"halo_max_percent": 0})
