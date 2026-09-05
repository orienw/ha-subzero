"""Appliance temperatures, setpoints, filter life and Wi-Fi signal strength."""

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
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SubZeroConfigEntry
from .entity import SubZeroEntity

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
                if key in discovered or description.key not in coordinator.data:
                    continue
                if (
                    description.device_class == SensorDeviceClass.TEMPERATURE
                    and coordinator.device.get("temperature_unit") != "F"
                ):
                    continue
                discovered.add(key)
                entities.append(SubZeroSensor(coordinator, description))
        async_add_entities(entities)

    discover_entities()
    for coordinator in entry.runtime_data.coordinators.values():
        entry.async_on_unload(coordinator.async_add_listener(discover_entities))


class SubZeroSensor(SubZeroEntity, SensorEntity):
    @property
    def native_value(self) -> int | float | None:
        key = self.entity_description.key
        value = self.coordinator.data.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        if key.startswith("cav_"):
            if value == 0:
                return None
            if (
                key.startswith("cav_probe_")
                and self.coordinator.data.get("cav_probe_on") is not True
            ):
                return None
        return value
