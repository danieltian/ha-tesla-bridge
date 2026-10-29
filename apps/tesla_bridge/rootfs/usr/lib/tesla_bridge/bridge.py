"""Wires the app's subsystems together."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import logging
from pathlib import Path
import signal
import socket
from typing import Any

import aiohttp
from aiohttp import web

from . import API_VERSION, __version__
from .acme import CertificateManager, CertInfo
from .api import create_api_app, hello_message
from .const import (
    ACME_DIR,
    API_PORT,
    FLEET_TELEMETRY_BIN,
    FLEET_TELEMETRY_CONFIG,
    KEY_SERVER_PORT,
    PKI_DIR,
    SSL_DIR,
    WEBUI_PORT,
    ZMQ_ADDR,
)
from .duckdns import DuckDNSUpdater, ipv4_session
from .keyserver import KeyServer
from .options import Options
from .pki import TelemetryPki, ensure_pki
from .relay import Relay
from .selftest import SelfTest
from .state import StateStore
from .supervisor import send_discovery
from .telemetry import TelemetryServer, render_config
from .webui import create_webui_app

_LOGGER = logging.getLogger(__name__)

SELFTEST_INITIAL_DELAY = 60.0
SELFTEST_INTERVAL = 1800.0


class Bridge:
    """The running app: DuckDNS, certificates, key server, telemetry, relay, API."""

    def __init__(
        self,
        options: Options,
        state: StateStore,
        *,
        pki_dir: Path = PKI_DIR,
        acme_dir: Path = ACME_DIR,
        ssl_dir: Path = SSL_DIR,
        telemetry_binary: str = FLEET_TELEMETRY_BIN,
        telemetry_config: Path = FLEET_TELEMETRY_CONFIG,
        key_port: int = KEY_SERVER_PORT,
        api_port: int = API_PORT,
        webui_port: int = WEBUI_PORT,
        zmq_addr: str = ZMQ_ADDR,
    ) -> None:
        self.options = options
        self.state = state
        self._pki_dir = pki_dir
        self._api_port = api_port
        self._webui_port = webui_port
        self.pki: TelemetryPki | None = None
        self.relay = Relay(state, zmq_addr)
        self.telemetry = TelemetryServer(
            self._telemetry_restarted, telemetry_binary, telemetry_config
        )
        self.key_server = KeyServer(lambda: self.state.data.public_key_pem, key_port)
        self.certs = CertificateManager(
            options, self._certificate_changed, acme_dir, ssl_dir
        )
        self.selftest = SelfTest()
        self.duckdns: DuckDNSUpdater | None = None
        self._session: aiohttp.ClientSession | None = None
        self._ipv4_session: aiohttp.ClientSession | None = None
        self._api_runner: web.AppRunner | None = None
        self._webui_runner: web.AppRunner | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self.started_at = dt.datetime.now(dt.UTC)

    # ---- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()
        self._ipv4_session = ipv4_session()
        self._spawn(self.relay.run(), "relay")

        self._api_runner = web.AppRunner(create_api_app(self), access_log=None)
        await self._api_runner.setup()
        await web.TCPSite(self._api_runner, "0.0.0.0", self._api_port).start()
        _LOGGER.info("Integration API listening on port %s", self._api_port)

        self._webui_runner = web.AppRunner(
            create_webui_app(self.status), access_log=None
        )
        await self._webui_runner.setup()
        await web.TCPSite(self._webui_runner, "0.0.0.0", self._webui_port).start()

        domain = self.options.domain
        if domain:
            self.pki = await asyncio.to_thread(ensure_pki, self._pki_dir, domain)
            self.telemetry.apply(
                render_config(
                    self.pki,
                    extra_vehicle_ca=self.options.dev_extra_vehicle_ca,
                    log_level=self.options.log_level,
                )
            )
            if self.options.can_update_dns:
                assert self.options.subdomain and self.options.duckdns_token
                self.duckdns = DuckDNSUpdater(
                    self._ipv4_session,
                    self.options.subdomain,
                    self.options.duckdns_token,
                )
                self._spawn(self.duckdns.run(), "duckdns")
            self._spawn(self.certs.run(), "certificates")
            self._spawn(self._selftest_loop(), "selftest")
        else:
            _LOGGER.warning(
                "No DuckDNS domain configured yet; waiting for the Tesla Bridge "
                "integration to finish setup"
            )
        self._spawn(self._announce(), "discovery")

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.telemetry.stop()
        await self.key_server.stop()
        for runner in (self._api_runner, self._webui_runner):
            if runner is not None:
                await runner.cleanup()
        for session in (self._session, self._ipv4_session):
            if session is not None:
                await session.close()
        self.state.save_now()

    def _spawn(self, coro: Any, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(self._task_done)
        self._tasks.append(task)

    @staticmethod
    def _task_done(task: asyncio.Task[Any]) -> None:
        if not task.cancelled() and (exc := task.exception()) is not None:
            _LOGGER.error("Background task %s crashed", task.get_name(), exc_info=exc)

    # ---- callbacks -------------------------------------------------------------

    async def _telemetry_restarted(self) -> None:
        self.relay.reset_connections()

    async def _certificate_changed(self, cert: CertInfo) -> None:
        await self.key_server.apply_certificate(cert)

    async def _announce(self) -> None:
        assert self._session is not None
        for delay in (0, 5, 15, 60, 300):
            await asyncio.sleep(delay)
            if await send_discovery(self._session, self.discovery_config()):
                return

    async def _selftest_loop(self) -> None:
        await asyncio.sleep(SELFTEST_INITIAL_DELAY)
        while True:
            await self.run_selftest()
            await asyncio.sleep(SELFTEST_INTERVAL)

    async def run_selftest(self) -> dict[str, Any]:
        assert self._session is not None
        if not (self.options.domain and self.pki):
            return self.selftest.result
        return await self.selftest.run_once(
            self._session,
            self.options.domain,
            self.duckdns.status.ip if self.duckdns else None,
            self.options.external_telemetry_port,
            self.pki.ca_pem,
            self.state.data.public_key_pem,
            self.key_server.listening,
        )

    # ---- integration-facing state ---------------------------------------------

    async def set_public_key(self, pem: str) -> None:
        if pem != self.state.data.public_key_pem:
            self.state.data.public_key_pem = pem
            self.state.save_now()
            _LOGGER.info("Stored the Tesla partner public key")

    def public_key_status(self) -> dict[str, Any]:
        pem = self.state.data.public_key_pem
        return {
            "present": pem is not None,
            "sha256": hashlib.sha256(pem.encode()).hexdigest() if pem else None,
            "served": self.key_server.listening and pem is not None,
        }

    def set_vins(self, vins: list[str]) -> None:
        if vins != self.state.data.vins:
            self.state.data.vins = vins
            self.state.save_now()

    def endpoint_info(self) -> dict[str, Any]:
        return {
            "domain": self.options.domain,
            "telemetry_port": self.options.external_telemetry_port,
            "ca_sha256": self.pki.ca_sha256 if self.pki else None,
        }

    def discovery_config(self) -> dict[str, Any]:
        return {
            "host": socket.gethostname(),
            "port": self._api_port,
            "token": self.state.data.api_token,
            "api": API_VERSION,
            **self.endpoint_info(),
        }

    def hello(self) -> dict[str, Any]:
        return hello_message(self)

    def status(self) -> dict[str, Any]:
        return {
            "api": API_VERSION,
            "version": __version__,
            "started_at": self.started_at.isoformat(),
            **self.endpoint_info(),
            "configured": {
                "domain": self.options.domain is not None,
                "duckdns_token": self.options.duckdns_token is not None,
                "lets_encrypt_terms": self.options.accept_terms,
            },
            "duckdns": self.duckdns.status.as_dict() if self.duckdns else None,
            "key_server": {
                **self.certs.status(),
                "listening": self.key_server.listening,
                "public_key": self.public_key_status(),
            },
            "telemetry": {
                **self.telemetry.status(),
                "server_cert_not_after": (
                    self.pki.server_not_after.isoformat() if self.pki else None
                ),
            },
            "relay": self.relay.stats(),
            "selftest": self.selftest.result,
            "vins": self.state.data.vins,
        }


async def run_forever(bridge: Bridge) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await bridge.start()
    try:
        await stop.wait()
    finally:
        _LOGGER.info("Shutting down")
        await bridge.stop()
