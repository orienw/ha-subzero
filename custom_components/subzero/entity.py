"""Device identity and availability shared by an appliance's entities."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .controls import is_dishwasher, is_oven
from .coordinator import SubZeroCoordinator


class SubZeroEntity(CoordinatorEntity[SubZeroCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: SubZeroCoordinator, description: EntityDescription):
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.device_id}_{description.key}"
        version = coordinator.data.get("version")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.device_id)},
            name=coordinator.device["name"],
            manufacturer=(
                "Cove"
                if is_dishwasher(coordinator.data)
                else "Wolf"
                if is_oven(coordinator.data)
                else "Sub-Zero"
            ),
            model=coordinator.data.get("appliance_model"),
            sw_version=version.get("fw") if isinstance(version, dict) else None,
        )

    @property
    def available(self) -> bool:
        return (
            super().available and self.coordinator.data.get(self.entity_description.key) is not None
        )
