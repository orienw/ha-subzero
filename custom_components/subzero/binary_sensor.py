"""Door, operating-mode and service indicators."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SubZeroConfigEntry
from .entity import SubZeroEntity, async_setup_entities

# Entities write state in this order within one update, and automations can observe it.
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
    BinarySensorEntityDescription(key="night_ice_on", name="Night ice", icon="mdi:weather-night"),
    BinarySensorEntityDescription(key="sabbath_on", name="Sabbath mode", icon="mdi:star-david"),
    BinarySensorEntityDescription(
        key="high_use_on", name="High use mode", icon="mdi:fridge-outline"
    ),
    BinarySensorEntityDescription(
        key="short_vacation_on", name="Short vacation mode", icon="mdi:bag-suitcase-outline"
    ),
    BinarySensorEntityDescription(
        key="long_vacation_on", name="Long vacation mode", icon="mdi:bag-suitcase-outline"
    ),
    BinarySensorEntityDescription(
        key="cav_door_ajar", name="Oven door", device_class=BinarySensorDeviceClass.DOOR
    ),
    BinarySensorEntityDescription(key="cav_unit_on", name="Cooking", icon="mdi:stove"),
    BinarySensorEntityDescription(
        key="cav_at_set_temp", name="Preheated", icon="mdi:thermometer-check"
    ),
    BinarySensorEntityDescription(key="cav_remote_ready", name="Remote ready", icon="mdi:remote"),
    BinarySensorEntityDescription(key="cav_probe_on", name="Probe in use", icon="mdi:thermometer"),
    BinarySensorEntityDescription(
        key="cav_probe_at_set_temp", name="Probe target reached", icon="mdi:thermometer-check"
    ),
    BinarySensorEntityDescription(
        key="cav_gourmet_mode_on", name="Gourmet mode", icon="mdi:chef-hat"
    ),
    BinarySensorEntityDescription(
        key="cav_cook_timer_complete", name="Cooking timer complete", icon="mdi:timer-check-outline"
    ),
    BinarySensorEntityDescription(
        key="kitchen_timer_active", name="Kitchen timer active", icon="mdi:timer-outline"
    ),
    BinarySensorEntityDescription(
        key="kitchen_timer_complete", name="Kitchen timer complete", icon="mdi:timer-check-outline"
    ),
    BinarySensorEntityDescription(
        key="kitchen_timer2_active", name="Kitchen timer 2 active", icon="mdi:timer-outline"
    ),
    BinarySensorEntityDescription(
        key="kitchen_timer2_complete",
        name="Kitchen timer 2 complete",
        icon="mdi:timer-check-outline",
    ),
    BinarySensorEntityDescription(
        key="cav_cook_timer_active", name="Cooking timer active", icon="mdi:timer-outline"
    ),
    BinarySensorEntityDescription(
        key="cav_mode_change_enabled", name="Cooking mode change enabled", icon="mdi:stove"
    ),
    BinarySensorEntityDescription(
        key="cav2_door_ajar", name="Lower oven door", device_class=BinarySensorDeviceClass.DOOR
    ),
    BinarySensorEntityDescription(key="cav2_unit_on", name="Lower oven cooking", icon="mdi:stove"),
    BinarySensorEntityDescription(
        key="cav2_at_set_temp", name="Lower oven preheated", icon="mdi:thermometer-check"
    ),
    BinarySensorEntityDescription(
        key="cav2_remote_ready", name="Lower oven remote ready", icon="mdi:remote"
    ),
    BinarySensorEntityDescription(
        key="cav2_probe_on", name="Lower oven probe in use", icon="mdi:thermometer"
    ),
    BinarySensorEntityDescription(
        key="cav2_probe_at_set_temp",
        name="Lower oven probe target reached",
        icon="mdi:thermometer-check",
    ),
    BinarySensorEntityDescription(
        key="cav2_gourmet_mode_on", name="Lower oven gourmet mode", icon="mdi:chef-hat"
    ),
    BinarySensorEntityDescription(
        key="cav2_cook_timer_complete",
        name="Lower oven cooking timer complete",
        icon="mdi:timer-check-outline",
    ),
    BinarySensorEntityDescription(
        key="cav2_cook_timer_active",
        name="Lower oven cooking timer active",
        icon="mdi:timer-outline",
    ),
    BinarySensorEntityDescription(
        key="cav2_mode_change_enabled",
        name="Lower oven cooking mode change enabled",
        icon="mdi:stove",
    ),
    BinarySensorEntityDescription(
        key="door_ajar", name="Door", device_class=BinarySensorDeviceClass.DOOR
    ),
    BinarySensorEntityDescription(
        key="wash_cycle_on", name="Wash cycle active", device_class=BinarySensorDeviceClass.RUNNING
    ),
    BinarySensorEntityDescription(key="remote_ready", name="Remote ready", icon="mdi:remote"),
    BinarySensorEntityDescription(
        key="rinse_aid_low", name="Rinse aid low", device_class=BinarySensorDeviceClass.PROBLEM
    ),
    BinarySensorEntityDescription(
        key="softener_low", name="Softener salt low", device_class=BinarySensorDeviceClass.PROBLEM
    ),
    BinarySensorEntityDescription(
        key="delay_start_timer_active", name="Delay start active", icon="mdi:timer-sand"
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: SubZeroConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_entities(
        entry,
        async_add_entities,
        DESCRIPTIONS,
        SubZeroBinarySensor,
        lambda coordinator, description: description.key in coordinator.data,
    )


class SubZeroBinarySensor(SubZeroEntity, BinarySensorEntity):
    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.data.get(self.entity_description.key)
        return value if isinstance(value, bool) else None
