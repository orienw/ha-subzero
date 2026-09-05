"""Entity discovery, device identity, and availability."""

from collections.abc import Callable
from functools import partial

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SubZeroConfigEntry
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


@callback
def async_setup_entities(
    entry: SubZeroConfigEntry,
    async_add_entities: AddEntitiesCallback,
    descriptions: tuple[EntityDescription, ...],
    entity_class: type[SubZeroEntity],
    supported: Callable[[SubZeroCoordinator, EntityDescription], bool],
) -> None:
    """Discover entities at setup and when their appliance reports new capabilities."""
    discovered: set[tuple[str, str]] = set()

    @callback
    def discover(coordinator: SubZeroCoordinator) -> None:
        entities = []
        for description in descriptions:
            key = (coordinator.device_id, description.key)
            if key not in discovered and supported(coordinator, description):
                discovered.add(key)
                entities.append(entity_class(coordinator, description))
        if entities:
            async_add_entities(entities)

    for coordinator in entry.runtime_data.coordinators.values():
        discover_device = partial(discover, coordinator)
        discover_device()
        entry.async_on_unload(coordinator.async_add_listener(discover_device))
