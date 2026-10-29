"""Optional automatic port forwarding through the router (UPnP IGD).

Home Assistant Core runs on the host network, so it can discover the router via
SSDP and ask it to forward the two ports the cars and Tesla need to reach the
Tesla Bridge app. Many routers ship with UPnP disabled; then the setup flow
shows manual forwarding instructions instead.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import datetime as dt
from ipaddress import IPv4Address, ip_address, ip_network
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .const import LOGGER

ST_IGD = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
)
# async-upnp-client sends only the seconds part of the timedelta; keep < 1 day.
LEASE = dt.timedelta(hours=12)
RENEW_EVERY = dt.timedelta(hours=6)
DESCRIPTION = "Tesla Bridge"


class UpnpUnavailable(Exception):
    """No UPnP-capable router was found or it refused the request."""


@dataclass
class PortMapping:
    """A forward the integration maintains."""

    external_port: int
    internal_port: int


@dataclass
class UpnpStatus:
    """What the last attempt achieved."""

    router: str | None = None
    external_ip: str | None = None
    internal_ip: str | None = None
    mapped: list[int] = field(default_factory=list)
    error: str | None = None
    updated: dt.datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "router": self.router,
            "external_ip": self.external_ip,
            "internal_ip": self.internal_ip,
            "mapped_ports": self.mapped,
            "error": self.error,
            "updated": self.updated.isoformat() if self.updated else None,
        }


_SHARED_ADDRESS_SPACE = ip_network("100.64.0.0/10")


def is_cgnat(ip: str | None) -> bool:
    """True if the router's WAN address is not a public IPv4 (CGNAT or private).

    Behind carrier-grade NAT the internet cannot reach the home at all, so port
    forwarding (manual or UPnP) cannot work.
    """
    if not ip:
        return False
    addr = ip_address(ip)
    return addr in _SHARED_ADDRESS_SPACE or addr.is_private


async def _async_find_igd(hass: HomeAssistant) -> Any:
    from async_upnp_client.aiohttp import AiohttpSessionRequester
    from async_upnp_client.client_factory import UpnpFactory
    from async_upnp_client.profiles.igd import IgdDevice

    from homeassistant.components import ssdp

    if "ssdp" not in hass.config.components:
        raise UpnpUnavailable(
            "network discovery (SSDP) is not running; enable default_config or ssdp:"
        )
    location: str | None = None
    for st in ST_IGD:
        for info in await ssdp.async_get_discovery_info_by_st(hass, st):
            if info.ssdp_location and not info.ssdp_location.startswith("http://["):
                location = info.ssdp_location
                break
        if location:
            break
    if location is None:
        raise UpnpUnavailable("no UPnP router found on the network")
    requester = AiohttpSessionRequester(async_get_clientsession(hass), with_sleep=True)
    device = await UpnpFactory(requester, non_strict=True).async_create_device(location)
    return IgdDevice(device, None)


class UpnpPortMapper:
    """Maintains the port forwards for as long as the entry is loaded."""

    def __init__(self, hass: HomeAssistant, mappings: list[PortMapping]) -> None:
        self.hass = hass
        self._mappings = mappings
        self.status = UpnpStatus()
        self._unsub: CALLBACK_TYPE | None = None
        self._igd: Any = None

    async def async_start(self) -> UpnpStatus:
        await self.async_refresh()
        self._unsub = async_track_time_interval(
            self.hass, self._async_tick, RENEW_EVERY, name="Tesla Bridge UPnP renew"
        )
        return self.status

    async def _async_tick(self, _now: dt.datetime) -> None:
        await self.async_refresh()

    async def async_refresh(self) -> UpnpStatus:
        from async_upnp_client.exceptions import UpnpError

        from homeassistant.components import network

        status = UpnpStatus(updated=dt.datetime.now(dt.UTC))
        try:
            if self._igd is None:
                self._igd = await _async_find_igd(self.hass)
            igd = self._igd
            status.router = igd.name
            router_host = igd.device.device_url.split("/")[2].split(":")[0]
            internal = await network.async_get_source_ip(self.hass, router_host)
            status.internal_ip = internal
            status.external_ip = await igd.async_get_external_ip_address()
            for mapping in self._mappings:
                await igd.async_add_port_mapping(
                    remote_host=IPv4Address("0.0.0.0"),
                    external_port=mapping.external_port,
                    protocol="TCP",
                    internal_port=mapping.internal_port,
                    internal_client=IPv4Address(internal),
                    enabled=True,
                    description=DESCRIPTION,
                    lease_duration=LEASE,
                )
                status.mapped.append(mapping.external_port)
        except (UpnpUnavailable, UpnpError, OSError, TimeoutError, ValueError) as err:
            status.error = str(err) or type(err).__name__
            self._igd = None
            LOGGER.warning("UPnP port forwarding failed: %s", status.error)
        else:
            LOGGER.info(
                "UPnP: forwarded %s on %s to %s",
                status.mapped,
                status.router,
                status.internal_ip,
            )
        self.status = status
        return status

    async def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        if self._igd is None:
            return
        from async_upnp_client.exceptions import UpnpError

        for mapping in self._mappings:
            try:
                async with asyncio.timeout(10):
                    await self._igd.async_delete_port_mapping(
                        IPv4Address("0.0.0.0"), mapping.external_port, "TCP"
                    )
            except UpnpError, OSError, TimeoutError:
                LOGGER.debug("Could not remove UPnP mapping %s", mapping.external_port)
