"""Windows, charge port door, frunk and trunk."""

from __future__ import annotations

from typing import Any

from tesla_fleet_api.const import Trunk, WindowCommand

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TeslaBridgeConfigEntry
from .entity import VehicleStreamEntity
from .helpers import as_bool, door_open, location, window_open
from .models import VehicleData
from .vehicle import run_command

PARALLEL_UPDATES = 0

WINDOW_FIELDS = ("FdWindow", "FpWindow", "RdWindow", "RpWindow")


class Windows(VehicleStreamEntity, CoverEntity):
    """All windows: vent or close."""

    _attr_device_class = CoverDeviceClass.WINDOW
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE

    def __init__(self, vehicle: VehicleData) -> None:
        super().__init__(vehicle, "windows", (*WINDOW_FIELDS, "Location"))

    @callback
    def _async_update_attrs(self) -> None:
        states = [window_open(self.get(f)) for f in WINDOW_FIELDS]
        known = [s for s in states if s is not None]
        self._attr_is_closed = None if not known else not any(known)

    def _coords(self) -> tuple[float | None, float | None]:
        # Closing requires the car to be near the user; Fleet API checks lat/lon.
        coords = location(self.get("Location"))
        return coords if coords else (None, None)

    async def async_open_cover(self, **kwargs: Any) -> None:
        lat, lon = self._coords()
        await run_command(
            self.vehicle,
            lambda: self.vehicle.api.window_control(WindowCommand.VENT, lat, lon),
        )
        for field in WINDOW_FIELDS:
            self.stream.set_override(field, "WindowStatePartiallyOpen")

    async def async_close_cover(self, **kwargs: Any) -> None:
        lat, lon = self._coords()
        await run_command(
            self.vehicle,
            lambda: self.vehicle.api.window_control(WindowCommand.CLOSE, lat, lon),
        )
        for field in WINDOW_FIELDS:
            self.stream.set_override(field, "WindowStateClosed")


class ChargePortDoor(VehicleStreamEntity, CoverEntity):
    """Charge port door."""

    _attr_device_class = CoverDeviceClass.DOOR
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE

    def __init__(self, vehicle: VehicleData) -> None:
        super().__init__(vehicle, "charge_port_door", ("ChargePortDoorOpen",))

    @callback
    def _async_update_attrs(self) -> None:
        is_open = as_bool(self.get("ChargePortDoorOpen"))
        self._attr_is_closed = None if is_open is None else not is_open

    async def async_open_cover(self, **kwargs: Any) -> None:
        await run_command(self.vehicle, self.vehicle.api.charge_port_door_open)
        self.stream.set_override("ChargePortDoorOpen", True)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await run_command(self.vehicle, self.vehicle.api.charge_port_door_close)
        self.stream.set_override("ChargePortDoorOpen", False)


class Trunk_(VehicleStreamEntity, CoverEntity):
    """Frunk (open only) or trunk (open/close; actuating toggles it)."""

    _attr_device_class = CoverDeviceClass.DOOR

    def __init__(self, vehicle: VehicleData, which: Trunk) -> None:
        key = "frunk" if which == Trunk.FRONT else "trunk"
        super().__init__(vehicle, key, ("DoorState",))
        self._which = which
        self._attr_supported_features = (
            CoverEntityFeature.OPEN
            if which == Trunk.FRONT
            else CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
        )

    @callback
    def _async_update_attrs(self) -> None:
        is_open = door_open(self.get("DoorState"), self.key)
        self._attr_is_closed = None if is_open is None else not is_open

    async def _actuate(self) -> None:
        await run_command(
            self.vehicle, lambda: self.vehicle.api.actuate_trunk(self._which)
        )

    async def async_open_cover(self, **kwargs: Any) -> None:
        if self.is_closed is not False:
            await self._actuate()

    async def async_close_cover(self, **kwargs: Any) -> None:
        if self.is_closed is not True:
            await self._actuate()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TeslaBridgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        entity
        for vehicle in entry.runtime_data.vehicles
        for entity in (
            Windows(vehicle),
            ChargePortDoor(vehicle),
            Trunk_(vehicle, Trunk.FRONT),
            Trunk_(vehicle, Trunk.REAR),
        )
    )
