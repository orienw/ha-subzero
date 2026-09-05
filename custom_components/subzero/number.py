"""Fridge setpoints, accent lighting, and oven kitchen timers."""

import math

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import PERCENTAGE, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SubZeroConfigEntry
from .const import KITCHEN_TIMERS
from .controls import supports_control, temperature_range, timer_minutes
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
DESCRIPTIONS += (
    NumberEntityDescription(
        key="accent_light_level",
        name="Accent light",
        mode=NumberMode.SLIDER,
        icon="mdi:lightbulb-outline",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        entity_registry_enabled_default=False,
    ),
    *(
        NumberEntityDescription(
            key=key,
            name=name,
            icon="mdi:timer-outline",
            mode=NumberMode.BOX,
            native_min_value=0,
            native_max_value=660,
            native_step=1,
            native_unit_of_measurement=UnitOfTime.MINUTES,
        )
        for key, name in (
            ("kitchen_timer_duration", "Kitchen timer duration"),
            ("kitchen_timer2_duration", "Kitchen timer 2 duration"),
        )
    ),
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
                if (
                    description.device_class == NumberDeviceClass.TEMPERATURE
                    and coordinator.device.get("temperature_unit") != "F"
                ):
                    continue
                key = (device_id, description.key)
                if key not in discovered and supports_control(coordinator.data, description.key):
                    discovered.add(key)
                    entities.append(SubZeroNumber(coordinator, description))
        async_add_entities(entities)

    discover_entities()
    for coordinator in entry.runtime_data.coordinators.values():
        entry.async_on_unload(coordinator.async_add_listener(discover_entities))


class SubZeroNumber(SubZeroEntity, NumberEntity):
    @property
    def native_value(self) -> int | float | None:
        if self.entity_description.key in KITCHEN_TIMERS:
            return timer_minutes(self.coordinator.data, self.entity_description.key)
        value = self.coordinator.data.get(self.entity_description.key)
        return value if type(value) in (int, float) and math.isfinite(value) else None

    @property
    def native_min_value(self) -> float:
        if self.entity_description.device_class != NumberDeviceClass.TEMPERATURE:
            return self.entity_description.native_min_value
        return (temperature_range(self.entity_description.key, self.coordinator.data) or (34, 42))[
            0
        ]

    @property
    def native_max_value(self) -> float:
        if self.entity_description.device_class != NumberDeviceClass.TEMPERATURE:
            return self.entity_description.native_max_value
        return (temperature_range(self.entity_description.key, self.coordinator.data) or (34, 42))[
            1
        ]

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        key = self.entity_description.key
        if not self.coordinator.last_update_success or not supports_control(data, key):
            return False
        if key in KITCHEN_TIMERS:
            return type(data.get(f"{KITCHEN_TIMERS[key]}_active")) is bool
        if key == "accent_light_level":
            return self.native_value is not None
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
            raise ServiceValidationError("Enter a valid number.")
        await self.coordinator.async_set_properties({self.entity_description.key: round(value)})
