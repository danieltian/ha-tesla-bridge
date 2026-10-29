"""Numbers: charge limit and current; Powerwall backup and off-grid EV reserves."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, UnitOfElectricCurrent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TeslaBridgeConfigEntry
from .entity import EnergySiteEntity, VehicleStreamEntity
from .helpers import as_float, as_int
from .models import EnergySiteData, VehicleData
from .vehicle import run_command, run_energy_command

PARALLEL_UPDATES = 0


class ChargeLimit(VehicleStreamEntity, NumberEntity):
    """Charge limit (%)."""

    _attr_native_min_value = 50
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = NumberDeviceClass.BATTERY
    _attr_mode = NumberMode.SLIDER

    def __init__(self, vehicle: VehicleData) -> None:
        super().__init__(vehicle, "charge_limit", ("ChargeLimitSoc",))

    @callback
    def _async_update_attrs(self) -> None:
        self._attr_native_value = as_float(self.get("ChargeLimitSoc"))

    async def async_set_native_value(self, value: float) -> None:
        percent = round(value)
        await run_command(
            self.vehicle, lambda: self.vehicle.api.set_charge_limit(percent)
        )
        self.stream.set_override("ChargeLimitSoc", percent)


class ChargeCurrent(VehicleStreamEntity, NumberEntity):
    """Requested charge current (A); the maximum comes from the charger."""

    _attr_native_min_value = 1
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_device_class = NumberDeviceClass.CURRENT
    _attr_mode = NumberMode.SLIDER

    def __init__(self, vehicle: VehicleData) -> None:
        super().__init__(
            vehicle,
            "charge_current",
            ("ChargeCurrentRequest", "ChargeCurrentRequestMax"),
        )

    @callback
    def _async_update_attrs(self) -> None:
        self._attr_native_value = as_float(self.get("ChargeCurrentRequest"))
        self._attr_native_max_value = (
            as_float(self.get("ChargeCurrentRequestMax")) or 48
        )

    async def async_set_native_value(self, value: float) -> None:
        amps = round(value)
        await run_command(
            self.vehicle, lambda: self.vehicle.api.set_charging_amps(amps)
        )
        self.stream.set_override("ChargeCurrentRequest", amps)


class BackupReserve(EnergySiteEntity[dict], NumberEntity):
    """Powerwall backup reserve (% as the Tesla app shows it)."""

    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = NumberDeviceClass.BATTERY
    _attr_mode = NumberMode.SLIDER

    # The gateway reports the reserve every few seconds; follow it too.
    follow_local = True

    def __init__(self, site: EnergySiteData) -> None:
        super().__init__(site, site.info, "backup_reserve")

    @callback
    def _async_update_attrs(self) -> None:
        local = self.site.local.data if self.site.local else None
        if local is not None and local.backup_reserve is not None:
            reported: float | None = round(local.backup_reserve)
        else:
            reported = as_float(
                (self.coordinator.data or {}).get("backup_reserve_percent")
            )
        self._attr_native_value = self._confirmed(reported)

    async def async_set_native_value(self, value: float) -> None:
        percent = round(value)
        await run_energy_command(self.site.api.backup(percent))
        self._set_pending(percent)
        self._attr_native_value = percent
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()


class OffGridEvReserve(EnergySiteEntity[dict], NumberEntity):
    """During an outage, stop EV charging below this Powerwall level."""

    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER
    _attr_entity_registry_enabled_default = False

    def __init__(self, site: EnergySiteData) -> None:
        super().__init__(site, site.info, "off_grid_ev_reserve")

    @callback
    def _async_update_attrs(self) -> None:
        self._attr_native_value = as_int(
            (self.coordinator.data or {}).get(
                "off_grid_vehicle_charging_reserve_percent"
            )
        )

    async def async_set_native_value(self, value: float) -> None:
        percent = round(value)
        await run_energy_command(
            self.site.api.off_grid_vehicle_charging_reserve(percent)
        )
        self._attr_native_value = percent
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TeslaBridgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    data = entry.runtime_data
    entities: list[NumberEntity] = []
    for vehicle in data.vehicles:
        entities += [ChargeLimit(vehicle), ChargeCurrent(vehicle)]
    for site in data.energy_sites:
        if site.has_battery:
            entities += [BackupReserve(site), OffGridEvReserve(site)]
    async_add_entities(entities)
