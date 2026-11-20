"""Fridge modes, accent lighting, oven cooking modes, and dishwasher delay start."""

from collections.abc import Collection

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SubZeroConfigEntry
from .const import (
    COOK_MODES,
    DISHWASHER_MODES,
    DOOR_AJAR_TIMEOUTS,
    FRIDGE_ENUM_OPTIONS,
    HOOD_SENSITIVITY,
    ICE_KEYS,
    ICE_MODES,
    MANUAL_COOK_MODES,
    WASH_CYCLES,
)
from .controls import (
    accent_light_options,
    cook_mode_offered,
    enum_labels,
    ice_mode,
    is_fridge,
    is_ice_maker,
    is_int,
    operating_mode_keys,
    supports_control,
    wash_settings_enabled,
)
from .entity import SubZeroEntity, async_setup_entities

MODES = {
    "Sabbath": "sabbath_on",
    "High use": "high_use_on",
    "Short vacation": "short_vacation_on",
    "Long vacation": "long_vacation_on",
}
ENUM_OPTIONS = {
    "auto_sensivity": HOOD_SENSITIVITY,
    **FRIDGE_ENUM_OPTIONS,
    "cav_cook_mode": COOK_MODES,
    "cav2_cook_mode": COOK_MODES,
    "wash_cycle": {name: value for value, name in WASH_CYCLES.items() if value != 0},
    "mode": DISHWASHER_MODES,
    "door_ajar_timeout": DOOR_AJAR_TIMEOUTS,
    "delay_start_timer_duration": {
        "Off": 0,
        **{f"{hours} hour{'s' if hours != 1 else ''}": hours for hours in range(1, 13)},
    },
}

AUTO_SENSITIVITY_DESCRIPTIONS = (
    SelectEntityDescription(
        key="auto_sensivity", name="Automatic fan sensitivity", icon="mdi:fan-auto"
    ),
)
ICE_MAKER_DESCRIPTIONS = (
    SelectEntityDescription(key="ice_maker_mode", name="Ice maker", icon="mdi:ice-pop"),
)
OPERATING_MODE_DESCRIPTIONS = (
    SelectEntityDescription(key="operating_mode", name="Mode", icon="mdi:fridge-outline"),
)
FRIDGE_ENUM_DESCRIPTIONS = (
    SelectEntityDescription(
        key="crisp_temp_mode", name="Crisper temperature mode", icon="mdi:thermometer-auto"
    ),
    SelectEntityDescription(key="humidity_control", name="Humidity control", icon="mdi:water"),
    SelectEntityDescription(key="night_mode", name="Night mode", icon="mdi:weather-night"),
    SelectEntityDescription(
        key="accent_light_level", name="Accent light", icon="mdi:lightbulb-outline"
    ),
)
DOOR_DELAY_DESCRIPTIONS = (
    SelectEntityDescription(
        key="door_ajar_timeout",
        name="Door open delay",
        icon="mdi:door-open",
        entity_category=EntityCategory.CONFIG,
    ),
)
COOK_MODE_DESCRIPTIONS = (
    SelectEntityDescription(key="cav_cook_mode", name="Cooking mode", icon="mdi:stove"),
    SelectEntityDescription(key="cav2_cook_mode", name="Lower oven cooking mode", icon="mdi:stove"),
)
DISHWASHER_DESCRIPTIONS = (
    SelectEntityDescription(key="wash_cycle", name="Wash cycle", icon="mdi:dishwasher"),
    SelectEntityDescription(key="mode", name="Mode", icon="mdi:dishwasher"),
)
DELAY_START_DESCRIPTIONS = (
    SelectEntityDescription(
        key="delay_start_timer_duration", name="Delay start", icon="mdi:timer-sand"
    ),
)


def _ice_maker_keys(data: dict) -> tuple[str, ...]:
    if is_ice_maker(data):
        return ("ice_maker_on",) if "ice_maker_on" in data else ()
    if is_fridge(data) and "ice_maker_on" in data:
        return tuple(k for k in ICE_KEYS if k in data)
    return ()


async def async_setup_entry(
    hass: HomeAssistant, entry: SubZeroConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    def control_supported(coordinator, description) -> bool:
        return supports_control(coordinator.data, description.key)

    # Entities write state in this order within one update, and automations can observe it.
    for descriptions, entity_class, supported in (
        (AUTO_SENSITIVITY_DESCRIPTIONS, SubZeroEnumSelect, control_supported),
        (
            ICE_MAKER_DESCRIPTIONS,
            SubZeroIceMakerSelect,
            lambda coordinator, _: bool(_ice_maker_keys(coordinator.data)),
        ),
        (
            OPERATING_MODE_DESCRIPTIONS,
            SubZeroOperatingModeSelect,
            lambda coordinator, _: bool(operating_mode_keys(coordinator.data)),
        ),
        (FRIDGE_ENUM_DESCRIPTIONS, SubZeroFridgeEnumSelect, control_supported),
        (DOOR_DELAY_DESCRIPTIONS, SubZeroEnumSelect, control_supported),
        (COOK_MODE_DESCRIPTIONS, SubZeroCookModeSelect, control_supported),
        (DISHWASHER_DESCRIPTIONS, SubZeroDishwasherSelect, control_supported),
        (DELAY_START_DESCRIPTIONS, SubZeroEnumSelect, control_supported),
    ):
        async_setup_entities(entry, async_add_entities, descriptions, entity_class, supported)


class _SubZeroSelect(SubZeroEntity, SelectEntity):
    def _validate_option(self, option: str) -> None:
        if option not in self.options:
            raise ServiceValidationError("The appliance does not support that option.")


class SubZeroIceMakerSelect(_SubZeroSelect):
    @property
    def options(self) -> list[str]:
        keys = _ice_maker_keys(self.coordinator.data)
        return ["Off", "On", *(name for name, key in ICE_MODES.items() if key in keys)]

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        keys = _ice_maker_keys(data)
        return (
            self.coordinator.last_update_success
            and bool(keys)
            and all(type(data[key]) is bool for key in keys)
        )

    @property
    def current_option(self) -> str | None:
        return ice_mode(self.coordinator.data) if self.available else None

    async def async_select_option(self, option: str) -> None:
        self._validate_option(option)
        await self.coordinator.async_set_ice_mode(option)


class SubZeroOperatingModeSelect(_SubZeroSelect):
    @property
    def options(self) -> list[str]:
        keys = operating_mode_keys(self.coordinator.data)
        return ["Normal", *(name for name, key in MODES.items() if key in keys)]

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        keys = operating_mode_keys(data)
        return (
            self.coordinator.last_update_success
            and bool(keys)
            and all(type(data[key]) is bool for key in keys)
        )

    @property
    def current_option(self) -> str | None:
        if not self.available:
            return None
        data = self.coordinator.data
        active = [name for name, key in MODES.items() if data.get(key) is True]
        return active[0] if len(active) == 1 else "Normal" if not active else None

    async def async_select_option(self, option: str) -> None:
        self._validate_option(option)
        await self.coordinator.async_set_operating_mode(MODES.get(option))


class SubZeroEnumSelect(_SubZeroSelect):
    @property
    def _options_map(self) -> dict[str, int]:
        return ENUM_OPTIONS[self.entity_description.key]

    @property
    def _reported_values(self) -> Collection[int]:
        return self._options_map.values()

    @property
    def _state_data(self) -> dict:
        return self.coordinator.data

    def _option_label(self, value: int) -> str | None:
        return next(
            (name for name, candidate in self._options_map.items() if value == candidate), None
        )

    def _option_properties(self, option: str) -> dict[str, bool | int]:
        return {self.entity_description.key: self._options_map[option]}

    @property
    def options(self) -> list[str]:
        return list(self._options_map)

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        key = self.entity_description.key
        return (
            self.coordinator.last_update_success
            and supports_control(data, key)
            and is_int(data[key])
            and data[key] in self._reported_values
        )

    @property
    def current_option(self) -> str | None:
        if not self.available:
            return None
        return self._option_label(self._state_data[self.entity_description.key])

    async def async_select_option(self, option: str) -> None:
        self._validate_option(option)
        await self.coordinator.async_set_properties(self._option_properties(option))


class SubZeroFridgeEnumSelect(SubZeroEnumSelect):
    @property
    def _reported_values(self) -> Collection[int]:
        return enum_labels(self.entity_description.key)

    def _option_label(self, value: int) -> str | None:
        return enum_labels(self.entity_description.key)[value]

    def _option_properties(self, option: str) -> dict[str, bool | int]:
        key = self.entity_description.key
        if option not in self._options_map:
            raise ServiceValidationError("This setting can only be chosen at the appliance.")
        options = (
            accent_light_options(self.coordinator.data)
            if key == "accent_light_level"
            else self._options_map
        )
        return {key: options[option]}

    @property
    def options(self) -> list[str]:
        key = self.entity_description.key
        names = list(self._options_map)
        reported = enum_labels(key).get(self.coordinator.data.get(key))
        return names if reported is None or reported in names else [*names, reported]


class SubZeroCookModeSelect(SubZeroEnumSelect):
    @property
    def _state_data(self) -> dict:
        return self.coordinator.start_state

    def _option_properties(self, option: str) -> dict[str, bool | int]:
        key = self.entity_description.key
        if option == "Off":
            return {key.replace("cook_mode", "unit_on"): False}
        return {key: COOK_MODES[option]}

    @property
    def options(self) -> list[str]:
        data = self.coordinator.data
        key = self.entity_description.key
        return [
            name
            for name, value in COOK_MODES.items()
            if value == data.get(key)
            or (value not in MANUAL_COOK_MODES and cook_mode_offered(data, key, value))
        ]


class SubZeroDishwasherSelect(SubZeroEnumSelect):
    @property
    def _reported_values(self) -> Collection[int]:
        if self.entity_description.key == "wash_cycle":
            return WASH_CYCLES
        return DISHWASHER_MODES.values()

    @property
    def available(self) -> bool:
        return super().available and wash_settings_enabled(self.coordinator.data)
