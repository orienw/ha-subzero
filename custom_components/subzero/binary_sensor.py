"""Door, operating-mode and service indicators."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SubZeroConfigEntry
from .entity import SubZeroEntity

DESCRIPTIONS = (
    BinarySensorEntityDescription(
        key="ref_door_ajar", name="Refrigerator door", device_class=BinarySensorDeviceClass.DOOR
    ),
    BinarySensorEntityDescription(
        key="frz_door_ajar", name="Freezer door", device_class=BinarySensorDeviceClass.DOOR
    ),
    BinarySensorEntityDescription(
        key="service_required",
        name="Service required",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    BinarySensorEntityDescription(
        key="unit_on", name="Power", device_class=BinarySensorDeviceClass.POWER
    ),
    BinarySensorEntityDescription(key="ice_maker_on", name="Ice maker enabled", icon="mdi:ice-pop"),
    BinarySensorEntityDescription(key="max_ice_on", name="Max ice", icon="mdi:ice-pop"),
    BinarySensorEntityDescription(
        key="night_ice_on",
        name="Night ice",
        icon="mdi:weather-night",
    ),
    BinarySensorEntityDescription(
        key="air_filter_on", name="Air purification", icon="mdi:air-filter"
    ),
    BinarySensorEntityDescription(
        key="sabbath_on",
        name="Sabbath mode",
        icon="mdi:star-david",
    ),
    BinarySensorEntityDescription(
        key="high_use_on",
        name="High use mode",
        icon="mdi:fridge-outline",
    ),
    BinarySensorEntityDescription(
        key="short_vacation_on",
        name="Short vacation mode",
        icon="mdi:bag-suitcase-outline",
    ),
    BinarySensorEntityDescription(
        key="long_vacation_on",
        name="Long vacation mode",
        icon="mdi:bag-suitcase-outline",
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
                if key not in discovered and description.key in coordinator.data:
                    discovered.add(key)
                    entities.append(SubZeroBinarySensor(coordinator, description))
        async_add_entities(entities)

    discover_entities()
    for coordinator in entry.runtime_data.coordinators.values():
        entry.async_on_unload(coordinator.async_add_listener(discover_entities))


class SubZeroBinarySensor(SubZeroEntity, BinarySensorEntity):
    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.data.get(self.entity_description.key)
        return value if isinstance(value, bool) else None
