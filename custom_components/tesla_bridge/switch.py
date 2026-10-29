"""Switches: charging, sentry, defrost, heaters; Powerwall storm watch and grid charging."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from tesla_fleet_api.const import AutoSeat

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TeslaBridgeConfigEntry
from .entity import EnergySiteEntity, VehicleStreamEntity
from .helpers import as_bool, as_int, enum_state
from .models import EnergySiteData, VehicleData
from .stream import VehicleStream
from .vehicle import run_command, run_energy_command

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class VehicleSwitchDescription(SwitchEntityDescription):
    """A streamed state with an on/off command."""

    fields: tuple[str, ...]
    value_fn: Callable[[VehicleStream], bool | None]
    on_fn: Callable[[Any], Awaitable[dict[str, Any]]]
    off_fn: Callable[[Any], Awaitable[dict[str, Any]]]
    # (field, value) written optimistically after the on/off command.
    on_override: tuple[str, Any] | None = None
    off_override: tuple[str, Any] | None = None


def _charging(stream: VehicleStream) -> bool | None:
    state = enum_state(stream.get("DetailedChargeState"), "DetailedChargeState")
    return None if state is None else state in ("charging", "starting")


VEHICLE_SWITCHES: tuple[VehicleSwitchDescription, ...] = (
    VehicleSwitchDescription(
        key="charge",
        fields=("DetailedChargeState",),
        value_fn=_charging,
        on_fn=lambda api: api.charge_start(),
        off_fn=lambda api: api.charge_stop(),
        on_override=("DetailedChargeState", "DetailedChargeStateStarting"),
        off_override=("DetailedChargeState", "DetailedChargeStateStopped"),
    ),
    VehicleSwitchDescription(
        key="sentry_mode",
        fields=("SentryMode",),
        value_fn=lambda s: (
            None
            if (st := enum_state(s.get("SentryMode"), "SentryModeState")) is None
            else st != "off"
        ),
        on_fn=lambda api: api.set_sentry_mode(True),
        off_fn=lambda api: api.set_sentry_mode(False),
        on_override=("SentryMode", "SentryModeStateArmed"),
        off_override=("SentryMode", "SentryModeStateOff"),
    ),
    VehicleSwitchDescription(
        key="defrost",
        fields=("DefrostMode",),
        value_fn=lambda s: (
            None
            if (st := enum_state(s.get("DefrostMode"), "DefrostModeState")) is None
            else st == "max"
        ),
        on_fn=lambda api: api.set_preconditioning_max(True, False),
        off_fn=lambda api: api.set_preconditioning_max(False, False),
        on_override=("DefrostMode", "DefrostModeStateMax"),
        off_override=("DefrostMode", "DefrostModeStateOff"),
    ),
    VehicleSwitchDescription(
        key="steering_wheel_heater",
        fields=("HvacSteeringWheelHeatLevel",),
        value_fn=lambda s: (
            None
            if (lvl := as_int(s.get("HvacSteeringWheelHeatLevel"))) is None
            else lvl > 0
        ),
        on_fn=lambda api: api.remote_steering_wheel_heater_request(True),
        off_fn=lambda api: api.remote_steering_wheel_heater_request(False),
    ),
    VehicleSwitchDescription(
        key="auto_steering_wheel_heat",
        fields=("HvacSteeringWheelHeatAuto",),
        value_fn=lambda s: as_bool(s.get("HvacSteeringWheelHeatAuto")),
        on_fn=lambda api: api.remote_auto_steering_wheel_heat_climate_request(True),
        off_fn=lambda api: api.remote_auto_steering_wheel_heat_climate_request(False),
        entity_registry_enabled_default=False,
    ),
    VehicleSwitchDescription(
        key="auto_seat_climate_left",
        fields=("AutoSeatClimateLeft",),
        value_fn=lambda s: as_bool(s.get("AutoSeatClimateLeft")),
        on_fn=lambda api: api.remote_auto_seat_climate_request(
            AutoSeat.FRONT_LEFT, True
        ),
        off_fn=lambda api: api.remote_auto_seat_climate_request(
            AutoSeat.FRONT_LEFT, False
        ),
        on_override=("AutoSeatClimateLeft", True),
        off_override=("AutoSeatClimateLeft", False),
        entity_registry_enabled_default=False,
    ),
    VehicleSwitchDescription(
        key="auto_seat_climate_right",
        fields=("AutoSeatClimateRight",),
        value_fn=lambda s: as_bool(s.get("AutoSeatClimateRight")),
        on_fn=lambda api: api.remote_auto_seat_climate_request(
            AutoSeat.FRONT_RIGHT, True
        ),
        off_fn=lambda api: api.remote_auto_seat_climate_request(
            AutoSeat.FRONT_RIGHT, False
        ),
        on_override=("AutoSeatClimateRight", True),
        off_override=("AutoSeatClimateRight", False),
        entity_registry_enabled_default=False,
    ),
)


class VehicleSwitch(VehicleStreamEntity, SwitchEntity):
    """A streamed on/off state with commands."""

    entity_description: VehicleSwitchDescription

    def __init__(
        self, vehicle: VehicleData, description: VehicleSwitchDescription
    ) -> None:
        self.entity_description = description
        super().__init__(vehicle, description.key, description.fields)

    @callback
    def _async_update_attrs(self) -> None:
        self._attr_is_on = self.entity_description.value_fn(self.stream)

    async def async_turn_on(self, **kwargs: Any) -> None:
        desc = self.entity_description
        await run_command(self.vehicle, lambda: desc.on_fn(self.vehicle.api))
        if desc.on_override:
            self.stream.set_override(*desc.on_override)
        else:
            self._attr_is_on = True
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        desc = self.entity_description
        await run_command(self.vehicle, lambda: desc.off_fn(self.vehicle.api))
        if desc.off_override:
            self.stream.set_override(*desc.off_override)
        else:
            self._attr_is_on = False
            self.async_write_ha_state()


# ---- energy site ---------------------------------------------------------------


class StormWatchSwitch(EnergySiteEntity[dict[str, Any]], SwitchEntity):
    """Storm Watch: charge to full ahead of severe weather."""

    def __init__(self, site: EnergySiteData) -> None:
        super().__init__(site, site.info, "storm_watch")

    @callback
    def _async_update_attrs(self) -> None:
        self._attr_is_on = self._confirmed(
            as_bool(
                (self.coordinator.data or {}).get("user_settings_storm_mode_enabled")
            )
        )

    async def _set(self, enabled: bool) -> None:
        await run_energy_command(self.site.api.storm_mode(enabled))
        self._set_pending(enabled)
        self._attr_is_on = enabled
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class GridChargingSwitch(EnergySiteEntity[dict[str, Any]], SwitchEntity):
    """Allow charging the Powerwall from the grid."""

    def __init__(self, site: EnergySiteData) -> None:
        super().__init__(site, site.info, "allow_grid_charging")

    @callback
    def _async_update_attrs(self) -> None:
        disallow = as_bool(
            (self.coordinator.data or {}).get(
                "components_disallow_charge_from_grid_with_solar_installed"
            )
        )
        self._attr_is_on = self._confirmed(None if disallow is None else not disallow)

    async def _set(self, allow: bool) -> None:
        await run_energy_command(
            self.site.api.grid_import_export(
                disallow_charge_from_grid_with_solar_installed=not allow
            )
        )
        self._set_pending(allow)
        self._attr_is_on = allow
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TeslaBridgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    data = entry.runtime_data
    entities: list[SwitchEntity] = [
        VehicleSwitch(vehicle, description)
        for vehicle in data.vehicles
        for description in VEHICLE_SWITCHES
    ]
    for site in data.energy_sites:
        if site.has_battery:
            entities.append(StormWatchSwitch(site))
            if site.has_solar:
                entities.append(GridChargingSwitch(site))
    async_add_entities(entities)
