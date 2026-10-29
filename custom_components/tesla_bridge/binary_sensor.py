"""Binary sensors: car states from the stream, Powerwall, and app health."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TeslaBridgeConfigEntry
from .entity import BridgeAppEntity, EnergySiteEntity, VehicleStreamEntity
from .helpers import as_bool, door_open, enum_state, window_open
from .models import EnergySiteData, VehicleData
from .stream import VehicleStream

PARALLEL_UPDATES = 0


def _charge_state(stream: VehicleStream) -> str | None:
    return enum_state(stream.get("DetailedChargeState"), "DetailedChargeState")


def _door(key: str) -> Callable[[VehicleStream], bool | None]:
    return lambda stream: door_open(stream.get("DoorState"), key)


def _window(field: str) -> Callable[[VehicleStream], bool | None]:
    return lambda stream: window_open(stream.get(field))


def _tire_warning(field: str, tire: str) -> Callable[[VehicleStream], bool | None]:
    def value(stream: VehicleStream) -> bool | None:
        raw = stream.get(field)
        return bool(raw.get(tire)) if isinstance(raw, dict) else None

    return value


@dataclass(frozen=True, kw_only=True)
class VehicleBinaryDescription(BinarySensorEntityDescription):
    """A binary state computed from streamed fields."""

    fields: tuple[str, ...]
    value_fn: Callable[[VehicleStream], bool | None]


VEHICLE_BINARY: tuple[VehicleBinaryDescription, ...] = (
    VehicleBinaryDescription(
        key="online",
        fields=("__connected__",),
        value_fn=lambda s: s.connected,
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VehicleBinaryDescription(
        key="charging",
        fields=("DetailedChargeState",),
        value_fn=lambda s: (
            None
            if _charge_state(s) is None
            else _charge_state(s) in ("charging", "starting")
        ),
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
    ),
    VehicleBinaryDescription(
        key="charger_connected",
        fields=("DetailedChargeState",),
        value_fn=lambda s: (
            None if _charge_state(s) is None else _charge_state(s) != "disconnected"
        ),
        device_class=BinarySensorDeviceClass.PLUG,
    ),
    VehicleBinaryDescription(
        key="charge_port_door",
        fields=("ChargePortDoorOpen",),
        value_fn=lambda s: as_bool(s.get("ChargePortDoorOpen")),
        device_class=BinarySensorDeviceClass.OPENING,
    ),
    *(
        VehicleBinaryDescription(
            key=key,
            fields=("DoorState",),
            value_fn=_door(key),
            device_class=BinarySensorDeviceClass.DOOR
            if key.startswith("door")
            else BinarySensorDeviceClass.OPENING,
        )
        for key in (
            "door_driver_front",
            "door_passenger_front",
            "door_driver_rear",
            "door_passenger_rear",
            "frunk",
            "trunk",
        )
    ),
    *(
        VehicleBinaryDescription(
            key=f"window_{name}",
            fields=(field,),
            value_fn=_window(field),
            device_class=BinarySensorDeviceClass.WINDOW,
        )
        for name, field in (
            ("driver_front", "FdWindow"),
            ("passenger_front", "FpWindow"),
            ("driver_rear", "RdWindow"),
            ("passenger_rear", "RpWindow"),
        )
    ),
    VehicleBinaryDescription(
        key="preconditioning",
        fields=("PreconditioningEnabled",),
        value_fn=lambda s: as_bool(s.get("PreconditioningEnabled")),
        entity_registry_enabled_default=False,
    ),
    VehicleBinaryDescription(
        key="scheduled_charging_pending",
        fields=("ScheduledChargingPending",),
        value_fn=lambda s: as_bool(s.get("ScheduledChargingPending")),
        entity_registry_enabled_default=False,
    ),
    VehicleBinaryDescription(
        key="battery_heater",
        fields=("BatteryHeaterOn",),
        value_fn=lambda s: as_bool(s.get("BatteryHeaterOn")),
        device_class=BinarySensorDeviceClass.HEAT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    VehicleBinaryDescription(
        key="homelink_nearby",
        fields=("HomelinkNearby",),
        value_fn=lambda s: as_bool(s.get("HomelinkNearby")),
        entity_registry_enabled_default=False,
    ),
    VehicleBinaryDescription(
        key="located_at_home",
        fields=("LocatedAtHome",),
        value_fn=lambda s: as_bool(s.get("LocatedAtHome")),
        device_class=BinarySensorDeviceClass.PRESENCE,
        entity_registry_enabled_default=False,
    ),
    VehicleBinaryDescription(
        key="driver_seat_occupied",
        fields=("DriverSeatOccupied",),
        value_fn=lambda s: as_bool(s.get("DriverSeatOccupied")),
        device_class=BinarySensorDeviceClass.OCCUPANCY,
        entity_registry_enabled_default=False,
    ),
    *(
        VehicleBinaryDescription(
            key=f"tpms_{level}_warning_{short}",
            fields=(field,),
            value_fn=_tire_warning(field, tire),
            device_class=BinarySensorDeviceClass.PROBLEM,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        for level, field in (("soft", "TpmsSoftWarnings"), ("hard", "TpmsHardWarnings"))
        for short, tire in (
            ("fl", "frontLeft"),
            ("fr", "frontRight"),
            ("rl", "rearLeft"),
            ("rr", "rearRight"),
        )
    ),
)


class VehicleBinarySensor(VehicleStreamEntity, BinarySensorEntity):
    """A streamed boolean."""

    entity_description: VehicleBinaryDescription

    def __init__(
        self, vehicle: VehicleData, description: VehicleBinaryDescription
    ) -> None:
        self.entity_description = description
        super().__init__(vehicle, description.key, description.fields)

    @callback
    def _async_update_attrs(self) -> None:
        self._attr_is_on = self.entity_description.value_fn(self.stream)


# ---- energy site ---------------------------------------------------------------


class GridConnectedSensor(EnergySiteEntity[Any], BinarySensorEntity):
    """On while the home is connected to the grid."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, site: EnergySiteData) -> None:
        super().__init__(site, site.local or site.live, "grid_connected")

    @callback
    def _async_update_attrs(self) -> None:
        if self.site.local is not None:
            data = self.site.local.data
            self._attr_is_on = (
                None if data is None else data.grid_status == "SystemGridConnected"
            )
        else:
            status = (self.coordinator.data or {}).get("island_status")
            self._attr_is_on = None if status is None else status == "on_grid"


class GridServicesSensor(EnergySiteEntity[Any], BinarySensorEntity):
    """Grid services (e.g. a VPP event) active."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, site: EnergySiteData) -> None:
        super().__init__(site, site.local or site.live, "grid_services_active")

    @callback
    def _async_update_attrs(self) -> None:
        if self.site.local is not None:
            data = self.site.local.data
            self._attr_is_on = None if data is None else data.grid_services_active
        else:
            self._attr_is_on = as_bool(
                (self.coordinator.data or {}).get("grid_services_active")
            )


class BatteryChargingSensor(EnergySiteEntity[Any], BinarySensorEntity):
    """The Powerwall is charging (negative battery power)."""

    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(self, site: EnergySiteData) -> None:
        super().__init__(site, site.local or site.live, "battery_charging")

    @callback
    def _async_update_attrs(self) -> None:
        if self.site.local is not None:
            data = self.site.local.data
            meter = data.meters.get("battery") if data else None
            power = None if meter is None else meter.instant_power
        else:
            power = (self.coordinator.data or {}).get("battery_power")
        self._attr_is_on = None if power is None else power < -50


# ---- app -----------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class BridgeBinaryDescription(BinarySensorEntityDescription):
    """An app health flag."""

    value_fn: Callable[[dict[str, Any]], bool | None]


BRIDGE_BINARY: tuple[BridgeBinaryDescription, ...] = (
    BridgeBinaryDescription(
        key="telemetry_server",
        value_fn=lambda s: (s.get("telemetry") or {}).get("running"),
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BridgeBinaryDescription(
        key="key_published",
        value_fn=lambda s: ((s.get("key_server") or {}).get("public_key") or {}).get(
            "served"
        ),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BridgeBinaryDescription(
        key="dns_updated",
        value_fn=lambda s: (s.get("duckdns") or {}).get("ok"),
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BridgeBinaryDescription(
        key="loopback",
        value_fn=lambda s: (
            (s.get("selftest") or {}).get("telemetry_loopback") or {}
        ).get("ok"),
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


class BridgeBinarySensor(BridgeAppEntity, BinarySensorEntity):
    """App health flag."""

    entity_description: BridgeBinaryDescription

    def __init__(
        self, entry: TeslaBridgeConfigEntry, description: BridgeBinaryDescription
    ) -> None:
        self.entity_description = description
        super().__init__(entry.runtime_data.bridge, description.key, entry.entry_id)

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.coordinator.data or {})


class BridgeStreamSensor(BinarySensorEntity):
    """Is Home Assistant receiving the live stream from the app?"""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "stream_connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: TeslaBridgeConfigEntry) -> None:
        bridge = entry.runtime_data.bridge
        self._hub = bridge.hub
        self._attr_unique_id = f"{entry.entry_id}-bridge-stream_connected"
        self._attr_device_info = bridge.device

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            self._hub.async_add_hub_listener(self.async_write_ha_state)
        )

    @property
    def is_on(self) -> bool:
        return self._hub.bridge_connected


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TeslaBridgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    data = entry.runtime_data
    entities: list[BinarySensorEntity] = [
        VehicleBinarySensor(vehicle, description)
        for vehicle in data.vehicles
        for description in VEHICLE_BINARY
    ]
    for site in data.energy_sites:
        entities.append(GridConnectedSensor(site))
        if site.has_battery:
            entities += [GridServicesSensor(site), BatteryChargingSensor(site)]
    entities.extend(BridgeBinarySensor(entry, d) for d in BRIDGE_BINARY)
    entities.append(BridgeStreamSensor(entry))
    async_add_entities(entities)
