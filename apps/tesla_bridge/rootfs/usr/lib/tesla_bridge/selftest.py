"""Reachability checks the app can do from inside the home network.

A car parked at home on Wi-Fi resolves the DuckDNS name to the public IP and
needs the router to loop the connection back in (NAT loopback / hairpin).
Connecting to our own public address exercises exactly that path. A failure
means either the port forward is missing or the router can't hairpin; a car
on LTE would still work in the second case.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import socket
import ssl
from typing import Any

import aiohttp

from .const import PUBLIC_KEY_PATH

_LOGGER = logging.getLogger(__name__)


async def resolve_ipv4(host: str) -> str | None:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, family=socket.AF_INET)
    except OSError:
        return None
    return infos[0][4][0] if infos else None


async def check_telemetry_port(
    ip: str, port: int, hostname: str, ca_pem: str, timeout: float = 10.0
) -> tuple[bool, str]:
    """TLS-connect to ``ip:port`` and verify it is our telemetry server."""
    context = ssl.create_default_context(cadata=ca_pem)
    context.check_hostname = True
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(
                ip, port, ssl=context, server_hostname=hostname
            )
    except ssl.SSLCertVerificationError:
        # verify_message would name hosts; the status ends up in diagnostics.
        return (
            False,
            f"something else answered on port {port} "
            "(its certificate is not this app's)",
        )
    except ssl.SSLError as err:
        # TLS 1.3 completes the client side before the server rejects our
        # missing client certificate; an alert here still proves it was us.
        if "CERTIFICATE_REQUIRED" in str(err) or "certificate required" in str(err):
            return True, "ok"
        return False, f"TLS error: {err}"
    except (OSError, TimeoutError) as err:
        return (
            False,
            f"no connection to the public address on port {port} ({type(err).__name__})",
        )
    writer.close()
    with contextlib.suppress(ssl.SSLError, OSError):
        await writer.wait_closed()
    del reader
    return True, "ok"


async def check_key_url(
    session: aiohttp.ClientSession, domain: str, expected_pem: str | None
) -> tuple[bool, str]:
    url = f"https://{domain}{PUBLIC_KEY_PATH}"
    try:
        async with asyncio.timeout(15):
            async with session.get(url) as resp:
                body = await resp.text()
    except (aiohttp.ClientError, TimeoutError) as err:
        return False, f"public key URL not reachable ({type(err).__name__})"
    if resp.status != 200:
        return False, f"public key URL returned HTTP {resp.status}"
    if expected_pem and body.strip() != expected_pem.strip():
        return False, "public key URL serves a different key"
    return True, "ok"


class SelfTest:
    """Runs the loopback checks periodically and keeps the latest result."""

    def __init__(self) -> None:
        self.result: dict[str, Any] = {"ran_at": None}

    async def run_once(
        self,
        session: aiohttp.ClientSession,
        domain: str,
        public_ip: str | None,
        telemetry_port: int,
        ca_pem: str,
        public_key_pem: str | None,
        key_server_ready: bool,
    ) -> dict[str, Any]:
        resolved = await resolve_ipv4(domain)
        result: dict[str, Any] = {
            "ran_at": dt.datetime.now(dt.UTC).isoformat(),
            "resolved_ip": resolved,
            "public_ip": public_ip,
            "dns_matches": resolved is not None and resolved == public_ip,
        }
        target = resolved or public_ip
        if target:
            ok, detail = await check_telemetry_port(
                target, telemetry_port, domain, ca_pem
            )
        else:
            ok, detail = False, "the DuckDNS name does not resolve"
        result["telemetry_loopback"] = {"ok": ok, "detail": detail}
        if key_server_ready:
            ok, detail = await check_key_url(session, domain, public_key_pem)
            result["key_loopback"] = {"ok": ok, "detail": detail}
        else:
            result["key_loopback"] = {"ok": False, "detail": "no certificate yet"}
        self.result = result
        _LOGGER.debug("Self-test: %s", result)
        return result
