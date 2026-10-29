"""Door lock and charge-cable lock."""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TeslaBridgeConfigEntry
from .const import DOMAIN
from .entity import VehicleStreamEntity
from .helpers import as_bool, enum_state
from .models import VehicleData
from .vehicle import run_command

PARALLEL_UPDATES = 0


class DoorLock(VehicleStreamEntity, LockEntity):
    """All doors."""

    def __init__(self, vehicle: VehicleData) -> None:
        super().__init__(vehicle, "lock", ("Locked",))

    @callback
    def _async_update_attrs(self) -> None:
        self._attr_is_locked = as_bool(self.get("Locked"))

    async def async_lock(self, **kwargs: Any) -> None:
        await run_command(self.vehicle, self.vehicle.api.door_lock)
        self.stream.set_override("Locked", True)

    async def async_unlock(self, **kwargs: Any) -> None:
        await run_command(self.vehicle, self.vehicle.api.door_unlock)
        self.stream.set_override("Locked", False)


class ChargeCableLock(VehicleStreamEntity, LockEntity):
    """The charge-port latch; unlocking releases the cable."""

    def __init__(self, vehicle: VehicleData) -> None:
        super().__init__(vehicle, "charge_cable_lock", ("ChargePortLatch",))

    @callback
    def _async_update_attrs(self) -> None:
        state = enum_state(self.get("ChargePortLatch"), "ChargePortLatch")
        self._attr_is_locked = None if state is None else state == "engaged"

    async def async_lock(self, **kwargs: Any) -> None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="cable_lock_auto"
        )

    async def async_unlock(self, **kwargs: Any) -> None:
        await run_command(self.vehicle, self.vehicle.api.charge_port_door_open)
        self.stream.set_override("ChargePortLatch", "ChargePortLatchDisengaged")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TeslaBridgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(
        entity
        for vehicle in entry.runtime_data.vehicles
        for entity in (DoorLock(vehicle), ChargeCableLock(vehicle))
    )
