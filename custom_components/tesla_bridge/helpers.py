"""Convert streamed Fleet Telemetry values into Home Assistant states."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from homeassistant.util import dt as dt_util

# Word boundaries in Tesla enum names, keeping acronyms together:
# "NoPower" -> no_power, "ACSingleWireCAN" -> ac_single_wire_can.
_WORDS = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SPECIAL = {"CHAdeMO": "chademo"}


def as_float(value: Any) -> float | None:
    """Numbers stay numbers; numeric strings ('123.00000') are parsed.

    Tesla's protocol may switch a field between numeric and string encodings
    across firmware versions, so every numeric field goes through here.
    """
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def as_int(value: Any) -> int | None:
    number = as_float(value)
    return None if number is None else round(number)


def as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in ("true", "1", "on"):
            return True
        if lowered in ("false", "0", "off"):
            return False
    return None


def enum_state(value: Any, prefix: str) -> str | None:
    """'DetailedChargeStateNoPower' with prefix 'DetailedChargeState' -> 'no_power'.

    Unknown/SNA/Invalid map to None (unknown in Home Assistant).
    """
    if not isinstance(value, str) or not value:
        return None
    name = value.removeprefix(prefix)
    if name in ("Unknown", "SNA", "Invalid", ""):
        return None
    if name in _SPECIAL:
        return _SPECIAL[name]
    return re.sub(r"_+", "_", _WORDS.sub("_", name)).lower()


def hours_from_now(value: Any, *, round_to: int = 60) -> dt.datetime | None:
    """A duration in hours -> the timestamp it ends at (rounded to limit churn)."""
    hours = as_float(value)
    if hours is None or hours <= 0:
        return None
    seconds = round(hours * 3600 / round_to) * round_to
    return (dt_util.utcnow() + dt.timedelta(seconds=seconds)).replace(microsecond=0)


def minutes_from_now(value: Any) -> dt.datetime | None:
    minutes = as_float(value)
    if minutes is None or minutes < 0:
        return None
    return (dt_util.utcnow() + dt.timedelta(minutes=round(minutes))).replace(
        second=0, microsecond=0
    )


def location(value: Any) -> tuple[float, float] | None:
    if isinstance(value, dict):
        lat = as_float(value.get("latitude"))
        lon = as_float(value.get("longitude"))
        if lat is not None and lon is not None and not (lat == 0 and lon == 0):
            return lat, lon
    return None


DOOR_KEYS = {
    "door_driver_front": "DriverFront",
    "door_passenger_front": "PassengerFront",
    "door_driver_rear": "DriverRear",
    "door_passenger_rear": "PassengerRear",
    "frunk": "TrunkFront",
    "trunk": "TrunkRear",
}


def door_open(value: Any, key: str) -> bool | None:
    if not isinstance(value, dict):
        return None
    return bool(value.get(DOOR_KEYS[key]))


def window_open(value: Any) -> bool | None:
    state = enum_state(value, "WindowState")
    if state is None:
        return None
    return state != "closed"


_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
DUCKDNS_SUFFIX = ".duckdns.org"


def normalize_duckdns_domain(raw: str) -> str:
    """'myhome', 'myhome.duckdns.org' or a URL -> 'myhome.duckdns.org'.

    Raises ValueError for anything that is not a single DuckDNS account label.
    (Mirrors the app's own validation so the flow can reject typos early.)
    """
    value = re.sub(r"^[a-z]+://", "", raw.strip().lower()).split("/", 1)[0]
    value = value.split(":", 1)[0].rstrip(".")
    value = value.removesuffix(DUCKDNS_SUFFIX)
    if "." in value or not _LABEL.match(value):
        raise ValueError(f"'{raw}' is not a DuckDNS name")
    return f"{value}{DUCKDNS_SUFFIX}"
