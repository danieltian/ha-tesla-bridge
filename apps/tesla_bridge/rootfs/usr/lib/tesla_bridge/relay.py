"""Turn fleet-telemetry's ZMQ output into a simple event stream with a snapshot.

fleet-telemetry publishes protojson (``transmit_decoded_records``) on a PUB
socket as two frames ``[<namespace>_<record type>, <json>]``. The relay:

* flattens each typed protobuf value to plain JSON
  (``{"doubleValue": 71.2}`` -> ``71.2``, enums -> their name, invalid -> None),
* keeps the newest value of every field per VIN (ignoring resends that are
  older than what it already has) and persists that snapshot,
* tracks open connections per VIN so "online" never depends on polling,
* counts streamed signals per VIN per day/month (Fleet API billing), and
* fans every event out to connected integration clients.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import datetime as dt
import json
import logging
from typing import Any

from .const import ZMQ_ADDR, ZMQ_NAMESPACE
from .state import StateStore

_LOGGER = logging.getLogger(__name__)

SUBSCRIBER_QUEUE = 1000

# Protobuf messages that are passed through as objects rather than enums/scalars.
_OBJECT_VALUES = {
    "locationValue",
    "doorValue",
    "tireLocationValue",
    "timeValue",
}
_NUMERIC_VALUES = {"intValue", "floatValue", "doubleValue"}


def normalize_value(value: Any) -> Any:
    """Flatten one protojson ``Value`` message to a plain JSON value."""
    if not isinstance(value, dict) or not value:
        return None
    if value.get("invalid"):
        return None
    # protojson with EmitUnpopulated may include defaults for the oneof; the
    # populated member is the only key present in practice, but be defensive.
    for key, raw in value.items():
        if key == "invalid":
            continue
        if key == "stringValue":
            return raw
        if key in _NUMERIC_VALUES:
            return raw
        if key == "longValue":
            # int64 is encoded as a JSON string by protojson.
            try:
                return int(raw)
            except (TypeError, ValueError):
                return raw
        if key == "booleanValue":
            return bool(raw)
        if key in _OBJECT_VALUES:
            if key == "tireLocationValue" and isinstance(raw, dict):
                # Only the four road wheels matter for cars.
                return {
                    tire: bool(raw.get(tire))
                    for tire in ("frontLeft", "frontRight", "rearLeft", "rearRight")
                }
            return raw
        # Every other oneof member is an enum, emitted as its name string.
        return raw
    return None


def _parse_ts(raw: Any) -> dt.datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(ts: dt.datetime) -> str:
    return ts.astimezone(dt.UTC).isoformat()


class Relay:
    """Consumes the ZMQ socket and broadcasts normalized events."""

    def __init__(self, state: StateStore, zmq_addr: str = ZMQ_ADDR) -> None:
        self._state = state
        self._zmq_addr = zmq_addr
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        # vin -> {connection_id: {"interface": str|None, "since": iso}}
        self._connections: dict[str, dict[str, dict[str, Any]]] = {}
        self.records = 0
        self.last_record: dt.datetime | None = None
        self.dropped_unknown_vin = 0
        self._now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)

    # ---- subscribers -------------------------------------------------------

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(SUBSCRIBER_QUEUE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def _broadcast(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A stuck client must not stall telemetry intake. Replace its
                # backlog with an overflow marker; the WebSocket handler then
                # closes it and the client resynchronises from a new snapshot.
                _LOGGER.warning("Dropping a slow stream client")
                self._subscribers.discard(queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"type": "overflow"})

    # ---- snapshot ----------------------------------------------------------

    def connected(self, vin: str) -> bool:
        return bool(self._connections.get(vin))

    def snapshot(self) -> dict[str, Any]:
        vehicles: dict[str, Any] = {}
        for vin, stored in self._state.data.vehicles.items():
            vehicles[vin] = {
                "fields": stored.get("fields", {}),
                "last_seen": stored.get("last_seen"),
                "connected": self.connected(vin),
                "connections": list(self._connections.get(vin, {}).values()),
                "signals": self.current_signals(vin),
            }
        for vin in self._connections:
            vehicles.setdefault(
                vin,
                {
                    "fields": {},
                    "last_seen": None,
                    "connected": self.connected(vin),
                    "connections": list(self._connections[vin].values()),
                    "signals": {},
                },
            )
        return {"type": "snapshot", "vehicles": vehicles}

    def stats(self) -> dict[str, Any]:
        return {
            "records": self.records,
            "last_record": _iso(self.last_record) if self.last_record else None,
            "subscribers": len(self._subscribers),
            "dropped_unknown_vin": self.dropped_unknown_vin,
            "vehicles": {
                vin: {
                    "connected": self.connected(vin),
                    "last_seen": stored.get("last_seen"),
                    "signals": self.current_signals(vin),
                }
                for vin, stored in self._state.data.vehicles.items()
            },
        }

    # ---- record handling ----------------------------------------------------

    def _vin_allowed(self, vin: str) -> bool:
        allowed = self._state.data.vins
        if not allowed or vin in allowed:
            return True
        self.dropped_unknown_vin += 1
        return False

    def current_signals(self, vin: str) -> dict[str, Any]:
        """Counters as of now: a car that sent nothing today shows 0 for today.

        The stored counters only roll over when a record arrives, so a parked
        car would otherwise keep reporting yesterday's (or last month's) total.
        """
        entry = dict(self._state.data.signals.get(vin) or {})
        if not entry:
            return entry
        day = self._now().date().isoformat()
        if entry.get("day") != day:
            entry["day"], entry["count"] = day, 0
        if entry.get("month") != day[:7]:
            entry["month"], entry["month_count"] = day[:7], 0
        return entry

    def _count_signals(self, vin: str, count: int, now: dt.datetime) -> dict[str, Any]:
        day = now.date().isoformat()
        month = day[:7]
        entry = self._state.data.signals.setdefault(vin, {})
        if entry.get("day") != day:
            entry["day"], entry["count"] = day, 0
        if entry.get("month") != month:
            entry["month"], entry["month_count"] = month, 0
        entry["count"] = entry.get("count", 0) + count
        entry["month_count"] = entry.get("month_count", 0) + count
        return entry

    def handle_data(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        vin = payload.get("vin")
        if not isinstance(vin, str) or not self._vin_allowed(vin):
            return None
        now = self._now()
        created = _parse_ts(payload.get("createdAt")) or now
        resend = bool(payload.get("isResend"))
        fields: dict[str, Any] = {}
        for datum in payload.get("data") or []:
            key = datum.get("key")
            if isinstance(key, str):
                fields[key] = normalize_value(datum.get("value"))
        if not fields:
            return None

        stored = self._state.data.vehicles.setdefault(vin, {"fields": {}})
        stored_fields: dict[str, Any] = stored.setdefault("fields", {})
        created_iso = _iso(created)
        newer: dict[str, Any] = {}
        for key, value in fields.items():
            previous = stored_fields.get(key)
            previous_ts = _parse_ts(previous.get("ts")) if previous else None
            if previous_ts is not None and created < previous_ts:
                continue  # A late resend must not overwrite fresher data.
            stored_fields[key] = {"v": value, "ts": created_iso}
            newer[key] = value
        stored["last_seen"] = _iso(now)
        signals = self._count_signals(vin, len(fields), now)
        self.records += 1
        self.last_record = now
        self._state.schedule_save()

        event = {
            "type": "data",
            "vin": vin,
            "ts": created_iso,
            "resend": resend,
            "fields": newer,
            "stale": sorted(set(fields) - set(newer)),
            "signals": signals,
        }
        self._broadcast(event)
        return event

    def handle_connectivity(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        vin = payload.get("vin")
        conn_id = payload.get("connectionId")
        if not isinstance(vin, str) or not isinstance(conn_id, str):
            return None
        if not self._vin_allowed(vin):
            return None
        status = str(payload.get("status", "")).upper()
        conns = self._connections.setdefault(vin, {})
        ts = _parse_ts(payload.get("createdAt")) or self._now()
        if status == "CONNECTED":
            conns[conn_id] = {
                "id": conn_id,
                "interface": payload.get("networkInterface"),
                "since": _iso(ts),
            }
        elif status == "DISCONNECTED":
            # A reconnect can report the new CONNECTED before the old
            # DISCONNECTED, so track connections individually.
            conns.pop(conn_id, None)
        else:
            return None
        event = {
            "type": "connectivity",
            "vin": vin,
            "status": status,
            "connection_id": conn_id,
            "interface": payload.get("networkInterface"),
            "ts": _iso(ts),
            "connected": bool(conns),
        }
        self._broadcast(event)
        return event

    def handle_passthrough(
        self, kind: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        vin = payload.get("vin")
        if isinstance(vin, str) and not self._vin_allowed(vin):
            return None
        event = {"type": kind, "vin": vin, "payload": payload}
        self._broadcast(event)
        return event

    def reset_connections(self) -> None:
        """fleet-telemetry restarted: every socket it held is gone."""
        now = _iso(self._now())
        for vin, conns in self._connections.items():
            if not conns:
                continue
            conns.clear()
            self._broadcast(
                {
                    "type": "connectivity",
                    "vin": vin,
                    "status": "DISCONNECTED",
                    "connection_id": None,
                    "interface": None,
                    "ts": now,
                    "connected": False,
                }
            )

    def dispatch(self, topic: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        kind = topic.removeprefix(f"{ZMQ_NAMESPACE}_")
        if kind == "V":
            return self.handle_data(payload)
        if kind == "connectivity":
            return self.handle_connectivity(payload)
        if kind in ("alerts", "errors"):
            return self.handle_passthrough(kind.rstrip("s"), payload)
        _LOGGER.debug("Ignoring record type %s", kind)
        return None

    # ---- transport ---------------------------------------------------------

    async def run(self) -> None:
        import zmq
        import zmq.asyncio

        ctx = zmq.asyncio.Context()
        sock = ctx.socket(zmq.SUB)
        sock.setsockopt(zmq.SUBSCRIBE, f"{ZMQ_NAMESPACE}_".encode())
        sock.setsockopt(zmq.RCVHWM, 100_000)
        sock.connect(self._zmq_addr)
        _LOGGER.info("Relay subscribed to %s", self._zmq_addr)
        try:
            while True:
                frames = await sock.recv_multipart()
                if len(frames) != 2:
                    continue
                try:
                    payload = json.loads(frames[1])
                except ValueError:
                    _LOGGER.warning("Undecodable record on %s", frames[0][:40])
                    continue
                try:
                    self.dispatch(frames[0].decode(errors="replace"), payload)
                except Exception:
                    _LOGGER.exception("Failed to handle a telemetry record")
        finally:
            sock.close(linger=0)
            ctx.term()
