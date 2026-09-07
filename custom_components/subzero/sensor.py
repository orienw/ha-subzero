"""Appliance temperatures, timers, cycle status, and diagnostics."""

from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SubZeroConfigEntry
from .const import COOK_MODES, NETWORK_KEYS, OVEN_PREFIXES, WASH_CYCLES, WASH_STATUSES
from .controls import appliance_datetime, is_finite_number
from .entity import SubZeroEntity, async_setup_entities

ENUM_VALUES = {
    "wash_cycle": WASH_CYCLES,
    "wash_status": WASH_STATUSES,
    **{
        f"{prefix}_cook_mode": {value: name for name, value in COOK_MODES.items()}
        for prefix in OVEN_PREFIXES
    },
}
# Entities write state in this order within one update, and automations can observe it.
DESCRIPTIONS = (
    SensorEntityDescription(
        key="ref_set_temp",
        name="Refrigerator setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
    ),
    SensorEntityDescription(
        key="frz_set_temp",
        name="Freezer setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
    ),
    SensorEntityDescription(
        key="crisp_set_temp",
        name="Crisper setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
    ),
    SensorEntityDescription(
        key="cav_temp",
        name="Oven temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="cav_set_temp",
        name="Oven setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
    ),
    SensorEntityDescription(
        key="cav_probe_temp",
        name="Probe temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="cav_probe_set_temp",
        name="Probe setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
    ),
    SensorEntityDescription(
        key="air_filter_pct_remaining",
        name="Air filter remaining",
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:air-filter",
    ),
    SensorEntityDescription(
        key="water_filter_pct_remaining",
        name="Water filter remaining",
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:water",
    ),
    SensorEntityDescription(
        key="ap_rssi",
        name="Wi-Fi signal",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="cav2_temp",
        name="Lower oven temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="cav2_set_temp",
        name="Lower oven setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
    ),
    SensorEntityDescription(
        key="cav2_probe_temp",
        name="Lower oven probe temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="cav2_probe_set_temp",
        name="Lower oven probe setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
    ),
    SensorEntityDescription(
        key="live_reporting_mode",
        name="Live reporting mode",
        device_class=SensorDeviceClass.ENUM,
        options=["Cloud push", "Disconnected"],
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    *(
        SensorEntityDescription(
            key=key,
            name=name,
            device_class=SensorDeviceClass.TEMPERATURE,
            native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        )
        for key, name in (
            ("ref_display_temp", "Refrigerator display temperature"),
            ("frz_display_temp", "Freezer display temperature"),
        )
    ),
    SensorEntityDescription(
        key="water_filter_gal_remaining",
        name="Water filter capacity remaining",
        device_class=SensorDeviceClass.VOLUME,
        native_unit_of_measurement=UnitOfVolume.GALLONS,
        icon="mdi:water",
    ),
    *(
        SensorEntityDescription(
            key=key,
            name=name,
            device_class=SensorDeviceClass.ENUM,
            options=list(ENUM_VALUES[key].values()),
        )
        for key, name in (
            ("wash_cycle", "Wash cycle"),
            ("wash_status", "Wash status"),
            ("cav_cook_mode", "Cooking mode"),
            ("cav2_cook_mode", "Lower oven cooking mode"),
        )
    ),
    *(
        SensorEntityDescription(key=key, name=name, device_class=SensorDeviceClass.TIMESTAMP)
        for key, name in (
            ("max_ice_start_time", "Max ice start"),
            ("max_ice_end_time", "Max ice end"),
            ("high_use_start_time", "High use start"),
            ("high_use_end_time", "High use end"),
            ("cav_cook_timer_start_time", "Cooking timer start"),
            ("cav_cook_timer_end_time", "Cooking timer end"),
            ("cav2_cook_timer_start_time", "Lower oven cooking timer start"),
            ("cav2_cook_timer_end_time", "Lower oven cooking timer end"),
            ("kitchen_timer_start_time", "Kitchen timer start"),
            ("kitchen_timer_end_time", "Kitchen timer end"),
            ("kitchen_timer2_start_time", "Kitchen timer 2 start"),
            ("kitchen_timer2_end_time", "Kitchen timer 2 end"),
            ("wash_cycle_end_time", "Wash cycle end"),
            ("delay_start_timer_start_time", "Delay start timer start"),
            ("delay_start_timer_end_time", "Delay start timer end"),
        )
    ),
    SensorEntityDescription(
        key="uptime",
        name="Uptime",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    *(
        SensorEntityDescription(
            key=key,
            name=name,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
        )
        for key, name in (("ipv4_addr", "IP address"), ("device_wlan_id", "MAC address"))
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: SubZeroConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_entities(
        entry,
        async_add_entities,
        DESCRIPTIONS,
        SubZeroSensor,
        lambda coordinator, description: (
            (description.key == "live_reporting_mode" or description.key in coordinator.data)
            and (
                description.device_class != SensorDeviceClass.TEMPERATURE
                or coordinator.device.get("temperature_unit") == "F"
            )
        ),
    )


class SubZeroSensor(SubZeroEntity, SensorEntity):
    @property
    def available(self) -> bool:
        return self.entity_description.key == "live_reporting_mode" or super().available

    @property
    def native_value(self) -> int | float | str | datetime | None:
        key = self.entity_description.key
        if key == "live_reporting_mode":
            return (
                "Cloud push"
                if self.coordinator.client.push_connected and self.coordinator.last_update_success
                else "Disconnected"
            )
        value = self.coordinator.data.get(key)
        if self.entity_description.device_class == SensorDeviceClass.TIMESTAMP:
            return appliance_datetime(value, self.coordinator.data)
        if key in ENUM_VALUES:
            return ENUM_VALUES[key].get(value) if type(value) is int else None
        if key in NETWORK_KEYS:
            return value if isinstance(value, str) else None
        if key == "uptime" and isinstance(value, str):
            try:
                hours, minutes, seconds = map(int, value.split(":"))
                return (
                    hours * 3600 + minutes * 60 + seconds
                    if hours >= 0 and 0 <= minutes < 60 and 0 <= seconds < 60
                    else None
                )
            except ValueError:
                return None
        if not is_finite_number(value):
            return None
        if key.startswith(("cav_", "cav2_")):
            if value == 0:
                return None
            if (
                "_probe_" in key
                and self.coordinator.data.get(key.split("_", 1)[0] + "_probe_on") is not True
            ):
                return None
        return value
