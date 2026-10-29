"""HTTPS server on port 443 that publishes only the Tesla partner public key.

Tesla (and the Tesla app, when pairing a virtual key) fetches
``https://<domain>/.well-known/appspecific/com.tesla.3p.public-key.pem``.
Every other path returns 404, so nothing else about the home is exposed.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
import ssl

from aiohttp import web

from .acme import CertInfo
from .const import KEY_SERVER_PORT, PUBLIC_KEY_PATH

_LOGGER = logging.getLogger(__name__)


def create_key_app(get_public_key: Callable[[], str | None]) -> web.Application:
    async def public_key(request: web.Request) -> web.Response:
        pem = get_public_key()
        if not pem:
            raise web.HTTPNotFound
        return web.Response(
            text=pem,
            content_type="application/x-pem-file",
            headers={"Cache-Control": "no-cache"},
        )

    async def not_found(request: web.Request) -> web.Response:
        raise web.HTTPNotFound

    app = web.Application()
    app.router.add_get(PUBLIC_KEY_PATH, public_key)
    app.router.add_route("*", "/{tail:.*}", not_found)
    return app


class KeyServer:
    """Owns the listener; the TLS certificate can be swapped without a restart."""

    def __init__(
        self, get_public_key: Callable[[], str | None], port: int = KEY_SERVER_PORT
    ) -> None:
        self._get_public_key = get_public_key
        self._port = port
        self._runner: web.AppRunner | None = None
        self._ssl: ssl.SSLContext | None = None

    @property
    def listening(self) -> bool:
        return self._runner is not None

    async def apply_certificate(self, cert: CertInfo) -> None:
        """Start serving with ``cert``, or hot-swap it if already running."""
        if self._ssl is not None:
            # New handshakes pick up the reloaded chain; no listener restart.
            self._ssl.load_cert_chain(cert.cert_path, cert.key_path)
            _LOGGER.info("Key server certificate reloaded")
            return
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert.cert_path, cert.key_path)
        self._ssl = context
        self._runner = web.AppRunner(
            create_key_app(self._get_public_key), access_log=None
        )
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self._port, ssl_context=context)
        await site.start()
        _LOGGER.info("Serving the Tesla public key on port %s", self._port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._ssl = None
