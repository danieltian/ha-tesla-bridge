"""Keep the DuckDNS A record pointed at this home's public IPv4 address."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import datetime as dt
import logging
import socket

import aiohttp

from .const import DUCKDNS_UPDATE_URL

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = 300.0
BACKOFF = (60.0, 300.0, 900.0, 1800.0)


class DuckDNSError(Exception):
    """DuckDNS rejected the update (bad token or domain) or was unreachable."""


@dataclass
class DuckDNSStatus:
    """Outcome of the most recent update attempt."""

    ip: str | None = None
    last_success: dt.datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    changes: int = field(default=0)

    def as_dict(self) -> dict[str, object]:
        return {
            "ip": self.ip,
            "last_success": self.last_success.isoformat()
            if self.last_success
            else None,
            "last_error": self.last_error,
            "ok": self.consecutive_failures == 0 and self.last_success is not None,
        }


def parse_response(body: str) -> tuple[str | None, bool]:
    """Parse a ``verbose=true`` reply: ``OK\\n<ipv4>\\n<ipv6>\\nUPDATED|NOCHANGE``."""
    lines = body.strip().splitlines()
    if not lines or lines[0].strip() != "OK":
        raise DuckDNSError("DuckDNS answered KO (check the domain and token)")
    ip = lines[1].strip() if len(lines) > 1 and lines[1].strip() else None
    changed = len(lines) > 3 and lines[3].strip() == "UPDATED"
    return ip, changed


class DuckDNSUpdater:
    """Periodically refreshes the A record; IPv4 only on purpose.

    DuckDNS fills in the address the request came from. Forcing an IPv4
    connection makes sure that is the WAN IPv4 the router forwards ports on,
    never an IPv6 address the cars would fail to reach.
    """

    def __init__(
        self, session: aiohttp.ClientSession, subdomain: str, token: str
    ) -> None:
        self._session = session
        self._subdomain = subdomain
        self._token = token
        self.status = DuckDNSStatus()
        self._wake = asyncio.Event()

    async def update(self) -> DuckDNSStatus:
        params = {
            "domains": self._subdomain,
            "token": self._token,
            "ip": "",
            "verbose": "true",
        }
        try:
            async with asyncio.timeout(30):
                async with self._session.get(DUCKDNS_UPDATE_URL, params=params) as resp:
                    resp.raise_for_status()
                    body = await resp.text()
            ip, changed = parse_response(body)
        except aiohttp.ClientResponseError as err:
            # str(err) would include the request URL, and with it the token.
            return self._failed(f"DuckDNS returned HTTP {err.status}")
        except (aiohttp.ClientError, TimeoutError) as err:
            return self._failed(f"DuckDNS not reachable ({type(err).__name__})")
        except DuckDNSError as err:
            return self._failed(str(err))
        if changed or ip != self.status.ip:
            _LOGGER.info("DuckDNS %s now points to %s", self._subdomain, ip)
            self.status.changes += 1
        self.status.ip = ip
        self.status.last_success = dt.datetime.now(dt.UTC)
        self.status.last_error = None
        self.status.consecutive_failures = 0
        return self.status

    def _failed(self, message: str) -> DuckDNSStatus:
        # Belt and braces: the token must never reach logs or the status API.
        message = message.replace(self._token, "**REDACTED**")
        self.status.consecutive_failures += 1
        self.status.last_error = message
        _LOGGER.warning("DuckDNS update failed: %s", message)
        return self.status

    def trigger(self) -> None:
        """Run an update now (e.g. after the integration reports an IP change)."""
        self._wake.set()

    async def run(self) -> None:
        while True:
            await self.update()
            failures = self.status.consecutive_failures
            delay = (
                UPDATE_INTERVAL
                if failures == 0
                else BACKOFF[min(failures, len(BACKOFF)) - 1]
            )
            self._wake.clear()
            try:
                async with asyncio.timeout(delay):
                    await self._wake.wait()
            except TimeoutError:
                pass


def ipv4_session() -> aiohttp.ClientSession:
    """A client session whose connections only use IPv4."""
    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    return aiohttp.ClientSession(connector=connector)
