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
# A rate from the first hours of tracking swings wildly (one drive, or a
# night of nothing); measure it over at least a day.
MIN_PROJECTION_DAYS = 1.0
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


def _month_start(now: dt.datetime) -> dt.datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


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
        now = dt_util.utcnow()
        self.month = _month(now)
        self.counts: dict[str, int] = {}
        # When counting started this month. Projections use the rate since then,
        # so a first, partial month is not spread over days nothing was counted.
        self.since = now
        self._listeners: list[CALLBACK_TYPE] = []

    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        if stored.get("month") == self.month:
            self.counts = {k: int(v) for k, v in (stored.get("counts") or {}).items()}
            # Saved before "since" existed: count from now (a higher, safer rate).
            self.since = dt_util.parse_datetime(stored.get("since") or "") or self.since
        elif stored:
            # Counting ran last month, so it was already on when this one began.
            self.since = _month_start(self.since)
        self._store.async_delay_save(self._data, SAVE_DELAY)

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
        now = dt_util.utcnow()
        if _month(now) != self.month:
            self.month, self.counts, self.since = _month(now), {}, _month_start(now)

    def _data(self) -> dict[str, Any]:
        return {
            "month": self.month,
            "counts": self.counts,
            "since": self.since.isoformat(),
        }

    @callback
    def async_add_listener(self, listener: CALLBACK_TYPE) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def summary(self, vehicles: Iterable[VehicleData]) -> dict[str, Any]:
        """This month so far: counts, costs, total and projections.

        ``projected`` is this month's bill: the cost so far plus the rate since
        counting started for the rest of the month. ``full_month`` is what a
        whole month at that rate would cost, which matters in a first,
        partial month.
        """
        self._roll()
        now = dt_util.utcnow()
        counts = {category: self.counts.get(category, 0) for category in PRICES}
        counts[STREAMING] = streamed_this_month(vehicles, now)
        costs = {category: n * PRICES[category] for category, n in counts.items()}
        total = sum(costs.values())
        start = max(self.since, _month_start(now))
        tracked = max((now - start).total_seconds() / 86400, MIN_PROJECTION_DAYS)
        per_day = total / tracked
        days = calendar.monthrange(now.year, now.month)[1]
        month_end = _month_start(now) + dt.timedelta(days=days)
        remaining = (month_end - now).total_seconds() / 86400
        return {
            "month": self.month,
            "since": start.isoformat(),
            "counts": counts,
            "costs": costs,
            "total": total,
            "projected": total + per_day * remaining,
            "full_month": per_day * days,
            "top": max(costs, key=lambda c: costs[c]),
            "requests": sum(n for c, n in counts.items() if c != STREAMING),
        }
