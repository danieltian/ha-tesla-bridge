"""Selects: seat heaters/coolers, wheel heat, cabin overheat protection; Powerwall modes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from tesla_fleet_api.const import EnergyExportMode, EnergyOperationMode, Seat

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TeslaBridgeConfigEntry
from .entity import EnergySiteEntity, VehicleStreamEntity
from .helpers import as_int, enum_state
from .models import EnergySiteData, VehicleData
from .vehicle import run_command, run_energy_command

PARALLEL_UPDATES = 0

LEVELS = ["off", "low", "medium", "high"]
COOLER_LEVELS = ["off", "low", "medium", "high"]


@dataclass(frozen=True, kw_only=True)
class LevelSelectDescription(SelectEntityDescription):
    """A 0-3 level streamed as an int, set with a command."""

    field: str
    command: Callable[[Any, int], Awaitable[dict[str, Any]]]
    # Wire value per option (defaults to the option's index).
    levels: tuple[int, ...] | None = None


def _seat(position: Seat) -> Callable[[Any, int], Awaitable[dict[str, Any]]]:
    return lambda api, level: api.remote_seat_heater_request(position, level)


def _cooler(position: Seat) -> Callable[[Any, int], Awaitable[dict[str, Any]]]:
    # Cooler positions are 1-indexed (front-left=1), unlike the heater Seat enum.
    return lambda api, level: api.remote_seat_cooler_request(int(position) + 1, level)


LEVEL_SELECTS: tuple[LevelSelectDescription, ...] = (
    LevelSelectDescription(
        key="seat_heater_left",
        field="SeatHeaterLeft",
        command=_seat(Seat.FRONT_LEFT),
        options=LEVELS,
    ),
    LevelSelectDescription(
        key="seat_heater_right",
        field="SeatHeaterRight",
        command=_seat(Seat.FRONT_RIGHT),
        options=LEVELS,
    ),
    LevelSelectDescription(
        key="seat_heater_rear_left",
        field="SeatHeaterRearLeft",
        command=_seat(Seat.REAR_LEFT),
        options=LEVELS,
        entity_registry_enabled_default=False,
    ),
    LevelSelectDescription(
        key="seat_heater_rear_center",
        field="SeatHeaterRearCenter",
        command=_seat(Seat.REAR_CENTER),
        options=LEVELS,
        entity_registry_enabled_default=False,
    ),
    LevelSelectDescription(
        key="seat_heater_rear_right",
        field="SeatHeaterRearRight",
        command=_seat(Seat.REAR_RIGHT),
        options=LEVELS,
        entity_registry_enabled_default=False,
    ),
    LevelSelectDescription(
        key="seat_cooler_left",
        field="ClimateSeatCoolingFrontLeft",
        command=_cooler(Seat.FRONT_LEFT),
        options=COOLER_LEVELS,
        entity_registry_enabled_default=False,
    ),
    LevelSelectDescription(
        key="seat_cooler_right",
        field="ClimateSeatCoolingFrontRight",
        command=_cooler(Seat.FRONT_RIGHT),
        options=COOLER_LEVELS,
        entity_registry_enabled_default=False,
    ),
    LevelSelectDescription(
        key="steering_wheel_heat_level",
        field="HvacSteeringWheelHeatLevel",
        command=lambda api, level: api.remote_steering_wheel_heat_level_request(level),
        # The wheel has no medium setting: Tesla accepts only 0, 1 and 3.
        options=["off", "low", "high"],
        levels=(0, 1, 3),
        entity_registry_enabled_default=False,
    ),
)


class LevelSelect(VehicleStreamEntity, SelectEntity):
    """Seat heater, seat cooler or steering wheel level."""

    entity_description: LevelSelectDescription

    def __init__(
        self, vehicle: VehicleData, description: LevelSelectDescription
    ) -> None:
        self.entity_description = description
        self._attr_options = list(description.options or LEVELS)
        levels = description.levels or tuple(range(len(self._attr_options)))
        self._levels = dict(zip(self._attr_options, levels, strict=True))
        self._options_by_level = {v: k for k, v in self._levels.items()}
        super().__init__(vehicle, description.key, (description.field,))

    @callback
    def _async_update_attrs(self) -> None:
        level = as_int(self.get(self.entity_description.field))
        if level is None:
            self._attr_current_option = None
            return
        # Map unexpected wire values (e.g. a wheel level 2) to the next lower option.
        known = [lvl for lvl in self._options_by_level if lvl <= level]
        self._attr_current_option = (
            self._options_by_level[max(known)] if known else None
        )

    async def async_select_option(self, option: str) -> None:
        level = self._levels[option]
        desc = self.entity_description
        # Seat and wheel heat only work while the cabin climate is on.
        if level and enum_state(self.get("HvacPower"), "HvacPowerState") in (
            None,
            "off",
        ):
            await run_command(self.vehicle, self.vehicle.api.auto_conditioning_start)
            self.stream.set_override("HvacPower", "HvacPowerStateOn")
        await run_command(self.vehicle, lambda: desc.command(self.vehicle.api, level))
        self.stream.set_override(desc.field, level)


COP_OPTIONS = ["off", "on", "fan_only"]


class CabinOverheatProtection(VehicleStreamEntity, SelectEntity):
    """Cabin overheat protection mode."""

    _attr_options = COP_OPTIONS
    _attr_entity_registry_enabled_default = False

    def __init__(self, vehicle: VehicleData) -> None:
        super().__init__(
            vehicle, "cabin_overheat_protection", ("CabinOverheatProtectionMode",)
        )

    @callback
    def _async_update_attrs(self) -> None:
        self._attr_current_option = enum_state(
            self.get("CabinOverheatProtectionMode"), "CabinOverheatProtectionModeState"
        )

    async def async_select_option(self, option: str) -> None:
        await run_command(
            self.vehicle,
            lambda: self.vehicle.api.set_cabin_overheat_protection(
                option != "off", option == "fan_only"
            ),
        )
        stream_value = {"off": "Off", "on": "On", "fan_only": "FanOnly"}[option]
        self.stream.set_override(
            "CabinOverheatProtectionMode",
            f"CabinOverheatProtectionModeState{stream_value}",
        )


# ---- energy site ---------------------------------------------------------------


class OperationModeSelect(EnergySiteEntity[dict], SelectEntity):
    """Powerwall operation mode."""

    _attr_options = [m.value for m in EnergyOperationMode]
    follow_local = True

    def __init__(self, site: EnergySiteData) -> None:
        super().__init__(site, site.info, "operation_mode")

    @callback
    def _async_update_attrs(self) -> None:
        local = self.site.local.data if self.site.local else None
        mode = (local.operation_mode if local else None) or (
            self.coordinator.data or {}
        ).get("default_real_mode")
        mode = self._confirmed(mode)
        self._attr_current_option = mode if mode in self._attr_options else None

    async def async_select_option(self, option: str) -> None:
        await run_energy_command(self.site.api.operation(option))
        self._set_pending(option)
        self._attr_current_option = option
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()


class ExportRuleSelect(EnergySiteEntity[dict], SelectEntity):
    """What may be exported to the grid."""

    _attr_options = [m.value for m in EnergyExportMode]

    def __init__(self, site: EnergySiteData) -> None:
        super().__init__(site, site.info, "export_rule")

    @callback
    def _async_update_attrs(self) -> None:
        data = self.coordinator.data or {}
        rule = data.get("components_customer_preferred_export_rule")
        if rule is None and data.get("components_non_export_configured"):
            rule = EnergyExportMode.NEVER.value
        rule = self._confirmed(rule)
        self._attr_current_option = rule if rule in self._attr_options else None

    async def async_select_option(self, option: str) -> None:
        await run_energy_command(
            self.site.api.grid_import_export(customer_preferred_export_rule=option)
        )
        self._set_pending(option)
        self._attr_current_option = option
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TeslaBridgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    data = entry.runtime_data
    entities: list[SelectEntity] = []
    for vehicle in data.vehicles:
        entities += [LevelSelect(vehicle, d) for d in LEVEL_SELECTS]
        entities.append(CabinOverheatProtection(vehicle))
    for site in data.energy_sites:
        if site.has_battery:
            entities.append(OperationModeSelect(site))
            if site.has_solar:
                entities.append(ExportRuleSelect(site))
    async_add_entities(entities)
