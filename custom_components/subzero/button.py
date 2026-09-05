"""Remote starts using the appliance's physical Remote Ready interlock."""

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SubZeroConfigEntry
from .controls import supports_control, validate_remote_start
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
        await self.coordinator.async_set_properties(
            {self.entity_description.key.removeprefix("remote_start_"): True}
        )
