"""Temperature controls for reported refrigeration zones and oven cavities."""

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SubZeroConfigEntry
from .controls import is_finite_number, supports_control, temperature_range
from .entity import SubZeroEntity, async_setup_entities

DESCRIPTIONS = tuple(
    EntityDescription(key=key, name=name)
    for key, name in (
        ("ref_set_temp", "Refrigerator"),
        ("frz_set_temp", "Freezer"),
        ("cav_set_temp", "Oven"),
        ("cav2_set_temp", "Lower oven"),
    )
)


async def async_setup_entry(
    hass: HomeAssistant, entry: SubZeroConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_entities(
        entry,
        async_add_entities,
        DESCRIPTIONS,
        SubZeroClimate,
        lambda coordinator, description: (
            coordinator.device.get("temperature_unit") == "F"
            and supports_control(coordinator.data, description.key)
            and (
                not description.key.startswith("cav")
                or description.key.replace("set_temp", "unit_on") in coordinator.data
            )
        ),
    )


class SubZeroClimate(SubZeroEntity, ClimateEntity):
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_target_temperature_step = 1

    def __init__(self, coordinator, description):
        super().__init__(coordinator, description)
        self._prefix = description.key.split("_", 1)[0]
        self._oven = self._prefix in {"cav", "cav2"}
        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT] if self._oven else [HVACMode.COOL]
        if self._oven:
            self._attr_supported_features |= (
                ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
            )
            self._attr_target_temperature_step = 5

    @property
    def available(self) -> bool:
        return (
            super().available
            and supports_control(self.coordinator.data, self.entity_description.key)
            and (
                not self._oven or type(self.coordinator.data.get(f"{self._prefix}_unit_on")) is bool
            )
        )

    @property
    def hvac_mode(self) -> HVACMode | None:
        if not self._oven:
            return HVACMode.COOL
        value = self.coordinator.data.get(f"{self._prefix}_unit_on")
        return HVACMode.HEAT if value is True else HVACMode.OFF if value is False else None

    @property
    def current_temperature(self) -> int | float | None:
        key = f"{self._prefix}_{'temp' if self._oven else 'display_temp'}"
        value = self.coordinator.data.get(key)
        if not is_finite_number(value):
            return None
        return None if self._oven and value == 0 else value

    @property
    def target_temperature(self) -> int | float | None:
        value = self.coordinator.data.get(self.entity_description.key)
        if not is_finite_number(value):
            return None
        return None if self._oven and value == 0 else value

    @property
    def min_temp(self) -> float:
        return temperature_range(self.entity_description.key, self.coordinator.data)[0]

    @property
    def max_temp(self) -> float:
        return temperature_range(self.entity_description.key, self.coordinator.data)[1]

    async def async_set_temperature(self, **kwargs) -> None:
        value = kwargs.get(ATTR_TEMPERATURE)
        if not is_finite_number(value):
            raise ServiceValidationError("Enter a valid temperature.")
        mode = kwargs.get("hvac_mode")
        if mode is not None and mode not in self.hvac_modes:
            raise ServiceValidationError("The appliance does not support that mode.")
        properties = {self.entity_description.key: round(value)}
        if self._oven and mode is not None:
            properties[f"{self._prefix}_unit_on"] = mode == HVACMode.HEAT
        await self.coordinator.async_set_properties(properties)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode not in self.hvac_modes:
            raise ServiceValidationError("The appliance does not support that mode.")
        if self._oven:
            await self.coordinator.async_set_properties(
                {f"{self._prefix}_unit_on": hvac_mode == HVACMode.HEAT}
            )

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)
