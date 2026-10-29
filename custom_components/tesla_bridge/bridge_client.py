"""Client for the Tesla Bridge app's internal API (REST + WebSocket)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import aiohttp

from .const import BRIDGE_API_VERSION, LOGGER

RECONNECT_BACKOFF = (1, 2, 5, 10, 30, 60)


class BridgeError(Exception):
    """The app could not be reached or rejected a request."""


class BridgeAuthError(BridgeError):
    """The app rejected our token (it was reinstalled or reset)."""


class BridgeVersionError(BridgeError):
    """The app speaks an API version this integration does not understand."""


class BridgeClient:
    """Talks to the app over the hassio network."""

    def __init__(
        self, session: aiohttp.ClientSession, host: str, port: int, token: str
    ) -> None:
        self._session = session
        self.host = host
        self.port = port
        self._token = token

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/api/v1"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(
        self, method: str, path: str, *, timeout: float = 15, **kwargs: Any
    ) -> aiohttp.ClientResponse:
        try:
            async with asyncio.timeout(timeout):
                resp = await self._session.request(
                    method, f"{self.base_url}{path}", headers=self._headers(), **kwargs
                )
                await resp.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise BridgeError(f"Tesla Bridge app not reachable: {err}") from err
        if resp.status == 401:
            raise BridgeAuthError("Tesla Bridge app rejected the token")
        if resp.status >= 400:
            raise BridgeError(
                f"Tesla Bridge app: HTTP {resp.status} {await resp.text()}"
            )
        return resp

    async def status(self) -> dict[str, Any]:
        resp = await self._request("GET", "/status")
        data: dict[str, Any] = await resp.json()
        if int(data.get("api", 0)) != BRIDGE_API_VERSION:
            raise BridgeVersionError(
                f"Tesla Bridge app API {data.get('api')} is not supported; "
                "update the app and the integration together"
            )
        return data

    async def ca_pem(self) -> str:
        resp = await self._request("GET", "/ca")
        return await resp.text()

    async def put_public_key(self, pem: str) -> dict[str, Any]:
        resp = await self._request("PUT", "/public-key", data=pem)
        return await resp.json()

    async def put_vins(self, vins: list[str]) -> None:
        await self._request("PUT", "/vins", json=vins)

    async def stream_forever(
        self,
        on_event: Callable[[dict[str, Any]], None],
        on_connection: Callable[[bool], None],
    ) -> None:
        """Consume the event stream, reconnecting until cancelled."""
        attempt = 0
        while True:
            try:
                async with self._session.ws_connect(
                    f"{self.base_url}/stream", headers=self._headers(), heartbeat=30
                ) as ws:
                    attempt = 0
                    async for msg in ws:
                        if msg.type is not aiohttp.WSMsgType.TEXT:
                            continue
                        event = msg.json()
                        if event.get("type") == "hello":
                            if int(event.get("api", 0)) != BRIDGE_API_VERSION:
                                raise BridgeVersionError(
                                    f"Unsupported Tesla Bridge app API {event.get('api')}"
                                )
                            on_connection(True)
                        on_event(event)
                    LOGGER.debug("Tesla Bridge stream closed (%s)", ws.close_code)
            except BridgeVersionError:
                on_connection(False)
                raise
            except aiohttp.WSServerHandshakeError as err:
                on_connection(False)
                if err.status == 401:
                    raise BridgeAuthError(
                        "Tesla Bridge app rejected the token"
                    ) from err
                LOGGER.debug("Tesla Bridge stream handshake failed: %s", err)
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                LOGGER.debug("Tesla Bridge stream error: %s", err)
            on_connection(False)
            delay = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
            attempt += 1
            await asyncio.sleep(delay)
