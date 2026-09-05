"""Fridge temperature setpoints."""

import math

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberEntityDescription
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SubZeroConfigEntry
from .controls import is_fridge, temperature_range
from .entity import SubZeroEntity

DESCRIPTIONS = tuple(
    NumberEntityDescription(
        key=key,
        name=name,
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        native_step=1,
    )
    for key, name in (
        ("ref_set_temp", "Refrigerator setpoint"),
        ("frz_set_temp", "Freezer setpoint"),
        ("crisp_set_temp", "Crisper setpoint"),
    )
)


async def async_setup_entry(
    hass: HomeAssistant, entry: SubZeroConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    discovered: set[tuple[str, str]] = set()

    @callback
    def discover_entities() -> None:
        entities = []
        for device_id, coordinator in entry.runtime_data.coordinators.items():
            if not is_fridge(coordinator.data) or coordinator.device.get("temperature_unit") != "F":
                continue
            for description in DESCRIPTIONS:
                key = (device_id, description.key)
                if key not in discovered and description.key in coordinator.data:
                    discovered.add(key)
                    entities.append(SubZeroNumber(coordinator, description))
        async_add_entities(entities)

    discover_entities()
    for coordinator in entry.runtime_data.coordinators.values():
        entry.async_on_unload(coordinator.async_add_listener(discover_entities))


class SubZeroNumber(SubZeroEntity, NumberEntity):
    @property
    def native_value(self) -> int | float | None:
        value = self.coordinator.data.get(self.entity_description.key)
        return value if type(value) in (int, float) and math.isfinite(value) else None

    @property
    def native_min_value(self) -> float:
        return (temperature_range(self.entity_description.key, self.coordinator.data) or (34, 42))[
            0
        ]

    @property
    def native_max_value(self) -> float:
        return (temperature_range(self.entity_description.key, self.coordinator.data) or (34, 42))[
            1
        ]

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        key = self.entity_description.key
        if (
            not super().available
            or self.native_value is None
            or temperature_range(key, data) is None
        ):
            return False
        if key == "crisp_set_temp":
            return type(data.get("crisp_temp_mode")) is int and data["crisp_temp_mode"] == 0
        return key != "frz_set_temp" or "max_ice_on" not in data or data["max_ice_on"] is False

    async def async_set_native_value(self, value: float) -> None:
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ServiceValidationError("Enter a valid temperature.")
        await self.coordinator.async_set_properties({self.entity_description.key: round(value)})
