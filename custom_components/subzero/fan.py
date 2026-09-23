"""Wolf hood fan power and speed."""

import math

from homeassistant.components.fan import FanEntity, FanEntityDescription, FanEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SubZeroConfigEntry
from .controls import supports_control
from .entity import SubZeroEntity, async_setup_entities


async def async_setup_entry(
    hass: HomeAssistant, entry: SubZeroConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_entities(
        entry,
        async_add_entities,
        (FanEntityDescription(key="fan_on", name="Fan"),),
        SubZeroFan,
        lambda coordinator, description: supports_control(coordinator.data, "fan_on"),
    )


class SubZeroFan(SubZeroEntity, FanEntity):
    _attr_speed_count = 4

    @property
    def supported_features(self) -> FanEntityFeature:
        features = FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
        if supports_control(self.coordinator.data, "fan_speed"):
            features |= FanEntityFeature.SET_SPEED
        return features

    @property
    def available(self) -> bool:
        return (
            super().available
            and supports_control(self.coordinator.data, "fan_on")
            and self.is_on is not None
        )

    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.data.get("fan_on")
        return value if type(value) is bool else None

    @property
    def percentage(self) -> int | None:
        if self.is_on is False:
            return 0
        speed = self.coordinator.data.get("fan_speed")
        return speed * 25 if type(speed) is int and 0 <= speed <= 4 else None

    async def async_set_percentage(self, percentage: int) -> None:
        await self.async_turn_on(percentage)

    async def async_turn_on(self, percentage=None, preset_mode=None, **kwargs) -> None:
        if percentage == 0:
            await self.async_turn_off()
            return
        properties = {}
        if percentage is not None and FanEntityFeature.SET_SPEED in self.supported_features:
            properties["fan_speed"] = math.ceil(percentage / 25)
        properties["fan_on"] = True
        await self.coordinator.async_set_properties(properties)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_properties({"fan_on": False})
