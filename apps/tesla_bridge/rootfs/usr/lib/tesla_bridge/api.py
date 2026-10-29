"""Internal HTTP + WebSocket API for the Tesla Bridge integration.

Listens on the hassio network only (the port is not mapped to the host).
Everything except ``/api/v1/health`` (used by the Supervisor watchdog)
requires the bearer token the app announced through discovery.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from . import API_VERSION, __version__

if TYPE_CHECKING:
    from .bridge import Bridge

_LOGGER = logging.getLogger(__name__)

HEARTBEAT = 30.0
BRIDGE_KEY: web.AppKey[Bridge] = web.AppKey("bridge")


def validate_public_key(pem: str) -> str:
    """Return a normalized PEM if ``pem`` is a P-256 public key (Tesla's requirement)."""
    try:
        key = load_pem_public_key(pem.encode())
    except ValueError as err:
        raise web.HTTPBadRequest(text="not a PEM public key") from err
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(
        key.curve, ec.SECP256R1
    ):
        raise web.HTTPBadRequest(text="Tesla requires a prime256v1 (P-256) key")
    return pem.strip() + "\n"


@web.middleware
async def auth_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    if request.path == "/api/v1/health":
        return await handler(request)
    bridge = request.app[BRIDGE_KEY]
    header = request.headers.get("Authorization", "")
    token = header.removeprefix("Bearer ").strip()
    if not token or not hmac.compare_digest(token, bridge.state.data.api_token):
        raise web.HTTPUnauthorized
    return await handler(request)


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "version": __version__})


async def status(request: web.Request) -> web.Response:
    return web.json_response(request.app[BRIDGE_KEY].status())


async def get_ca(request: web.Request) -> web.Response:
    bridge = request.app[BRIDGE_KEY]
    if bridge.pki is None:
        raise web.HTTPServiceUnavailable(
            text="telemetry CA not created yet (no domain)"
        )
    return web.Response(text=bridge.pki.ca_pem, content_type="application/x-pem-file")


async def put_public_key(request: web.Request) -> web.Response:
    bridge = request.app[BRIDGE_KEY]
    pem = validate_public_key(await request.text())
    await bridge.set_public_key(pem)
    return web.json_response(bridge.public_key_status())


async def put_vins(request: web.Request) -> web.Response:
    bridge = request.app[BRIDGE_KEY]
    try:
        vins = await request.json()
    except json.JSONDecodeError as err:
        raise web.HTTPBadRequest(text="expected a JSON list of VINs") from err
    if not isinstance(vins, list) or not all(isinstance(v, str) for v in vins):
        raise web.HTTPBadRequest(text="expected a JSON list of VINs")
    bridge.set_vins(sorted({v.strip().upper() for v in vins if v.strip()}))
    return web.json_response({"vins": bridge.state.data.vins})


async def stream(request: web.Request) -> web.WebSocketResponse:
    bridge = request.app[BRIDGE_KEY]
    ws = web.WebSocketResponse(heartbeat=HEARTBEAT)
    await ws.prepare(request)
    queue = bridge.relay.subscribe()
    try:
        await ws.send_json(bridge.hello())
        await ws.send_json(bridge.relay.snapshot())
        sender = asyncio.create_task(_forward(ws, queue))
        try:
            async for msg in ws:
                # The integration does not send anything yet; ignore pings.
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
    finally:
        bridge.relay.unsubscribe(queue)
    return ws


async def _forward(
    ws: web.WebSocketResponse, queue: asyncio.Queue[dict[str, Any]]
) -> None:
    while True:
        event = await queue.get()
        if event.get("type") == "overflow":
            await ws.close(code=4000, message=b"overflow")
            return
        await ws.send_json(event)


def create_api_app(bridge: Bridge) -> web.Application:
    app = web.Application(middlewares=[auth_middleware], client_max_size=64 * 1024)
    app[BRIDGE_KEY] = bridge
    app.router.add_get("/api/v1/health", health)
    app.router.add_get("/api/v1/status", status)
    app.router.add_get("/api/v1/ca", get_ca)
    app.router.add_put("/api/v1/public-key", put_public_key)
    app.router.add_put("/api/v1/vins", put_vins)
    app.router.add_get("/api/v1/stream", stream)
    return app


def hello_message(bridge: Bridge) -> dict[str, Any]:
    return {
        "type": "hello",
        "api": API_VERSION,
        "version": __version__,
        **bridge.endpoint_info(),
    }
