"""Sensors: streamed vehicle values, Powerwall (local first), app diagnostics."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import datetime as dt
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfPower,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import (
    async_track_time_interval,
    async_track_utc_time_change,
)
from homeassistant.util import dt as dt_util

from . import TeslaBridgeConfigEntry
from .const import DOMAIN, MANUFACTURER, SIGNALS_PER_DOLLAR
from .entity import BridgeAppEntity, EnergySiteEntity, VehicleStreamEntity
from .helpers import as_float, enum_state, hours_from_now, minutes_from_now
from .models import EnergySiteData, VehicleData
from .powerwall_local import LocalPowerwallData
from .stream import VehicleStream
from .usage import LABELS, STREAMING

PARALLEL_UPDATES = 0

CHARGE_STATES = [
    "disconnected",
    "no_power",
    "starting",
    "charging",
    "complete",
    "stopped",
]
SHIFT_STATES = ["p", "r", "n", "d"]
SENTRY_STATES = ["off", "idle", "armed", "aware", "panic", "quiet"]
CABLE_TYPES = ["iec", "sae", "gb_ac", "gb_dc"]
FAST_CHARGERS = [
    "supercharger",
    "chademo",
    "gb",
    "ac_single_wire_can",
    "combo",
    "mc_single_wire_can",
    "other",
]


def _charger_power(stream: VehicleStream) -> float | None:
    if stream.get("FastChargerPresent"):
        return as_float(stream.get("DCChargingPower"))
    return as_float(stream.get("ACChargingPower"))


def _charge_energy(stream: VehicleStream) -> float | None:
    if stream.get("FastChargerPresent"):
        return as_float(stream.get("DCChargingEnergyIn"))
    return as_float(stream.get("ACChargingEnergyIn"))


def _pack_power(stream: VehicleStream) -> float | None:
    voltage = as_float(stream.get("PackVoltage"))
    current = as_float(stream.get("PackCurrent"))
    if voltage is None or current is None:
        return None
    return round(voltage * current / 1000, 2)


def _full_charge_at(stream: VehicleStream) -> dt.datetime | None:
    if (
        enum_state(stream.get("DetailedChargeState"), "DetailedChargeState")
        != "charging"
    ):
        return None
    return hours_from_now(stream.get("TimeToFullCharge"))


def _number(field: str, digits: int | None = None) -> Callable[[VehicleStream], Any]:
    def value(stream: VehicleStream) -> Any:
        number = as_float(stream.get(field))
        if number is None or digits is None:
            return number
        return round(number, digits)

    return value


def _enum(field: str, prefix: str) -> Callable[[VehicleStream], str | None]:
    return lambda stream: enum_state(stream.get(field), prefix)


@dataclass(frozen=True, kw_only=True)
class VehicleSensorDescription(SensorEntityDescription):
    """A sensor computed from streamed fields."""

    fields: tuple[str, ...]
    value_fn: Callable[[VehicleStream], Any]
    # Re-evaluate at UTC midnight (daily/monthly counters roll over then).
    refresh_at_midnight: bool = False


def _signals_today(stream: VehicleStream) -> int | None:
    """The app counts per UTC day; a car silent since midnight sent 0 today."""
    signals = stream.signals
    if not signals:
        return None
    today = dt_util.utcnow().date().isoformat()
    return int(signals.get("count", 0)) if signals.get("day") == today else 0


def _cost_this_month(stream: VehicleStream) -> float | None:
    signals = stream.signals
    if not signals:
        return None
    month = dt_util.utcnow().date().isoformat()[:7]
    count = int(signals.get("month_count", 0)) if signals.get("month") == month else 0
    return round(count / SIGNALS_PER_DOLLAR, 3)


VEHICLE_SENSORS: tuple[VehicleSensorDescription, ...] = (
    VehicleSensorDescription(
        key="battery_level",
        fields=("BatteryLevel",),
        value_fn=_number("BatteryLevel", 1),
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    VehicleSensorDescription(
        key="usable_battery_level",
        fields=("Soc",),
        value_fn=_number("Soc", 1),
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
    ),
    VehicleSensorDescription(
        key="charging_state",
        fields=("DetailedChargeState",),
        value_fn=_enum("DetailedChargeState", "DetailedChargeState"),
        device_class=SensorDeviceClass.ENUM,
        options=CHARGE_STATES,
    ),
    VehicleSensorDescription(
        key="charger_power",
        fields=("ACChargingPower", "DCChargingPower", "FastChargerPresent"),
        value_fn=_charger_power,
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    VehicleSensorDescription(
        key="charge_energy_added",
        fields=("ACChargingEnergyIn", "DCChargingEnergyIn", "FastChargerPresent"),
        value_fn=_charge_energy,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
    ),
    VehicleSensorDescription(
        key="charger_voltage",
        fields=("ChargerVoltage",),
        value_fn=_number("ChargerVoltage", 0),
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VehicleSensorDescription(
        key="charger_current",
        fields=("ChargeAmps",),
        value_fn=_number("ChargeAmps", 0),
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VehicleSensorDescription(
        key="charge_cable",
        fields=("ChargingCableType",),
        value_fn=_enum("ChargingCableType", "CableType"),
        device_class=SensorDeviceClass.ENUM,
        options=CABLE_TYPES,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    VehicleSensorDescription(
        key="fast_charger_type",
        fields=("FastChargerType",),
        value_fn=_enum("FastChargerType", "FastCharger"),
        device_class=SensorDeviceClass.ENUM,
        options=FAST_CHARGERS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    VehicleSensorDescription(
        key="full_charge_at",
        fields=("TimeToFullCharge", "DetailedChargeState"),
        value_fn=_full_charge_at,
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    VehicleSensorDescription(
        key="battery_range",
        fields=("RatedRange",),
        value_fn=_number("RatedRange", 1),
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.MILES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    VehicleSensorDescription(
        key="est_battery_range",
        fields=("EstBatteryRange",),
        value_fn=_number("EstBatteryRange", 1),
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.MILES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
    ),
    VehicleSensorDescription(
        key="ideal_battery_range",
        fields=("IdealBatteryRange",),
        value_fn=_number("IdealBatteryRange", 1),
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.MILES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_registry_enabled_default=False,
    ),
    VehicleSensorDescription(
        key="energy_remaining",
        fields=("EnergyRemaining",),
        value_fn=_number("EnergyRemaining", 2),
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    VehicleSensorDescription(
        key="battery_power",
        fields=("PackVoltage", "PackCurrent"),
        value_fn=_pack_power,
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
    ),
    VehicleSensorDescription(
        key="speed",
        fields=("VehicleSpeed",),
        value_fn=_number("VehicleSpeed", 0),
        device_class=SensorDeviceClass.SPEED,
        native_unit_of_measurement=UnitOfSpeed.MILES_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VehicleSensorDescription(
        key="shift_state",
        fields=("Gear",),
        value_fn=_enum("Gear", "ShiftState"),
        device_class=SensorDeviceClass.ENUM,
        options=SHIFT_STATES,
    ),
    VehicleSensorDescription(
        key="odometer",
        fields=("Odometer",),
        value_fn=_number("Odometer", 1),
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.MILES,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
    ),
    VehicleSensorDescription(
        key="inside_temp",
        fields=("InsideTemp",),
        value_fn=_number("InsideTemp", 1),
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VehicleSensorDescription(
        key="outside_temp",
        fields=("OutsideTemp",),
        value_fn=_number("OutsideTemp", 1),
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    VehicleSensorDescription(
        key="driver_temp_setting",
        fields=("HvacLeftTemperatureRequest",),
        value_fn=_number("HvacLeftTemperatureRequest", 1),
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_registry_enabled_default=False,
    ),
    VehicleSensorDescription(
        key="passenger_temp_setting",
        fields=("HvacRightTemperatureRequest",),
        value_fn=_number("HvacRightTemperatureRequest", 1),
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        entity_registry_enabled_default=False,
    ),
    *(
        VehicleSensorDescription(
            key=f"tpms_pressure_{tire}",
            fields=(f"TpmsPressure{tire.capitalize()}",),
            value_fn=_number(f"TpmsPressure{tire.capitalize()}", 2),
            device_class=SensorDeviceClass.PRESSURE,
            native_unit_of_measurement=UnitOfPressure.BAR,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
            entity_category=EntityCategory.DIAGNOSTIC,
        )
        for tire in ("fl", "fr", "rl", "rr")
    ),
    VehicleSensorDescription(
        key="route_destination",
        fields=("DestinationName",),
        value_fn=lambda s: s.get("DestinationName") or None,
    ),
    VehicleSensorDescription(
        key="route_arrival",
        fields=("MinutesToArrival",),
        value_fn=lambda s: (
            minutes_from_now(s.get("MinutesToArrival"))
            if s.get("DestinationName")
            else None
        ),
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    VehicleSensorDescription(
        key="route_distance",
        fields=("MilesToArrival",),
        value_fn=_number("MilesToArrival", 1),
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.MILES,
    ),
    VehicleSensorDescription(
        key="route_energy_at_arrival",
        fields=("ExpectedEnergyPercentAtTripArrival",),
        value_fn=_number("ExpectedEnergyPercentAtTripArrival", 0),
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
    ),
    VehicleSensorDescription(
        key="route_traffic_delay",
        fields=("RouteTrafficMinutesDelay",),
        value_fn=_number("RouteTrafficMinutesDelay", 0),
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        entity_registry_enabled_default=False,
    ),
    VehicleSensorDescription(
        key="sentry_state",
        fields=("SentryMode",),
        value_fn=_enum("SentryMode", "SentryModeState"),
        device_class=SensorDeviceClass.ENUM,
        options=SENTRY_STATES,
        entity_registry_enabled_default=False,
    ),
    VehicleSensorDescription(
        key="signals_today",
        fields=("__signals__",),
        value_fn=_signals_today,
        refresh_at_midnight=True,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    VehicleSensorDescription(
        key="stream_cost_month",
        fields=("__signals__",),
        value_fn=_cost_this_month,
        refresh_at_midnight=True,
        native_unit_of_measurement="USD",
        device_class=SensorDeviceClass.MONETARY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


class VehicleSensor(VehicleStreamEntity, SensorEntity):
    """A streamed vehicle value."""

    entity_description: VehicleSensorDescription

    def __init__(
        self, vehicle: VehicleData, description: VehicleSensorDescription
    ) -> None:
        self.entity_description = description
        super().__init__(vehicle, description.key, description.fields)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.entity_description.refresh_at_midnight:
            self.async_on_remove(
                async_track_utc_time_change(
                    self.hass, self._midnight, hour=0, minute=0, second=1
                )
            )

    @callback
    def _midnight(self, _now: dt.datetime) -> None:
        self._handle_stream_update()

    @callback
    def _async_update_attrs(self) -> None:
        self._attr_native_value = self.entity_description.value_fn(self.stream)


# ---- energy site -------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class EnergySensorDescription(SensorEntityDescription):
    """Prefers the local gateway value; falls back to the cloud."""

    local_fn: Callable[[LocalPowerwallData], Any] | None = None
    cloud_key: str | None = None
    cloud_fn: Callable[[Any], Any] | None = None


def _meter_power(meter: str) -> Callable[[LocalPowerwallData], Any]:
    def value(data: LocalPowerwallData) -> Any:
        m = data.meters.get(meter)
        return None if m is None else round(m.instant_power / 1000, 3)

    return value


def _meter_energy(meter: str, direction: str) -> Callable[[LocalPowerwallData], Any]:
    def value(data: LocalPowerwallData) -> Any:
        m = data.meters.get(meter)
        if m is None:
            return None
        wh = m.energy_exported if direction == "exported" else m.energy_imported
        return round(wh / 1000, 3)

    return value


def _kw(value: Any) -> Any:
    number = as_float(value)
    return None if number is None else round(number / 1000, 3)


ENERGY_SENSORS: tuple[EnergySensorDescription, ...] = (
    EnergySensorDescription(
        key="percentage_charged",
        local_fn=lambda d: d.charge,
        cloud_key="percentage_charged",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    EnergySensorDescription(
        key="battery_power",
        local_fn=_meter_power("battery"),
        cloud_key="battery_power",
        cloud_fn=_kw,
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    EnergySensorDescription(
        key="solar_power",
        local_fn=_meter_power("solar"),
        cloud_key="solar_power",
        cloud_fn=_kw,
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    EnergySensorDescription(
        key="load_power",
        local_fn=_meter_power("load"),
        cloud_key="load_power",
        cloud_fn=_kw,
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    EnergySensorDescription(
        key="grid_power",
        local_fn=_meter_power("site"),
        cloud_key="grid_power",
        cloud_fn=_kw,
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
    ),
    EnergySensorDescription(
        key="energy_left",
        cloud_key="energy_left",
        cloud_fn=_kw,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    EnergySensorDescription(
        key="charge_raw",
        local_fn=lambda d: round(d.charge_raw, 1),
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    EnergySensorDescription(
        key="grid_status",
        local_fn=lambda d: {
            "SystemGridConnected": "connected",
            "SystemIslandedActive": "islanded",
            "SystemIslandedReady": "islanded_ready",
            "SystemTransitionToGrid": "transition_to_grid",
            "SystemTransitionToIsland": "transition_to_island",
            "SystemWaitForUser": "wait_for_user",
        }.get(d.grid_status),
        cloud_key="island_status",
        cloud_fn=lambda v: {
            "on_grid": "connected",
            "off_grid": "islanded",
            "off_grid_intentional": "islanded",
            "off_grid_unintentional": "islanded",
        }.get(v),
        device_class=SensorDeviceClass.ENUM,
        options=[
            "connected",
            "islanded",
            "islanded_ready",
            "transition_to_grid",
            "transition_to_island",
            "wait_for_user",
        ],
    ),
    # Lifetime meter totals for the Energy dashboard (local only).
    EnergySensorDescription(
        key="grid_imported",
        local_fn=_meter_energy("site", "imported"),
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
    ),
    EnergySensorDescription(
        key="grid_exported",
        local_fn=_meter_energy("site", "exported"),
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
    ),
    EnergySensorDescription(
        key="solar_generated",
        local_fn=_meter_energy("solar", "exported"),
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
    ),
    EnergySensorDescription(
        key="battery_discharged",
        local_fn=_meter_energy("battery", "exported"),
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
    ),
    EnergySensorDescription(
        key="battery_charged",
        local_fn=_meter_energy("battery", "imported"),
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
    ),
    EnergySensorDescription(
        key="home_consumed",
        local_fn=_meter_energy("load", "imported"),
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
    ),
    EnergySensorDescription(
        key="grid_voltage",
        local_fn=lambda d: d.meters["site"].voltage if "site" in d.meters else None,
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


# Only meaningful when the site has a Powerwall.
BATTERY_KEYS = {"percentage_charged", "battery_power", "energy_left", "charge_raw"}


class EnergySensor(EnergySiteEntity[Any], SensorEntity):
    """A Powerwall value from the gateway (or the cloud as fallback)."""

    entity_description: EnergySensorDescription

    def __init__(
        self, site: EnergySiteData, description: EnergySensorDescription
    ) -> None:
        self.entity_description = description
        coordinator = site.local if (site.local and description.local_fn) else site.live
        super().__init__(site, coordinator, description.key)

    @callback
    def _async_update_attrs(self) -> None:
        desc = self.entity_description
        if self.site.local is not None and desc.local_fn is not None:
            data = self.site.local.data
            self._attr_native_value = desc.local_fn(data) if data else None
            return
        raw = (self.coordinator.data or {}).get(desc.cloud_key or desc.key)
        self._attr_native_value = desc.cloud_fn(raw) if desc.cloud_fn else raw


class BatterySensor(EnergySiteEntity[LocalPowerwallData], SensorEntity):
    """Per-Powerwall values (local API, non-restricted gateways)."""

    def __init__(
        self,
        site: EnergySiteData,
        serial: str,
        description: SensorEntityDescription,
        value_fn: Callable[[Any], Any],
    ) -> None:
        assert site.local is not None
        self.entity_description = description
        self._serial = serial
        self._value_fn = value_fn
        super().__init__(site, site.local, f"{serial}_{description.key}")
        self._attr_translation_key = f"battery_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial)},
            manufacturer=MANUFACTURER,
            model="Powerwall",
            name=f"Powerwall {serial[-4:]}",
            serial_number=serial,
            via_device=(DOMAIN, str(site.site_id)),
        )

    @callback
    def _async_update_attrs(self) -> None:
        battery = (
            self.coordinator.data.batteries.get(self._serial)
            if self.coordinator.data
            else None
        )
        self._attr_native_value = self._value_fn(battery) if battery else None


BATTERY_SENSORS: tuple[tuple[SensorEntityDescription, Callable[[Any], Any]], ...] = (
    (
        SensorEntityDescription(
            key="capacity",
            device_class=SensorDeviceClass.ENERGY_STORAGE,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=2,
        ),
        lambda b: None if b.capacity is None else round(b.capacity / 1000, 3),
    ),
    (
        SensorEntityDescription(
            key="energy_remaining",
            device_class=SensorDeviceClass.ENERGY_STORAGE,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=2,
        ),
        lambda b: (
            None if b.energy_remaining is None else round(b.energy_remaining / 1000, 3)
        ),
    ),
    (
        SensorEntityDescription(
            key="power",
            device_class=SensorDeviceClass.POWER,
            native_unit_of_measurement=UnitOfPower.KILO_WATT,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=2,
        ),
        lambda b: None if b.p_out is None else round(b.p_out / 1000, 3),
    ),
    (
        SensorEntityDescription(
            key="charged",
            device_class=SensorDeviceClass.ENERGY,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            state_class=SensorStateClass.TOTAL_INCREASING,
            entity_registry_enabled_default=False,
        ),
        lambda b: (
            None if b.energy_charged is None else round(b.energy_charged / 1000, 3)
        ),
    ),
    (
        SensorEntityDescription(
            key="discharged",
            device_class=SensorDeviceClass.ENERGY,
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            state_class=SensorStateClass.TOTAL_INCREASING,
            entity_registry_enabled_default=False,
        ),
        lambda b: (
            None
            if b.energy_discharged is None
            else round(b.energy_discharged / 1000, 3)
        ),
    ),
)


# ---- the app ---------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class BridgeSensorDescription(SensorEntityDescription):
    """A value from the app's /status."""

    value_fn: Callable[[dict[str, Any]], Any]


def _cert_expiry(status: dict[str, Any]) -> dt.datetime | None:
    cert = (status.get("key_server") or {}).get("certificate") or {}
    return dt_util.parse_datetime(cert["not_after"]) if cert.get("not_after") else None


BRIDGE_SENSORS: tuple[BridgeSensorDescription, ...] = (
    BridgeSensorDescription(
        key="public_ip",
        value_fn=lambda s: (s.get("duckdns") or {}).get("ip"),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BridgeSensorDescription(
        key="certificate_expiry",
        value_fn=_cert_expiry,
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BridgeSensorDescription(
        key="connected_vehicles",
        value_fn=lambda s: sum(
            1
            for v in ((s.get("relay") or {}).get("vehicles") or {}).values()
            if v.get("connected")
        ),
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


class BridgeSensor(BridgeAppEntity, SensorEntity):
    """App health value."""

    entity_description: BridgeSensorDescription

    def __init__(
        self, entry: TeslaBridgeConfigEntry, description: BridgeSensorDescription
    ) -> None:
        self.entity_description = description
        super().__init__(entry.runtime_data.bridge, description.key, entry.entry_id)

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data or {})


@dataclass(frozen=True, kw_only=True)
class UsageSensorDescription(SensorEntityDescription):
    """A value from this month's Fleet API usage summary (see usage.py)."""

    value_fn: Callable[[dict[str, Any]], Any]
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None


USAGE_REFRESH = dt.timedelta(minutes=5)


def _usage_breakdown(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        LABELS[category]: f"{count:,} (${summary['costs'][category]:.2f})"
        for category, count in summary["counts"].items()
    }


USAGE_SENSORS: tuple[UsageSensorDescription, ...] = (
    UsageSensorDescription(
        key="api_cost_month",
        value_fn=lambda s: round(s["total"], 2),
        attrs_fn=_usage_breakdown,
        native_unit_of_measurement="USD",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
    ),
    UsageSensorDescription(
        key="api_cost_projected",
        value_fn=lambda s: round(s["projected"], 2),
        native_unit_of_measurement="USD",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
    ),
    UsageSensorDescription(
        key="api_requests_month",
        value_fn=lambda s: s["requests"],
        attrs_fn=lambda s: {
            LABELS[c]: n for c, n in s["counts"].items() if c != STREAMING
        },
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


class UsageSensor(SensorEntity):
    """Tesla Bridge's own Fleet API use this month, priced at Tesla's rates."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: UsageSensorDescription

    def __init__(
        self, entry: TeslaBridgeConfigEntry, description: UsageSensorDescription
    ) -> None:
        self.entity_description = description
        self._data = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}-usage-{description.key}"
        self._attr_translation_key = description.key
        self._attr_device_info = entry.runtime_data.bridge.device
        self._update()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._data.usage.async_add_listener(self._refresh))
        # Streamed values arrive constantly; their cost moves in cents per hour.
        self.async_on_remove(
            async_track_time_interval(self.hass, self._refresh, USAGE_REFRESH)
        )

    @callback
    def _refresh(self, _now: dt.datetime | None = None) -> None:
        self._update()
        self.async_write_ha_state()

    def _update(self) -> None:
        summary = self._data.usage.summary(self._data.vehicles)
        description = self.entity_description
        self._attr_native_value = description.value_fn(summary)
        if description.attrs_fn is not None:
            self._attr_extra_state_attributes = description.attrs_fn(summary)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TeslaBridgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    data = entry.runtime_data
    entities: list[SensorEntity] = [
        VehicleSensor(vehicle, description)
        for vehicle in data.vehicles
        for description in VEHICLE_SENSORS
    ]
    for site in data.energy_sites:
        for description in ENERGY_SENSORS:
            if site.local is None and description.cloud_key is None:
                continue  # local-only value without a gateway connection
            if description.key == "solar_power" and not site.has_solar:
                continue
            if description.key in BATTERY_KEYS and not site.has_battery:
                continue
            entities.append(EnergySensor(site, description))
        if site.local is not None and site.local.data is not None:
            for serial in site.local.data.batteries:
                entities.extend(
                    BatterySensor(site, serial, description, value_fn)
                    for description, value_fn in BATTERY_SENSORS
                )
    entities.extend(BridgeSensor(entry, description) for description in BRIDGE_SENSORS)
    entities.extend(UsageSensor(entry, description) for description in USAGE_SENSORS)
    async_add_entities(entities)
