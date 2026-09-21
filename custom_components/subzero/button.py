"""Remote starts and air-filter maintenance."""

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SubZeroConfigEntry
from .const import ICE_DELAY_KEYS
from .controls import (
    is_ice_maker,
    start_properties,
    supports_air_filter_reset,
    supports_control,
    validate_remote_start,
)
from .coordinator import SubZeroCoordinator
from .entity import SubZeroEntity, async_setup_entities

DESCRIPTIONS = tuple(
    ButtonEntityDescription(key=f"remote_start_{key}", name=name, icon="mdi:play-circle")
    for key, name in (
        ("cav_unit_on", "Start oven"),
        ("cav2_unit_on", "Start lower oven"),
        ("wash_cycle_on", "Start wash cycle"),
    )
)


async def async_setup_entry(
    hass: HomeAssistant, entry: SubZeroConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    def supported(coordinator: SubZeroCoordinator, description: EntityDescription) -> bool:
        unit_key = description.key.removeprefix("remote_start_")
        ready_key = (
            "remote_ready"
            if unit_key == "wash_cycle_on"
            else unit_key.replace("unit_on", "remote_ready")
        )
        return supports_control(coordinator.data, unit_key) and ready_key in coordinator.data

    async_setup_entities(entry, async_add_entities, DESCRIPTIONS, SubZeroStartButton, supported)
    async_setup_entities(
        entry,
        async_add_entities,
        (
            ButtonEntityDescription(
                key="cancel_wash_cycle", name="Cancel wash cycle", icon="mdi:stop-circle"
            ),
        ),
        SubZeroCancelWashButton,
        lambda coordinator, description: supports_control(coordinator.data, "wash_cycle_on"),
    )
    async_setup_entities(
        entry,
        async_add_entities,
        (
            ButtonEntityDescription(
                key="reset_air_filter",
                name="Reset air filter",
                icon="mdi:air-filter",
                entity_category=EntityCategory.CONFIG,
            ),
        ),
        SubZeroAirFilterResetButton,
        lambda coordinator, description: supports_air_filter_reset(coordinator.data),
    )
    async_setup_entities(
        entry,
        async_add_entities,
        (
            ButtonEntityDescription(
                key="end_ice_delay", name="End current ice delay", icon="mdi:timer-off-outline"
            ),
            ButtonEntityDescription(
                key="cancel_ice_delay", name="Cancel ice delay schedule", icon="mdi:calendar-remove"
            ),
        ),
        SubZeroIceDelayButton,
        lambda coordinator, description: (
            is_ice_maker(coordinator.data) and ICE_DELAY_KEYS.issubset(coordinator.data)
        ),
    )


class SubZeroStartButton(SubZeroEntity, ButtonEntity):
    @property
    def available(self) -> bool:
        data = self.coordinator.data
        key = self.entity_description.key.removeprefix("remote_start_")
        if not self.coordinator.last_update_success or data.get(key) is not False:
            return False
        try:
            validate_remote_start(data, key)
        except ServiceValidationError:
            return False
        return supports_control(data, key)

    async def async_press(self) -> None:
        key = self.entity_description.key.removeprefix("remote_start_")
        await self.coordinator.async_set_properties(
            start_properties(self.coordinator.data, key), force=True
        )


class SubZeroCancelWashButton(SubZeroEntity, ButtonEntity):
    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and supports_control(self.coordinator.data, "wash_cycle_on")
            and self.coordinator.data.get("wash_cycle_on") is True
        )

    async def async_press(self) -> None:
        await self.coordinator.async_set_properties({"wash_cycle_on": False})


class SubZeroAirFilterResetButton(SubZeroEntity, ButtonEntity):
    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and supports_air_filter_reset(
            self.coordinator.data
        )

    async def async_press(self) -> None:
        await self.coordinator.async_reset_air_filter()


class SubZeroIceDelayButton(SubZeroEntity, ButtonEntity):
    @property
    def available(self) -> bool:
        data = self.coordinator.data
        if not self.coordinator.last_update_success or not is_ice_maker(data):
            return False
        if not ICE_DELAY_KEYS.issubset(data):
            return False
        if self.entity_description.key == "end_ice_delay":
            return data.get("delay_active") is True
        return type(data.get("delay_duration")) is int and data["delay_duration"] > 0

    async def async_press(self) -> None:
        await self.coordinator.async_set_ice_delay(
            end_current=self.entity_description.key == "end_ice_delay"
        )
