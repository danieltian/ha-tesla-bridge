"""Telemetry field catalog and value helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.tesla_bridge.const import TelemetryPreset
from custom_components.tesla_bridge.fields import (
    FIELD_SPECS,
    estimate_signals_per_hour,
    fields_for_preset,
)
from custom_components.tesla_bridge.helpers import (
    as_float,
    enum_state,
    normalize_duckdns_domain,
    window_open,
)
from custom_components.tesla_bridge.powerwall_local import (
    app_percentage,
    raw_percentage,
)

PROTO_FIELDS = set(
    (Path(__file__).parent / "fixtures" / "telemetry_fields_v0.9.5.txt")
    .read_text()
    .split()
)


def test_every_field_exists_in_tesla_proto() -> None:
    unknown = {spec.name for spec in FIELD_SPECS} - PROTO_FIELDS
    assert not unknown, f"not in vehicle_data.proto: {sorted(unknown)}"


def test_no_duplicate_fields() -> None:
    names = [spec.name for spec in FIELD_SPECS]
    assert len(names) == len(set(names))


def test_presets_are_nested_and_sane() -> None:
    essential = fields_for_preset(TelemetryPreset.ESSENTIAL)
    standard = fields_for_preset(TelemetryPreset.STANDARD)
    everything = fields_for_preset(TelemetryPreset.EVERYTHING)
    assert set(essential) < set(standard) < set(everything)
    assert everything["Location"]["interval_seconds"] == 1
    assert standard["Location"]["interval_seconds"] == 10
    for preset in (essential, standard, everything):
        for entry in preset.values():
            assert entry["interval_seconds"] >= 1
    # Fields that need firmware 2026.32 stay out of the default preset.
    assert "SoftwareUpdateAvailable" not in standard
    assert (
        estimate_signals_per_hour(TelemetryPreset.ESSENTIAL)
        < estimate_signals_per_hour(TelemetryPreset.STANDARD)
        < estimate_signals_per_hour(TelemetryPreset.EVERYTHING)
    )


def test_estimates_are_typical_not_worst_case() -> None:
    # Only changed values are sent: an hour of driving with location and speed
    # every 10 s is about a thousand values, not tens of thousands.
    assert 800 < estimate_signals_per_hour(TelemetryPreset.ESSENTIAL) < 2_000
    assert 1_000 < estimate_signals_per_hour(TelemetryPreset.STANDARD) < 2_500
    # Every second for location, heading and speed dominates "everything".
    assert 5_000 < estimate_signals_per_hour(TelemetryPreset.EVERYTHING) < 10_000


def test_overrides() -> None:
    fields = fields_for_preset(
        "essential", {"Location": 30, "Odometer": None, "InsideTemp": 0}
    )
    assert fields["Location"]["interval_seconds"] == 30
    assert "Odometer" not in fields
    assert fields["InsideTemp"]["interval_seconds"] == 1


@pytest.mark.parametrize(
    ("value", "prefix", "expected"),
    [
        ("DetailedChargeStateNoPower", "DetailedChargeState", "no_power"),
        ("ShiftStateSNA", "ShiftState", None),
        ("FastChargerCHAdeMO", "FastCharger", "chademo"),
        ("CableTypeGB_DC", "CableType", "gb_dc"),
        (None, "X", None),
        (5, "X", None),
    ],
)
def test_enum_state(value: object, prefix: str, expected: str | None) -> None:
    assert enum_state(value, prefix) == expected


def test_as_float_handles_type_flips() -> None:
    assert as_float(12) == 12.0
    assert as_float("123.00000") == 123.0
    assert as_float("abc") is None
    assert as_float(None) is None


def test_window_open() -> None:
    assert window_open("WindowStateClosed") is False
    assert window_open("WindowStatePartiallyOpen") is True
    assert window_open("WindowStateUnknown") is None


@pytest.mark.parametrize(
    ("raw", "app"), [(5, 0.0), (36.89, 33.6), (100, 100.0), (2, 0.0), (None, None)]
)
def test_powerwall_app_percentage(raw: float | None, app: float | None) -> None:
    assert app_percentage(raw) == app


def test_powerwall_raw_roundtrip() -> None:
    assert app_percentage(raw_percentage(20)) == 20.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("myhome", "myhome.duckdns.org"),
        ("https://Myhome.duckdns.org/x", "myhome.duckdns.org"),
    ],
)
def test_normalize_duckdns(raw: str, expected: str) -> None:
    assert normalize_duckdns_domain(raw) == expected


def test_normalize_duckdns_rejects() -> None:
    with pytest.raises(ValueError, match="not a DuckDNS name"):
        normalize_duckdns_domain("a.b.duckdns.org")


def test_every_streamed_field_is_used_by_an_entity() -> None:
    """Every streamed value is billed; don't stream what nothing reads."""
    package = Path(__file__).parents[2] / "custom_components" / "tesla_bridge"
    source = "\n".join(
        p.read_text()
        for p in package.glob("*.py")
        if p.name not in ("fields.py", "migration.py")
    )
    # Tire pressures are referenced as f"TpmsPressure{tire.capitalize()}".
    dynamic = {f"TpmsPressure{t}" for t in ("Fl", "Fr", "Rl", "Rr")}
    unused = [
        spec.name
        for spec in FIELD_SPECS
        if f'"{spec.name}"' not in source and spec.name not in dynamic
    ]
    assert not unused, f"streamed but unused: {unused}"
