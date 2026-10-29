"""Vehicle location and navigation destination."""

from __future__ import annotations

from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TeslaBridgeConfigEntry
from .entity import VehicleStreamEntity
from .helpers import as_float, location
from .models import VehicleData

PARALLEL_UPDATES = 0


class VehicleLocation(VehicleStreamEntity, TrackerEntity):
    """Where the car is (streamed, so updates while driving)."""

    def __init__(self, vehicle: VehicleData) -> None:
        super().__init__(vehicle, "location", ("Location", "GpsHeading"))

    @callback
    def _async_update_attrs(self) -> None:
        coords = location(self.get("Location"))
        self._attr_latitude, self._attr_longitude = coords if coords else (None, None)
        heading = as_float(self.get("GpsHeading"))
        self._attr_extra_state_attributes = (
            {"heading": round(heading)} if heading is not None else {}
        )


class RouteDestination(VehicleStreamEntity, TrackerEntity):
    """The active navigation destination."""

    _attr_entity_registry_enabled_default = False

    def __init__(self, vehicle: VehicleData) -> None:
        super().__init__(vehicle, "route", ("DestinationLocation", "DestinationName"))

    @callback
    def _async_update_attrs(self) -> None:
        coords = (
            location(self.get("DestinationLocation"))
            if self.get("DestinationName")
            else None
        )
        self._attr_latitude, self._attr_longitude = coords if coords else (None, None)
        self._attr_location_name = self.get("DestinationName") or None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TeslaBridgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        entity
        for vehicle in entry.runtime_data.vehicles
        for entity in (VehicleLocation(vehicle), RouteDestination(vehicle))
    )
