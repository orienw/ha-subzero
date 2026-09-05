"""Fridge modes, ice maker, crisper temperature and humidity choices."""

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SubZeroConfigEntry
from .const import FRIDGE_ENUM_OPTIONS, FRIDGE_MODE_KEYS, ICE_KEYS
from .controls import is_fridge
from .entity import SubZeroEntity

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
)


def control_keys(key: str, data: dict) -> tuple[str, ...]:
    if key == "ice_maker_mode":
        return tuple(k for k in ICE_KEYS if k in data) if "ice_maker_on" in data else ()
    if key == "operating_mode":
        return tuple(k for k in FRIDGE_MODE_KEYS if k in data)
    return (key,) if key in FRIDGE_ENUM_OPTIONS and key in data else ()


async def async_setup_entry(
    hass: HomeAssistant, entry: SubZeroConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    discovered: set[tuple[str, str]] = set()

    @callback
    def discover_entities() -> None:
        entities = []
        for device_id, coordinator in entry.runtime_data.coordinators.items():
            if not is_fridge(coordinator.data):
                continue
            for description in DESCRIPTIONS:
                key = (device_id, description.key)
                if key not in discovered and control_keys(description.key, coordinator.data):
                    discovered.add(key)
                    entities.append(SubZeroSelect(coordinator, description))
        async_add_entities(entities)

    discover_entities()
    for coordinator in entry.runtime_data.coordinators.values():
        entry.async_on_unload(coordinator.async_add_listener(discover_entities))


class SubZeroSelect(SubZeroEntity, SelectEntity):
    @property
    def options(self) -> list[str]:
        data = self.coordinator.data
        if self.entity_description.key == "ice_maker_mode":
            return ["Off", "On", *(name for name, key in ICE_MODES.items() if key in data)]
        if self.entity_description.key == "operating_mode":
            return ["Normal", *(name for name, key in MODES.items() if key in data)]
        return list(FRIDGE_ENUM_OPTIONS[self.entity_description.key])

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        keys = control_keys(self.entity_description.key, data)
        if not self.coordinator.last_update_success or not is_fridge(data) or not keys:
            return False
        if self.entity_description.key in FRIDGE_ENUM_OPTIONS:
            key = self.entity_description.key
            return type(data[key]) is int and data[key] in FRIDGE_ENUM_OPTIONS[key].values()
        return all(type(data[key]) is bool for key in keys)

    @property
    def current_option(self) -> str | None:
        if not self.available:
            return None
        data = self.coordinator.data
        if self.entity_description.key in FRIDGE_ENUM_OPTIONS:
            key = self.entity_description.key
            return next(
                name for name, value in FRIDGE_ENUM_OPTIONS[key].items() if data[key] == value
            )
        if self.entity_description.key == "ice_maker_mode":
            if not data["ice_maker_on"]:
                return "Off"
            active = [name for name, key in ICE_MODES.items() if data.get(key) is True]
            return active[0] if len(active) == 1 else "On" if not active else None
        active = [name for name, key in MODES.items() if data.get(key) is True]
        return active[0] if len(active) == 1 else "Normal" if not active else None

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            raise ServiceValidationError("The appliance does not support that option.")
        data = self.coordinator.data
        key = self.entity_description.key
        if key in FRIDGE_ENUM_OPTIONS:
            properties = {key: FRIDGE_ENUM_OPTIONS[key][option]}
        elif key == "ice_maker_mode":
            selected = ICE_MODES.get(option)
            properties = {
                k: False for k in control_keys(key, data) if k not in ("ice_maker_on", selected)
            }
            properties["ice_maker_on"] = option != "Off"
            if selected is not None:
                properties[selected] = True
        else:
            selected = MODES.get(option)
            properties = {k: False for k in control_keys(key, data) if k != selected}
            if selected is not None:
                properties[selected] = True
        await self.coordinator.async_set_properties(properties)
