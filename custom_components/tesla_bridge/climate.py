"""Cabin climate."""

from __future__ import annotations

from typing import Any

from tesla_fleet_api.const import ClimateKeeperMode

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_HALVES, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TeslaBridgeConfigEntry
from .entity import VehicleStreamEntity
from .helpers import as_float, enum_state
from .models import VehicleData
from .vehicle import run_command

PARALLEL_UPDATES = 0

# Stream state -> preset name, and preset -> command value.
PRESETS = {
    "off": ClimateKeeperMode.OFF,
    "keep": ClimateKeeperMode.KEEP_MODE,
    "dog": ClimateKeeperMode.DOG_MODE,
    "camp": ClimateKeeperMode.CAMP_MODE,
}
_STREAM_PRESET = {"off": "off", "on": "keep", "dog": "dog", "party": "camp"}


class CabinClimate(VehicleStreamEntity, ClimateEntity):
    """Turn the HVAC on/off, set temperature and climate keeper mode."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT_COOL]
    _attr_preset_modes = list(PRESETS)
    _attr_min_temp = 15.0
    _attr_max_temp = 28.0
    _attr_target_temperature_step = PRECISION_HALVES
    _attr_supported_features = (
        ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
    )

    def __init__(self, vehicle: VehicleData) -> None:
        super().__init__(
            vehicle,
            "climate",
            (
                "HvacPower",
                "InsideTemp",
                "HvacLeftTemperatureRequest",
                "ClimateKeeperMode",
            ),
        )

    @callback
    def _async_update_attrs(self) -> None:
        power = enum_state(self.get("HvacPower"), "HvacPowerState")
        self._attr_hvac_mode = (
            None
            if power is None
            else HVACMode.OFF
            if power == "off"
            else HVACMode.HEAT_COOL
        )
        self._attr_current_temperature = as_float(self.get("InsideTemp"))
        self._attr_target_temperature = as_float(self.get("HvacLeftTemperatureRequest"))
        keeper = enum_state(self.get("ClimateKeeperMode"), "ClimateKeeperModeState")
        self._attr_preset_mode = _STREAM_PRESET.get(keeper or "")

    async def async_turn_on(self) -> None:
        await run_command(self.vehicle, self.vehicle.api.auto_conditioning_start)
        self.stream.set_override("HvacPower", "HvacPowerStateOn")

    async def async_turn_off(self) -> None:
        await run_command(self.vehicle, self.vehicle.api.auto_conditioning_stop)
        self.stream.set_override("HvacPower", "HvacPowerStateOff")

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
        else:
            await self.async_turn_on()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is not None:
            await run_command(
                self.vehicle, lambda: self.vehicle.api.set_temps(temp, temp)
            )
            self.stream.set_override("HvacLeftTemperatureRequest", temp)
        if (mode := kwargs.get(ATTR_HVAC_MODE)) is not None:
            await self.async_set_hvac_mode(mode)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        await run_command(
            self.vehicle,
            lambda: self.vehicle.api.set_climate_keeper_mode(PRESETS[preset_mode]),
        )
        stream_name = {v: k for k, v in _STREAM_PRESET.items()}[preset_mode]
        self.stream.set_override(
            "ClimateKeeperMode", f"ClimateKeeperModeState{stream_name.capitalize()}"
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TeslaBridgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(CabinClimate(v) for v in entry.runtime_data.vehicles)
