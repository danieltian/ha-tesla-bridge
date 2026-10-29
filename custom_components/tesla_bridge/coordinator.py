"""Polling coordinators: Fleet API energy data and the app's status."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from tesla_fleet_api.exceptions import (
    InvalidToken,
    LoginRequired,
    OAuthExpired,
    RateLimited,
    TeslaFleetError,
)
from tesla_fleet_api.tesla import EnergySite

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import LOGGER

if TYPE_CHECKING:
    from .bridge_client import BridgeClient

SITE_INFO_INTERVAL = dt.timedelta(minutes=5)
LIVE_INTERVAL = dt.timedelta(seconds=60)
BRIDGE_STATUS_INTERVAL = dt.timedelta(minutes=2)


def flatten(data: dict[str, Any], parent: str | None = None) -> dict[str, Any]:
    """Flatten nested dicts into ``parent_child`` keys."""
    result: dict[str, Any] = {}
    for key, value in data.items():
        name = f"{parent}_{key}" if parent else key
        if isinstance(value, dict):
            result.update(flatten(value, name))
        else:
            result[name] = value
    return result


class _EnergyCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Shared error handling for energy-site endpoints (free to call)."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: EnergySite,
        name: str,
        interval: dt.timedelta,
        initial: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            hass, LOGGER, config_entry=entry, name=name, update_interval=interval
        )
        self.api = api
        self._interval = interval
        self.data = initial or {}

    async def _fetch(self) -> dict[str, Any]:
        raise NotImplementedError

    async def _async_update_data(self) -> dict[str, Any]:
        self.update_interval = self._interval
        try:
            return await self._fetch()
        except RateLimited as err:
            after = err.data.get("after") if isinstance(err.data, dict) else None
            if after:
                self.update_interval = dt.timedelta(seconds=int(after))
            LOGGER.warning("%s rate limited", self.name)
            return self.data
        except LoginRequired as err:
            raise ConfigEntryAuthFailed from err
        except (InvalidToken, OAuthExpired) as err:
            raise UpdateFailed(err.message) from err
        except TeslaFleetError as err:
            raise UpdateFailed(err.message) from err


class EnergySiteInfoCoordinator(_EnergyCoordinator):
    """Site settings (reserve, mode, storm watch, export rule) from the cloud."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, api: EnergySite, product: dict
    ) -> None:
        super().__init__(
            hass,
            entry,
            api,
            "Tesla energy site info",
            SITE_INFO_INTERVAL,
            flatten(product),
        )

    async def _fetch(self) -> dict[str, Any]:
        return flatten((await self.api.site_info())["response"])


class EnergySiteLiveCoordinator(_EnergyCoordinator):
    """Cloud live status; the fallback when the gateway can't be polled locally."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, api: EnergySite
    ) -> None:
        super().__init__(hass, entry, api, "Tesla energy site live", LIVE_INTERVAL)

    async def _fetch(self) -> dict[str, Any]:
        data = (await self.api.live_status())["response"]
        if not isinstance(data, dict):
            return self.data
        data.pop("wall_connectors", None)
        return data


class BridgeStatusCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """The app's health (DuckDNS, certificates, server, self-test)."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: BridgeClient
    ) -> None:
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name="Tesla Bridge app status",
            update_interval=BRIDGE_STATUS_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> dict[str, Any]:
        from .bridge_client import BridgeError

        try:
            return await self.client.status()
        except BridgeError as err:
            raise UpdateFailed(str(err)) from err
