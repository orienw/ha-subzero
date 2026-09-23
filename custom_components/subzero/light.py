"""Wolf hood task-light power, brightness, and white temperature."""

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ColorMode,
    LightEntity,
    LightEntityDescription,
)
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
        (LightEntityDescription(key="light_on", name="Task light"),),
        SubZeroLight,
        lambda coordinator, description: supports_control(coordinator.data, "light_on"),
    )


class SubZeroLight(SubZeroEntity, LightEntity):
    _attr_min_color_temp_kelvin = 2700
    _attr_max_color_temp_kelvin = 5000

    @property
    def color_mode(self) -> ColorMode:
        data = self.coordinator.data
        if "light_percent" not in data:
            return ColorMode.ONOFF
        return ColorMode.COLOR_TEMP if "color_level" in data else ColorMode.BRIGHTNESS

    @property
    def supported_color_modes(self) -> set[ColorMode]:
        return {self.color_mode}

    @property
    def available(self) -> bool:
        return (
            super().available
            and supports_control(self.coordinator.data, "light_on")
            and self.is_on is not None
        )

    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.data.get("light_on")
        return value if type(value) is bool else None

    @property
    def brightness(self) -> int | None:
        value = self.coordinator.data.get("light_percent")
        return round(value * 255 / 100) if type(value) is int and 0 <= value <= 100 else None

    @property
    def color_temp_kelvin(self) -> int | None:
        value = self.coordinator.data.get("color_level")
        return 2700 + value * 23 if type(value) is int and 0 <= value <= 100 else None

    async def async_turn_on(self, **kwargs) -> None:
        properties = {}
        if ATTR_BRIGHTNESS in kwargs:
            properties["light_percent"] = max(5, round(kwargs[ATTR_BRIGHTNESS] * 100 / 255))
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = min(
                max(kwargs[ATTR_COLOR_TEMP_KELVIN], self.min_color_temp_kelvin),
                self.max_color_temp_kelvin,
            )
            properties["color_level"] = round((kelvin - 2700) / 23)
        properties["light_on"] = True
        await self.coordinator.async_set_properties(properties)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_properties({"light_on": False})
