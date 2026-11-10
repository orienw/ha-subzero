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
    supports_air_filter_reset,
    supports_control,
    validate_remote_start,
    wash_cancel_enabled,
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
    async_setup_entities(
        entry,
        async_add_entities,
        tuple(
            ButtonEntityDescription(
                key=f"dismiss_{prefix}", name=name, icon="mdi:timer-off-outline"
            )
            for prefix, name in (
                ("kitchen_timer", "Dismiss kitchen timer"),
                ("kitchen_timer2", "Dismiss kitchen timer 2"),
            )
        ),
        SubZeroTimerDismissButton,
        lambda coordinator, description: supports_control(
            coordinator.data, f"{description.key.removeprefix('dismiss_')}_duration"
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
            validate_remote_start(self.coordinator.start_state, key)
        except ServiceValidationError:
            return False
        return supports_control(data, key)

    async def async_press(self) -> None:
        await self.coordinator.async_start(
            self.entity_description.key.removeprefix("remote_start_")
        )


class SubZeroCancelWashButton(SubZeroEntity, ButtonEntity):
    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and supports_control(self.coordinator.data, "wash_cycle_on")
            and wash_cancel_enabled(self.coordinator.data)
        )

    async def async_press(self) -> None:
        await self.coordinator.async_cancel_wash()


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


class SubZeroTimerDismissButton(SubZeroEntity, ButtonEntity):
    @property
    def available(self) -> bool:
        prefix = self.entity_description.key.removeprefix("dismiss_")
        return (
            self.coordinator.last_update_success
            and supports_control(self.coordinator.data, f"{prefix}_duration")
            and (
                self.coordinator.data.get(f"{prefix}_active") is True
                or self.coordinator.data.get(f"{prefix}_complete") is True
            )
        )

    async def async_press(self) -> None:
        prefix = self.entity_description.key.removeprefix("dismiss_")
        await self.coordinator.async_dismiss_timer(
            f"{prefix}_duration",
            running=self.coordinator.data.get(f"{prefix}_complete") is not True,
        )
