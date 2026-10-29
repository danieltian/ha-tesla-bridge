"""Relay normalization, snapshot and connectivity tracking."""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import pytest

from tesla_bridge.relay import Relay, normalize_value
from tesla_bridge.state import StateStore


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"stringValue": "Sim Car"}, "Sim Car"),
        ({"doubleValue": 71.25}, 71.25),
        ({"floatValue": 70.5}, 70.5),
        ({"intValue": 80}, 80),
        ({"longValue": "9876"}, 9876),
        ({"booleanValue": True}, True),
        ({"invalid": True}, None),
        ({}, None),
        (None, None),
        (
            {"locationValue": {"latitude": 37.39, "longitude": -122.15}},
            {"latitude": 37.39, "longitude": -122.15},
        ),
        ({"shiftStateValue": "ShiftStateD"}, "ShiftStateD"),
        (
            {"detailedChargeStateValue": "DetailedChargeStateCharging"},
            "DetailedChargeStateCharging",
        ),
        ({"timeValue": {"hour": 23, "minute": 30}}, {"hour": 23, "minute": 30}),
        (
            {
                "tireLocationValue": {
                    "frontLeft": True,
                    "frontRight": False,
                    "semiRearAxleLeft": True,
                }
            },
            {
                "frontLeft": True,
                "frontRight": False,
                "rearLeft": False,
                "rearRight": False,
            },
        ),
    ],
)
def test_normalize_value(value: Any, expected: Any) -> None:
    assert normalize_value(value) == expected


def _relay(state: StateStore) -> Relay:
    relay = Relay(state, "tcp://127.0.0.1:1")
    relay._now = lambda: dt.datetime(2026, 10, 26, 12, 0, tzinfo=dt.UTC)
    return relay


async def test_captured_records(
    state: StateStore, zmq_records: list[dict[str, Any]]
) -> None:
    relay = _relay(state)
    queue = relay.subscribe()
    for record in zmq_records:
        relay.dispatch(record["topic"], record["payload"])

    snap = relay.snapshot()["vehicles"]
    car = snap["5YJSIM00000000001"]["fields"]
    assert car["VehicleName"]["v"] == "Sim Car"
    assert car["ChargeLimitSoc"]["v"] == 80
    assert car["LifetimeEnergyChargedKwh"]["v"] == 9876
    assert car["InsideTemp"]["v"] is None
    assert car["DoorState"]["v"]["DriverFront"] is True
    assert car["Gear"]["v"] == "ShiftStateP"  # park scenario was last
    assert car["EstBatteryRange"]["v"] == "123.00000"  # strings stay strings
    # The drive scenario moved the car after the initial record.
    assert car["Location"]["v"] == {"latitude": 37.3957, "longitude": -122.1493}

    # Every simulator session disconnected at the end.
    assert snap["5YJSIM00000000001"]["connected"] is False
    signals = state.data.signals["5YJSIM00000000001"]
    assert signals["count"] == 20 + 5 + 5 + 4 + 4 + 3
    assert signals["day"] == "2026-10-26"

    events = [queue.get_nowait() for _ in range(queue.qsize())]
    assert {e["type"] for e in events} == {"data", "connectivity"}
    resend = [e for e in events if e["type"] == "data" and e["resend"]]
    assert len(resend) == 1
    assert resend[0]["vin"] == "5YJSIM00000000002"


async def test_older_resend_does_not_overwrite(state: StateStore) -> None:
    relay = _relay(state)
    fresh = {
        "vin": "VIN1",
        "createdAt": "2026-10-26T10:00:05Z",
        "isResend": False,
        "data": [{"key": "BatteryLevel", "value": {"doubleValue": 60.0}}],
    }
    late = {
        "vin": "VIN1",
        "createdAt": "2026-10-26T10:00:01Z",
        "isResend": True,
        "data": [
            {"key": "BatteryLevel", "value": {"doubleValue": 61.0}},
            {"key": "Odometer", "value": {"doubleValue": 100.0}},
        ],
    }
    relay.handle_data(fresh)
    event = relay.handle_data(late)
    assert event is not None
    assert event["fields"] == {"Odometer": 100.0}
    assert event["stale"] == ["BatteryLevel"]
    fields = state.data.vehicles["VIN1"]["fields"]
    assert fields["BatteryLevel"]["v"] == 60.0


async def test_vin_allowlist(state: StateStore) -> None:
    relay = _relay(state)
    state.data.vins = ["ALLOWED"]
    record = {
        "vin": "OTHER",
        "createdAt": "2026-10-26T10:00:00Z",
        "data": [{"key": "BatteryLevel", "value": {"doubleValue": 1.0}}],
    }
    assert relay.handle_data(record) is None
    assert relay.dropped_unknown_vin == 1
    assert "OTHER" not in state.data.vehicles


async def test_connectivity_overlapping_connections(state: StateStore) -> None:
    relay = _relay(state)

    def conn(status: str, cid: str) -> dict[str, Any]:
        return relay.handle_connectivity(
            {
                "vin": "VIN1",
                "connectionId": cid,
                "status": status,
                "createdAt": "2026-10-26T10:00:00Z",
                "networkInterface": "wifi",
            }
        )

    assert conn("CONNECTED", "a")["connected"] is True
    # Reconnect reported before the old socket's disconnect.
    assert conn("CONNECTED", "b")["connected"] is True
    assert conn("DISCONNECTED", "a")["connected"] is True
    assert conn("DISCONNECTED", "b")["connected"] is False


async def test_reset_connections_broadcasts(state: StateStore) -> None:
    relay = _relay(state)
    relay.handle_connectivity(
        {"vin": "VIN1", "connectionId": "a", "status": "CONNECTED"}
    )
    queue = relay.subscribe()
    relay.reset_connections()
    event = queue.get_nowait()
    assert event == {
        "type": "connectivity",
        "vin": "VIN1",
        "status": "DISCONNECTED",
        "connection_id": None,
        "interface": None,
        "ts": "2026-10-26T12:00:00+00:00",
        "connected": False,
    }
    assert not relay.connected("VIN1")


async def test_slow_subscriber_gets_overflow(
    state: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tesla_bridge.relay.SUBSCRIBER_QUEUE", 2)
    relay = _relay(state)
    queue = relay.subscribe()
    for i in range(3):
        relay.handle_data(
            {
                "vin": "VIN1",
                "createdAt": f"2026-10-26T10:00:0{i}Z",
                "data": [{"key": "BatteryLevel", "value": {"doubleValue": float(i)}}],
            }
        )
    assert queue.get_nowait() == {"type": "overflow"}
    with pytest.raises(asyncio.QueueEmpty):
        queue.get_nowait()
