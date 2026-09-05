"""Fridge modes, oven cooking modes, and dishwasher delay start."""

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SubZeroConfigEntry
from .const import COOK_MODES, FRIDGE_ENUM_OPTIONS, FRIDGE_MODE_KEYS, ICE_KEYS, MANUAL_COOK_MODES
from .controls import is_fridge, supports_control
from .entity import SubZeroEntity, async_setup_entities

MODES = {
    "Sabbath": "sabbath_on",
    "High use": "high_use_on",
    "Short vacation": "short_vacation_on",
    "Long vacation": "long_vacation_on",
}
ICE_MODES = {"Max ice": "max_ice_on", "Night ice": "night_ice_on"}
DESCRIPTIONS = (
    SelectEntityDescription(key="ice_maker_mode", name="Ice maker", icon="mdi:ice-pop"),
    SelectEntityDescription(key="operating_mode", name="Mode", icon="mdi:fridge-outline"),
    SelectEntityDescription(
        key="crisp_temp_mode", name="Crisper temperature mode", icon="mdi:thermometer-auto"
    ),
    SelectEntityDescription(key="humidity_control", name="Humidity control", icon="mdi:water"),
    SelectEntityDescription(key="night_mode", name="Night mode", icon="mdi:weather-night"),
    SelectEntityDescription(key="cav_cook_mode", name="Cooking mode", icon="mdi:stove"),
    SelectEntityDescription(key="cav2_cook_mode", name="Lower oven cooking mode", icon="mdi:stove"),
    SelectEntityDescription(
        key="delay_start_timer_duration", name="Delay start", icon="mdi:timer-sand"
    ),
)
ENUM_OPTIONS = {
    **FRIDGE_ENUM_OPTIONS,
    "cav_cook_mode": COOK_MODES,
    "cav2_cook_mode": COOK_MODES,
    "delay_start_timer_duration": {
        "Off": 0,
        **{f"{hours} hour{'s' if hours != 1 else ''}": hours for hours in range(1, 13)},
    },
}


def control_keys(key: str, data: dict) -> tuple[str, ...]:
    if key == "ice_maker_mode":
        return (
            tuple(k for k in ICE_KEYS if k in data)
            if is_fridge(data) and "ice_maker_on" in data
            else ()
        )
    if key == "operating_mode":
        return tuple(k for k in FRIDGE_MODE_KEYS if k in data) if is_fridge(data) else ()
    return (key,) if key in ENUM_OPTIONS and supports_control(data, key) else ()


async def async_setup_entry(
    hass: HomeAssistant, entry: SubZeroConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_entities(
        entry,
        async_add_entities,
        DESCRIPTIONS,
        SubZeroSelect,
        lambda coordinator, description: bool(control_keys(description.key, coordinator.data)),
    )


class SubZeroSelect(SubZeroEntity, SelectEntity):
    @property
    def options(self) -> list[str]:
        data = self.coordinator.data
        if self.entity_description.key == "ice_maker_mode":
            return ["Off", "On", *(name for name, key in ICE_MODES.items() if key in data)]
        if self.entity_description.key == "operating_mode":
            return ["Normal", *(name for name, key in MODES.items() if key in data)]
        key = self.entity_description.key
        if key.endswith("_cook_mode"):
            return [
                name
                for name, value in COOK_MODES.items()
                if value not in MANUAL_COOK_MODES or value == data.get(key)
            ]
        return list(ENUM_OPTIONS[key])

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        keys = control_keys(self.entity_description.key, data)
        if not self.coordinator.last_update_success or not keys:
            return False
        if self.entity_description.key in ENUM_OPTIONS:
            key = self.entity_description.key
            return type(data[key]) is int and data[key] in ENUM_OPTIONS[key].values()
        return all(type(data[key]) is bool for key in keys)

    @property
    def current_option(self) -> str | None:
        if not self.available:
            return None
        data = self.coordinator.data
        if self.entity_description.key in ENUM_OPTIONS:
            key = self.entity_description.key
            return next(name for name, value in ENUM_OPTIONS[key].items() if data[key] == value)
        if self.entity_description.key == "ice_maker_mode":
            active = [name for name, key in ICE_MODES.items() if data.get(key) is True]
            if active:
                return active[0] if len(active) == 1 else None
            return "On" if data["ice_maker_on"] else "Off"
        active = [name for name, key in MODES.items() if data.get(key) is True]
        return active[0] if len(active) == 1 else "Normal" if not active else None

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            raise ServiceValidationError("The appliance does not support that option.")
        data = self.coordinator.data
        key = self.entity_description.key
        if key.endswith("_cook_mode") and option == "Off":
            properties = {key.replace("cook_mode", "unit_on"): False}
        elif key in ENUM_OPTIONS:
            properties = {key: ENUM_OPTIONS[key][option]}
        elif key == "ice_maker_mode":
            selected = ICE_MODES.get(option)
            properties = {
                k: False for k in control_keys(key, data) if k not in ("ice_maker_on", selected)
            }
            if option != "Night ice":
                properties["ice_maker_on"] = option != "Off"
            if selected is not None:
                properties[selected] = True
        else:
            selected = MODES.get(option)
            properties = {k: False for k in control_keys(key, data) if k != selected}
            if selected is not None:
                properties[selected] = True
        await self.coordinator.async_set_properties(properties)
