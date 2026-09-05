"""Device identity and availability shared by the fridge's entities."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
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
            name=coordinator.config_entry.title,
            manufacturer="Sub-Zero",
            model=coordinator.data.get("appliance_model"),
            sw_version=version.get("fw") if isinstance(version, dict) else None,
        )

    @property
    def available(self) -> bool:
        return (
            super().available and self.coordinator.data.get(self.entity_description.key) is not None
        )
