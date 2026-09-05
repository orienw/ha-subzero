"""Temperature setpoints, filter life and Wi-Fi signal strength."""

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
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: SubZeroConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    discovered: set[str] = set()

    @callback
    def discover_entities() -> None:
        descriptions = [
            description
            for description in DESCRIPTIONS
            if description.key in coordinator.data
            and description.key not in discovered
            and (
                description.device_class != SensorDeviceClass.TEMPERATURE
                or entry.data.get("temperature_unit") == "F"
            )
        ]
        discovered.update(description.key for description in descriptions)
        async_add_entities(SubZeroSensor(coordinator, description) for description in descriptions)

    discover_entities()
    entry.async_on_unload(coordinator.async_add_listener(discover_entities))


class SubZeroSensor(SubZeroEntity, SensorEntity):
    @property
    def native_value(self) -> int | float | None:
        value = self.coordinator.data.get(self.entity_description.key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
