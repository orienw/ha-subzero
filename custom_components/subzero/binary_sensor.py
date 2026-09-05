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
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="air_filter_on", name="Air purification", icon="mdi:air-filter"
    ),
    BinarySensorEntityDescription(
        key="sabbath_on",
        name="Sabbath mode",
        icon="mdi:star-david",
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="high_use_on",
        name="High use",
        icon="mdi:fridge-outline",
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="short_vacation_on",
        name="Short vacation",
        icon="mdi:bag-suitcase-outline",
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="long_vacation_on",
        name="Long vacation",
        icon="mdi:bag-suitcase-outline",
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
            if description.key in coordinator.data and description.key not in discovered
        ]
        discovered.update(description.key for description in descriptions)
        async_add_entities(
            SubZeroBinarySensor(coordinator, description) for description in descriptions
        )

    discover_entities()
    entry.async_on_unload(coordinator.async_add_listener(discover_entities))


class SubZeroBinarySensor(SubZeroEntity, BinarySensorEntity):
    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.data.get(self.entity_description.key)
        return value if isinstance(value, bool) else None
