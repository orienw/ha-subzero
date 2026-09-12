"""Fridge and wine setpoints, accent lighting, and oven kitchen timers."""

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import PERCENTAGE, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SubZeroConfigEntry
from .const import KITCHEN_TIMERS
from .controls import is_finite_number, supports_control, temperature_range, timer_minutes
from .entity import SubZeroEntity, async_setup_entities

DESCRIPTIONS = (
    *(
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
            ("wine_set_temp", "Wine setpoint"),
            ("wine2_set_temp", "Wine setpoint 2"),
        )
    ),
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
    async_setup_entities(
        entry,
        async_add_entities,
        DESCRIPTIONS,
        SubZeroNumber,
        lambda coordinator, description: (
            (
                description.device_class != NumberDeviceClass.TEMPERATURE
                or coordinator.device.get("temperature_unit") == "F"
            )
            and supports_control(coordinator.data, description.key)
        ),
    )


class SubZeroNumber(SubZeroEntity, NumberEntity):
    @property
    def native_value(self) -> int | float | None:
        if self.entity_description.key in KITCHEN_TIMERS:
            return timer_minutes(self.coordinator.data, self.entity_description.key)
        value = self.coordinator.data.get(self.entity_description.key)
        return value if is_finite_number(value) else None

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
        if self.native_value is None:
            return False
        if key == "accent_light_level":
            return True
        if temperature_range(key, data) is None:
            return False
        if key == "crisp_set_temp":
            return type(data.get("crisp_temp_mode")) is int and data["crisp_temp_mode"] == 0
        return key != "frz_set_temp" or "max_ice_on" not in data or data["max_ice_on"] is False

    async def async_set_native_value(self, value: float) -> None:
        if not is_finite_number(value):
            raise ServiceValidationError("Enter a valid number.")
        await self.coordinator.async_set_properties({self.entity_description.key: round(value)})
