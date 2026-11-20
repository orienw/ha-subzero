"""Refrigeration and probe setpoints, hood shutoff delay, and oven kitchen timers."""

from datetime import UTC, datetime, timedelta

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import UnitOfTemperature, UnitOfTime
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time

from . import SubZeroConfigEntry
from .const import KITCHEN_TIMERS
from .controls import (
    appliance_datetime,
    crisper_manual_enabled,
    freezer_setpoint_enabled,
    is_finite_number,
    is_int,
    probe_setpoint_enabled,
    supports_control,
    temperature_range,
    timer_minutes,
)
from .entity import SubZeroEntity, async_setup_entities

DELAY_OFF_DESCRIPTION = NumberEntityDescription(
    key="delay_off_duration",
    name="Delayed shutoff duration",
    icon="mdi:timer-outline",
    mode=NumberMode.BOX,
    native_min_value=0,
    native_max_value=719,
    native_step=1,
    native_unit_of_measurement=UnitOfTime.MINUTES,
)
TEMPERATURE_DESCRIPTIONS = (
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
            ("ref2_set_temp", "Refrigerator drawer setpoint"),
            ("frz_set_temp", "Freezer setpoint"),
            ("crisp_set_temp", "Crisper setpoint"),
            ("wine_set_temp", "Wine setpoint"),
            ("wine2_set_temp", "Wine setpoint 2"),
        )
    ),
    *(
        NumberEntityDescription(
            key=key,
            name=name,
            device_class=NumberDeviceClass.TEMPERATURE,
            native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
            native_step=5,
        )
        for key, name in (
            ("cav_probe_set_temp", "Probe target temperature"),
            ("cav2_probe_set_temp", "Lower oven probe target temperature"),
        )
    ),
)
TIMER_DESCRIPTIONS = tuple(
    NumberEntityDescription(
        key=key,
        name=name,
        icon="mdi:timer-outline",
        mode=NumberMode.BOX,
        native_min_value=0,
        native_max_value=719,
        native_step=1,
        native_unit_of_measurement=UnitOfTime.MINUTES,
    )
    for key, name in (
        ("kitchen_timer_duration", "Kitchen timer duration"),
        ("kitchen_timer2_duration", "Kitchen timer 2 duration"),
    )
)


async def async_setup_entry(
    hass: HomeAssistant, entry: SubZeroConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_entities(
        entry,
        async_add_entities,
        (DELAY_OFF_DESCRIPTION,),
        SubZeroDelayOffNumber,
        lambda coordinator, description: supports_control(coordinator.data, description.key),
    )
    async_setup_entities(
        entry,
        async_add_entities,
        TEMPERATURE_DESCRIPTIONS,
        SubZeroTemperatureNumber,
        lambda coordinator, description: (
            coordinator.device.get("temperature_unit") in ("F", "C")
            and supports_control(coordinator.data, description.key)
        ),
    )
    async_setup_entities(
        entry,
        async_add_entities,
        TIMER_DESCRIPTIONS,
        SubZeroKitchenTimerNumber,
        lambda coordinator, description: supports_control(coordinator.data, description.key),
    )


class SubZeroDelayOffNumber(SubZeroEntity, NumberEntity):
    @property
    def native_value(self) -> float | None:
        value = self.coordinator.data.get(self.entity_description.key)
        return value / 60000 if is_int(value) else None

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        key = self.entity_description.key
        return (
            self.coordinator.last_update_success
            and supports_control(data, key)
            and is_int(data[key])
            and 0 <= data[key] <= 719 * 60000
        )

    async def async_set_native_value(self, value: float) -> None:
        if not is_finite_number(value):
            raise ServiceValidationError("Enter a valid number.")
        await self.coordinator.async_set_properties(
            {self.entity_description.key: round(value) * 60000}
        )


class SubZeroTemperatureNumber(SubZeroEntity, NumberEntity):
    @property
    def native_value(self) -> int | float | None:
        key = self.entity_description.key
        value = self.coordinator.data.get(key)
        if key.endswith("_probe_set_temp") and value == 0:
            return None
        return value if is_finite_number(value) else None

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
            not self.coordinator.last_update_success
            or not supports_control(data, key)
            or not is_finite_number(data.get(key))
            or temperature_range(key, data) is None
        ):
            return False
        if key == "crisp_set_temp":
            return crisper_manual_enabled(data)
        if key.endswith("_probe_set_temp"):
            return probe_setpoint_enabled(data, key.split("_", 1)[0])
        return key != "frz_set_temp" or freezer_setpoint_enabled(data)

    async def async_set_native_value(self, value: float) -> None:
        if not is_finite_number(value):
            raise ServiceValidationError("Enter a valid number.")
        await self.coordinator.async_set_properties({self.entity_description.key: round(value)})


class SubZeroKitchenTimerNumber(SubZeroEntity, NumberEntity):
    _cancel_countdown: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._stop_countdown)
        self._schedule_countdown()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._schedule_countdown()
        super()._handle_coordinator_update()

    @callback
    def _stop_countdown(self) -> None:
        if self._cancel_countdown is not None:
            self._cancel_countdown()
            self._cancel_countdown = None

    @callback
    def _schedule_countdown(self) -> None:
        """Update a running kitchen timer's minutes left as each one passes."""
        self._stop_countdown()
        key = self.entity_description.key
        prefix = KITCHEN_TIMERS[key]
        data = self.coordinator.data
        end = appliance_datetime(data.get(f"{prefix}_end_time"), self.coordinator.clock_zone)
        minutes = timer_minutes(data, key, self.coordinator.clock_zone)
        if data.get(f"{prefix}_active") is not True or end is None or not minutes:
            return
        # In UTC, since a time read in a zone with daylight saving steps by wall clock.
        self._cancel_countdown = async_track_point_in_utc_time(
            self.hass, self._count_down, end.astimezone(UTC) - timedelta(minutes=minutes - 1)
        )

    @callback
    def _count_down(self, now: datetime) -> None:
        self._cancel_countdown = None
        self._schedule_countdown()
        self.async_write_ha_state()

    @property
    def native_value(self) -> int | float | None:
        return timer_minutes(
            self.coordinator.data, self.entity_description.key, self.coordinator.clock_zone
        )

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        key = self.entity_description.key
        return (
            self.coordinator.last_update_success
            and supports_control(data, key)
            and type(data.get(f"{KITCHEN_TIMERS[key]}_active")) is bool
        )

    async def async_set_native_value(self, value: float) -> None:
        if not is_finite_number(value):
            raise ServiceValidationError("Enter a valid number.")
        await self.coordinator.async_set_properties({self.entity_description.key: round(value)})
