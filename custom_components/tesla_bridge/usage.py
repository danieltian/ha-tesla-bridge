"""What Tesla Bridge costs on Tesla's Fleet API, estimated from its own traffic.

Tesla bills four categories (developer.tesla.com/docs/fleet-api/billing-and-limits):
streamed values, device data requests, commands and wakes, minus a $10 monthly
discount. A request counts when Tesla answered it with a status below 500.
Streamed values are counted by the app as they arrive; requests are counted
here as Tesla Bridge makes them.

This only sees Tesla Bridge. Another integration using the same developer app
(Tesla Fleet, for example) is billed to the same account without showing up
here, so Tesla's billing and usage page stays the authoritative number.
"""

from __future__ import annotations

import calendar
from collections.abc import Callable, Iterable
import datetime as dt
import re
from typing import TYPE_CHECKING, Any

from tesla_fleet_api.exceptions import TeslaFleetError

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MONTHLY_CREDIT_USD, SIGNALS_PER_DOLLAR

if TYPE_CHECKING:
    from tesla_fleet_api import TeslaFleetApi

    from .models import VehicleData

STREAMING = "streaming"
DATA = "data"
COMMANDS = "commands"
WAKES = "wakes"
OTHER = "other"

# USD per value or request (Tesla's US prices). Everything Tesla does not list
# as billed (vehicle state, energy sites, streaming setup, pairing checks) is
# counted under "other" but priced at nothing.
PRICES: dict[str, float] = {
    STREAMING: 1 / SIGNALS_PER_DOLLAR,
    DATA: 1 / 500,
    COMMANDS: 1 / 1_000,
    WAKES: 1 / 50,
    OTHER: 0.0,
}
LABELS = {
    STREAMING: "streamed values",
    DATA: "data requests",
    COMMANDS: "commands",
    WAKES: "wake-ups",
    OTHER: "other requests",
}
# Warn while there is still room to act before the credit runs out.
WARN_AT_USD = 0.8 * MONTHLY_CREDIT_USD
# Projections from the first hours of a month swing wildly; spread the cost
# over at least this many days.
MIN_PROJECTION_DAYS = 3.0
SAVE_DELAY = 60

_VEHICLE = r"api/1/vehicles/[^/]+"
_CATEGORIES = (
    (re.compile(rf"{_VEHICLE}/vehicle_data"), DATA),
    (re.compile(rf"{_VEHICLE}/wake_up"), WAKES),
    (re.compile(rf"{_VEHICLE}/(command/.+|signed_command)"), COMMANDS),
)


def billing_category(path: str) -> str:
    """Tesla's billing category for a Fleet API request path."""
    path = path.split("?", 1)[0].strip("/")
    for pattern, category in _CATEGORIES:
        if pattern.fullmatch(path):
            return category
    return OTHER


def _month(now: dt.datetime) -> str:
    return now.date().isoformat()[:7]


def streamed_this_month(vehicles: Iterable[VehicleData], now: dt.datetime) -> int:
    """Values the app received this month, summed over the cars (UTC months)."""
    month = _month(now)
    return sum(
        int(v.stream.signals.get("month_count") or 0)
        for v in vehicles
        if v.stream.signals and v.stream.signals.get("month") == month
    )


class ApiUsage:
    """Counts Tesla Bridge's Fleet API requests per billing category and month."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, 1, f"{DOMAIN}.{entry_id}.usage"
        )
        self.month = _month(dt_util.utcnow())
        self.counts: dict[str, int] = {}
        self._listeners: list[CALLBACK_TYPE] = []

    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        if stored.get("month") == self.month:
            self.counts = {k: int(v) for k, v in (stored.get("counts") or {}).items()}

    def instrument(self, tesla: TeslaFleetApi) -> None:
        """Count every request ``tesla`` sends.

        Vehicle and energy-site objects copy the client's ``_request`` when they
        are created, so this must run before any of them exist.
        """
        send = tesla._request

        async def counted(method: Any, path: str, *args: Any, **kwargs: Any) -> Any:
            try:
                result = await send(method, path, *args, **kwargs)
            except TeslaFleetError as err:
                # Tesla bills answered requests below 500, errors included.
                if err.status is not None and err.status < 500:
                    self.record(billing_category(path))
                raise
            self.record(billing_category(path))
            return result

        tesla._request = counted  # type: ignore[method-assign]

    @callback
    def record(self, category: str) -> None:
        self._roll()
        self.counts[category] = self.counts.get(category, 0) + 1
        self._store.async_delay_save(self._data, SAVE_DELAY)
        for listener in list(self._listeners):
            listener()

    def _roll(self) -> None:
        month = _month(dt_util.utcnow())
        if month != self.month:
            self.month, self.counts = month, {}

    def _data(self) -> dict[str, Any]:
        return {"month": self.month, "counts": self.counts}

    @callback
    def async_add_listener(self, listener: CALLBACK_TYPE) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def summary(self, vehicles: Iterable[VehicleData]) -> dict[str, Any]:
        """This month so far: counts, cost per category, total and projection."""
        self._roll()
        now = dt_util.utcnow()
        counts = {category: self.counts.get(category, 0) for category in PRICES}
        counts[STREAMING] = streamed_this_month(vehicles, now)
        costs = {category: n * PRICES[category] for category, n in counts.items()}
        total = sum(costs.values())
        days = calendar.monthrange(now.year, now.month)[1]
        elapsed = now - now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elapsed_days = max(elapsed.total_seconds() / 86400, MIN_PROJECTION_DAYS)
        return {
            "month": self.month,
            "counts": counts,
            "costs": costs,
            "total": total,
            "projected": max(total, total * days / elapsed_days),
            "top": max(costs, key=lambda c: costs[c]),
            "requests": sum(n for c, n in counts.items() if c != STREAMING),
        }
