"""In-memory state of every vehicle, fed by the Tesla Bridge app's stream."""

from __future__ import annotations

from collections.abc import Callable, Iterable
import datetime as dt
from typing import Any

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.util import dt as dt_util

from .const import LOGGER

type Listener = Callable[[], None]


class VehicleStream:
    """Latest streamed values for one vehicle plus change listeners.

    Values are kept while the car sleeps (it simply stops sending), so entities
    keep showing the last known state; only the connectivity changes.
    """

    def __init__(self, vin: str) -> None:
        self.vin = vin
        self.values: dict[str, Any] = {}
        self.updated: dict[str, dt.datetime] = {}
        self.connected: bool | None = None
        self.last_seen: dt.datetime | None = None
        self.signals: dict[str, Any] = {}
        # Optimistic values written by commands until the car confirms them.
        self._overrides: dict[str, tuple[Any, dt.datetime]] = {}
        self._field_listeners: dict[str, set[Listener]] = {}
        self._any_listeners: set[Listener] = set()

    # ---- reading -----------------------------------------------------------

    def get(self, field: str, default: Any = None) -> Any:
        if (override := self._overrides.get(field)) is not None:
            value, expires = override
            if dt_util.utcnow() < expires:
                return value
            del self._overrides[field]
        return self.values.get(field, default)

    def has(self, field: str) -> bool:
        return field in self.values or field in self._overrides

    # ---- listeners ---------------------------------------------------------

    @callback
    def async_add_listener(
        self, listener: Listener, fields: Iterable[str] | None = None
    ) -> CALLBACK_TYPE:
        """Call ``listener`` when any of ``fields`` (or anything, if None) changes."""
        if fields is None:
            self._any_listeners.add(listener)
            return lambda: self._any_listeners.discard(listener)
        keys = tuple(fields)
        for key in keys:
            self._field_listeners.setdefault(key, set()).add(listener)

        def remove() -> None:
            for key in keys:
                self._field_listeners.get(key, set()).discard(listener)

        return remove

    @callback
    def _notify(self, changed: Iterable[str]) -> None:
        listeners: set[Listener] = set(self._any_listeners)
        for key in changed:
            listeners |= self._field_listeners.get(key, set())
        for listener in listeners:
            try:
                listener()
            except Exception:
                LOGGER.exception("Error updating an entity for %s", self.vin)

    # ---- writing -----------------------------------------------------------

    @callback
    def apply(
        self, fields: dict[str, Any], ts: dt.datetime | None = None, notify: bool = True
    ) -> set[str]:
        """Merge streamed ``fields``; returns the names whose value changed."""
        now = ts or dt_util.utcnow()
        changed: set[str] = set()
        for key, value in fields.items():
            self._overrides.pop(key, None)  # the car has spoken
            if self.values.get(key, _MISSING) != value:
                changed.add(key)
            self.values[key] = value
            self.updated[key] = now
        self.last_seen = dt_util.utcnow()
        if notify and changed:
            self._notify(changed)
        return changed

    @callback
    def set_override(self, field: str, value: Any, seconds: float = 30) -> None:
        """Show ``value`` right after a command until the stream confirms it."""
        self._overrides[field] = (
            value,
            dt_util.utcnow() + dt.timedelta(seconds=seconds),
        )
        self._notify((field,))

    @callback
    def set_connected(self, connected: bool) -> None:
        if connected != self.connected:
            self.connected = connected
            self._notify(("__connected__",))

    @callback
    def set_signals(self, signals: dict[str, Any]) -> None:
        self.signals = signals or {}
        self._notify(("__signals__",))


_MISSING = object()


class StreamHub:
    """Routes app events to the per-vehicle stores."""

    def __init__(self) -> None:
        self.vehicles: dict[str, VehicleStream] = {}
        self.bridge_connected = False
        self.hello: dict[str, Any] = {}
        self._hub_listeners: set[Listener] = set()
        self._alert_listeners: set[Callable[[str, dict[str, Any]], None]] = set()

    def vehicle(self, vin: str) -> VehicleStream:
        if vin not in self.vehicles:
            self.vehicles[vin] = VehicleStream(vin)
        return self.vehicles[vin]

    @callback
    def async_add_hub_listener(self, listener: Listener) -> CALLBACK_TYPE:
        self._hub_listeners.add(listener)
        return lambda: self._hub_listeners.discard(listener)

    @callback
    def async_add_alert_listener(
        self, listener: Callable[[str, dict[str, Any]], None]
    ) -> CALLBACK_TYPE:
        self._alert_listeners.add(listener)
        return lambda: self._alert_listeners.discard(listener)

    @callback
    def set_bridge_connected(self, connected: bool) -> None:
        if connected != self.bridge_connected:
            self.bridge_connected = connected
            for listener in list(self._hub_listeners):
                listener()

    @callback
    def handle(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "hello":
            self.hello = event
        elif kind == "snapshot":
            self._apply_snapshot(event.get("vehicles") or {})
        elif kind == "data":
            vin = event.get("vin")
            if isinstance(vin, str) and vin in self.vehicles:
                stream = self.vehicles[vin]
                stream.apply(event.get("fields") or {}, _parse(event.get("ts")))
                if signals := event.get("signals"):
                    stream.set_signals(signals)
        elif kind == "connectivity":
            vin = event.get("vin")
            if isinstance(vin, str) and vin in self.vehicles:
                self.vehicles[vin].set_connected(bool(event.get("connected")))
        elif kind in ("alert", "error"):
            vin = event.get("vin")
            if isinstance(vin, str):
                for listener in list(self._alert_listeners):
                    listener(vin, event)

    @callback
    def _apply_snapshot(self, vehicles: dict[str, Any]) -> None:
        for vin, snap in vehicles.items():
            if vin not in self.vehicles:
                continue
            stream = self.vehicles[vin]
            fields = snap.get("fields") or {}
            for key, entry in fields.items():
                ts = _parse(entry.get("ts"))
                stream.apply({key: entry.get("v")}, ts, notify=False)
            stream.set_signals(snap.get("signals") or {})
            stream.connected = bool(snap.get("connected"))
            stream._notify(set(fields) | {"__connected__"})


def _parse(raw: Any) -> dt.datetime | None:
    return dt_util.parse_datetime(raw) if isinstance(raw, str) else None
