"""Which Fleet Telemetry fields each preset streams, and how often.

Every streamed signal is billed by Tesla (about 150,000 signals per $1, with a
$10 monthly credit per account). A field is sent only when its value changed
*and* at least ``interval_seconds`` passed since it was last sent, so intervals
cap the cost; ``minimum_delta`` additionally ignores tiny changes (requires
vehicle firmware 2024.44.32+). Only fields some entity reads are streamed;
``tests/integration/test_fields_helpers.py`` enforces that.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .const import TelemetryPreset

_ORDER = {
    TelemetryPreset.ESSENTIAL: 0,
    TelemetryPreset.STANDARD: 1,
    TelemetryPreset.EVERYTHING: 2,
}


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One telemetry field and its sending policy."""

    name: str
    preset: TelemetryPreset
    interval: int
    minimum_delta: float | None = None
    # Used instead of ``interval`` in the "everything" preset.
    fast_interval: int | None = None


E = TelemetryPreset.ESSENTIAL
S = TelemetryPreset.STANDARD
X = TelemetryPreset.EVERYTHING

FIELD_SPECS: tuple[FieldSpec, ...] = (
    # --- battery & charging ---------------------------------------------------
    FieldSpec("BatteryLevel", E, 60, 0.5, fast_interval=10),
    FieldSpec("Soc", E, 60, 0.5, fast_interval=10),
    FieldSpec("ChargeLimitSoc", E, 5),
    FieldSpec("DetailedChargeState", E, 2),
    FieldSpec("ChargeAmps", E, 10, 1),
    FieldSpec("ChargeCurrentRequest", E, 5),
    FieldSpec("ChargeCurrentRequestMax", E, 60),
    FieldSpec("ChargerVoltage", E, 30, 5),
    FieldSpec("ACChargingPower", E, 10, 0.2),
    FieldSpec("DCChargingPower", E, 10, 1),
    FieldSpec("ACChargingEnergyIn", E, 60, 0.1),
    FieldSpec("DCChargingEnergyIn", E, 60, 0.1),
    FieldSpec("TimeToFullCharge", E, 60, 0.05),
    FieldSpec("FastChargerPresent", E, 10),
    FieldSpec("FastChargerType", E, 60),
    FieldSpec("ChargingCableType", E, 10),
    FieldSpec("ChargePortDoorOpen", E, 2),
    FieldSpec("ChargePortLatch", E, 5),
    FieldSpec("EstBatteryRange", E, 60, 1),
    FieldSpec("RatedRange", E, 60, 1),
    FieldSpec("IdealBatteryRange", S, 300, 1),
    FieldSpec("ScheduledChargingPending", S, 60),
    FieldSpec("PreconditioningEnabled", S, 30),
    FieldSpec("BatteryHeaterOn", S, 60),
    FieldSpec("EnergyRemaining", X, 60, 0.1),
    FieldSpec("PackVoltage", X, 30, 1),
    FieldSpec("PackCurrent", X, 30, 1),
    # --- driving & location ---------------------------------------------------
    FieldSpec("Location", E, 10, 15, fast_interval=1),
    FieldSpec("GpsHeading", E, 10, 10, fast_interval=1),
    FieldSpec("VehicleSpeed", E, 10, 3, fast_interval=1),
    FieldSpec("Gear", E, 1),
    FieldSpec("Odometer", E, 300, 0.5, fast_interval=60),
    FieldSpec("DestinationName", S, 30),
    FieldSpec("DestinationLocation", S, 60),
    FieldSpec("MinutesToArrival", S, 30, 1),
    FieldSpec("MilesToArrival", S, 30, 0.5),
    FieldSpec("ExpectedEnergyPercentAtTripArrival", S, 60, 1),
    FieldSpec("RouteTrafficMinutesDelay", S, 60, 1),
    FieldSpec("LocatedAtHome", S, 30),
    FieldSpec("HomelinkNearby", S, 10),
    # --- security -------------------------------------------------------------
    FieldSpec("Locked", E, 1),
    FieldSpec("DoorState", E, 1),
    FieldSpec("FdWindow", E, 5),
    FieldSpec("FpWindow", E, 5),
    FieldSpec("RdWindow", E, 5),
    FieldSpec("RpWindow", E, 5),
    FieldSpec("SentryMode", E, 5),
    FieldSpec("DriverSeatOccupied", S, 10),
    # --- climate --------------------------------------------------------------
    FieldSpec("InsideTemp", E, 60, 0.5, fast_interval=10),
    FieldSpec("OutsideTemp", E, 300, 0.5, fast_interval=30),
    FieldSpec("HvacPower", E, 2),
    FieldSpec("HvacLeftTemperatureRequest", E, 5),
    FieldSpec("HvacRightTemperatureRequest", E, 5),
    FieldSpec("ClimateKeeperMode", E, 5),
    FieldSpec("DefrostMode", E, 5),
    FieldSpec("HvacSteeringWheelHeatLevel", S, 5),
    FieldSpec("HvacSteeringWheelHeatAuto", S, 5),
    FieldSpec("SeatHeaterLeft", S, 5),
    FieldSpec("SeatHeaterRight", S, 5),
    FieldSpec("SeatHeaterRearLeft", S, 5),
    FieldSpec("SeatHeaterRearRight", S, 5),
    FieldSpec("SeatHeaterRearCenter", S, 5),
    FieldSpec("AutoSeatClimateLeft", S, 5),
    FieldSpec("AutoSeatClimateRight", S, 5),
    FieldSpec("ClimateSeatCoolingFrontLeft", S, 5),
    FieldSpec("ClimateSeatCoolingFrontRight", S, 5),
    FieldSpec("CabinOverheatProtectionMode", S, 30),
    # --- tires ----------------------------------------------------------------
    FieldSpec("TpmsPressureFl", S, 600, 0.05),
    FieldSpec("TpmsPressureFr", S, 600, 0.05),
    FieldSpec("TpmsPressureRl", S, 600, 0.05),
    FieldSpec("TpmsPressureRr", S, 600, 0.05),
    FieldSpec("TpmsHardWarnings", S, 60),
    FieldSpec("TpmsSoftWarnings", S, 60),
    # --- software, media, identity ----------------------------------------------
    FieldSpec("Version", E, 3600),
    FieldSpec("SoftwareUpdateVersion", S, 3600),
    FieldSpec("SoftwareUpdateDownloadPercentComplete", S, 60, 5),
    FieldSpec("SoftwareUpdateInstallationPercentComplete", S, 60, 5),
    FieldSpec("MediaPlaybackStatus", S, 5),
    FieldSpec("MediaPlaybackSource", S, 10),
    FieldSpec("MediaAudioVolume", S, 5, 0.3),
    FieldSpec("MediaAudioVolumeMax", S, 3600),
    FieldSpec("MediaNowPlayingTitle", S, 10),
    FieldSpec("MediaNowPlayingArtist", S, 10),
    FieldSpec("MediaNowPlayingAlbum", S, 10),
    FieldSpec("MediaNowPlayingStation", S, 10),
    FieldSpec("MediaNowPlayingDuration", X, 30),
    FieldSpec("MediaNowPlayingElapsed", X, 30, 10),
    # Field 267 needs vehicle firmware 2026.32; older cars may reject it.
    FieldSpec("SoftwareUpdateInProgress", X, 60),
)


def fields_for_preset(
    preset: TelemetryPreset | str, overrides: dict[str, int | None] | None = None
) -> dict[str, dict[str, Any]]:
    """Return the ``fields`` object for a vehicle's ``fleet_telemetry_config``.

    ``overrides`` maps field name to an interval in seconds, or None to drop it.
    """
    level = _ORDER[TelemetryPreset(preset)]
    fast = level == _ORDER[TelemetryPreset.EVERYTHING]
    result: dict[str, dict[str, Any]] = {}
    for spec in FIELD_SPECS:
        if _ORDER[spec.preset] > level:
            continue
        entry: dict[str, Any] = {
            "interval_seconds": spec.fast_interval
            if fast and spec.fast_interval
            else spec.interval
        }
        if spec.minimum_delta is not None:
            entry["minimum_delta"] = spec.minimum_delta
        result[spec.name] = entry
    for name, interval in (overrides or {}).items():
        if interval is None:
            result.pop(name, None)
        else:
            result.setdefault(name, {})["interval_seconds"] = max(1, int(interval))
    return result


def estimate_signals_per_hour(preset: TelemetryPreset | str) -> int:
    """Rough upper bound of signals per active (driving/charging) hour."""
    total = 0.0
    for name, entry in fields_for_preset(preset).items():
        del name
        # Assume a busy field changes every interval, capped at once a minute
        # for slow-moving values when a minimum delta applies.
        interval = entry["interval_seconds"]
        per_hour = 3600 / interval
        if "minimum_delta" in entry:
            per_hour = min(per_hour, 360)
        total += min(per_hour, 3600)
    return int(total)
