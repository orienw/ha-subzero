"""Entity discovery, device identity, and availability."""

from collections.abc import Callable
from functools import partial

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SubZeroConfigEntry
from .controls import excluded_entity_keys, has_left_and_right_ovens
from .coordinator import SubZeroCoordinator


def zone_name(data: dict, key: str, names: dict[str, str | None]) -> str | None:
    """The app's name for a cavity or zone entity, when the appliance's layout changes it.

    Two cavities are upper and lower ovens, or right and left on a range, and two wine zones are
    upper and lower.
    """
    cavity = key.removeprefix("remote_start_")
    if cavity.startswith(("cav_", "cav2_")) and data.get("cav2_set_temp") is not None:
        second = cavity.startswith("cav2_")
        if second and not has_left_and_right_ovens(data):
            return None
        name = names.get(key if second else key.replace("cav_", "cav2_", 1))
        side = ("left" if second else "right") if has_left_and_right_ovens(data) else "upper"
        if name and "ower oven" in name:
            return name.replace("Lower oven", f"{side.title()} oven").replace(
                "lower oven", f"{side} oven"
            )
        return None
    if cavity.startswith(("wine_", "wine2_")) and cavity != "wine_temp_alert_on":
        door = cavity.endswith("_door_ajar")
        if ("wine2_door_ajar" if door else "wine2_set_temp") not in data:
            return None
        base = (names.get(key) or "").removesuffix(" 2")
        zone = "Lower" if cavity.startswith("wine2_") else "Upper"
        return f"{zone} {base[0].lower()}{base[1:]}" if base else None
    return None


class SubZeroEntity(CoordinatorEntity[SubZeroCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: SubZeroCoordinator, description: EntityDescription):
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.device_id}_{description.key}"
        # The platform's entity names by key, which cavity and zone names are built from.
        self.zone_names: dict[str, str | None] = {}
        self._zone_name: str | None = None

    def update_zone_name(self) -> None:
        """Name a cavity or zone entity as the app does for the appliance's layout.

        A second cavity or zone can be reported after the first one's entities exist.
        Renaming keeps the entity ID.
        """
        name = zone_name(self.coordinator.data, self.entity_description.key, self.zone_names)
        if name == self._zone_name:
            return
        self._zone_name = name
        if name is None:
            del self._attr_name
        else:
            self._attr_name = name

    @callback
    def _handle_coordinator_update(self) -> None:
        self.update_zone_name()
        super()._handle_coordinator_update()

    @property
    def device_info(self) -> DeviceInfo:
        return self.coordinator.device_info

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.entity_description.key in self.coordinator.data
            and self.entity_description.key not in excluded_entity_keys(self.coordinator.data)
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
    names = {description.key: description.name for description in descriptions}

    @callback
    def discover(coordinator: SubZeroCoordinator) -> None:
        entities = []
        for description in descriptions:
            key = (coordinator.device_id, description.key)
            if (
                key not in discovered
                and description.key not in excluded_entity_keys(coordinator.data)
                and supported(coordinator, description)
            ):
                discovered.add(key)
                entity = entity_class(coordinator, description)
                entity.zone_names = names
                entity.update_zone_name()
                entities.append(entity)
        if entities:
            async_add_entities(entities)

    for coordinator in entry.runtime_data.coordinators.values():
        discover_device = partial(discover, coordinator)
        discover_device()
        entry.async_on_unload(coordinator.async_add_listener(discover_device))
