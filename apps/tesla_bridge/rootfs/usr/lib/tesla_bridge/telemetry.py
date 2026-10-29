"""Render the fleet-telemetry config and supervise the server process."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any

import aiohttp

from .const import (
    FLEET_TELEMETRY_BIN,
    FLEET_TELEMETRY_CONFIG,
    TELEMETRY_PORT,
    TELEMETRY_STATUS_PORT,
    ZMQ_ADDR,
    ZMQ_NAMESPACE,
)
from .pki import TelemetryPki

_LOGGER = logging.getLogger(__name__)

RESTART_BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
# A run longer than this resets the restart backoff.
STABLE_RUNTIME = 120.0

_LEVELS = {
    "trace": logging.DEBUG,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "fatal": logging.CRITICAL,
    "panic": logging.CRITICAL,
}


def render_config(
    pki: TelemetryPki,
    *,
    extra_vehicle_ca: str | None = None,
    port: int = TELEMETRY_PORT,
    log_level: str = "info",
) -> dict[str, Any]:
    """Build the fleet-telemetry server configuration.

    Records go to a local ZMQ PUB socket with ``transmit_decoded_records`` so the
    relay receives JSON that still carries the vehicle's ``createdAt`` and the
    ``isResend`` flag (the MQTT dispatcher drops both).
    """
    tls: dict[str, str] = {
        "server_cert": str(pki.server_cert_path),
        "server_key": str(pki.server_key_path),
    }
    if extra_vehicle_ca:
        # Development only: lets a simulated vehicle (fake "Tesla Motors
        # Products CA") connect alongside the CAs built into the binary.
        tls["ca_file"] = extra_vehicle_ca
    return {
        "host": "0.0.0.0",
        "port": port,
        "status_port": TELEMETRY_STATUS_PORT,
        "log_level": "debug" if log_level == "debug" else "info",
        "json_log_enable": True,
        "namespace": ZMQ_NAMESPACE,
        "transmit_decoded_records": True,
        "reliable_ack_sources": {"V": "zmq"},
        # Loopback only: the relay is the sole consumer, and the stream has no
        # authentication of its own (the API on 8099 requires a token).
        "zmq": {"addr": ZMQ_ADDR},
        "records": {
            "V": ["zmq"],
            "connectivity": ["zmq"],
            "alerts": ["zmq"],
            "errors": ["zmq"],
        },
        "tls": tls,
    }


def write_config(config: dict[str, Any], path: Path = FLEET_TELEMETRY_CONFIG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2))
    os.replace(tmp, path)


def log_server_line(line: str) -> None:
    """Forward one fleet-telemetry log line (JSON) into our logger."""
    try:
        entry = json.loads(line)
    except ValueError:
        if line.strip():
            _LOGGER.info("fleet-telemetry: %s", line.strip())
        return
    level = _LEVELS.get(str(entry.pop("level", "info")).lower(), logging.INFO)
    msg = entry.pop("msg", "")
    for key in ("time", "context", "activity"):
        entry.pop(key, None)
    details = " ".join(f"{k}={v}" for k, v in entry.items())
    _LOGGER.log(level, "fleet-telemetry: %s %s", msg, details)


class TelemetryServer:
    """Keeps exactly one fleet-telemetry process running with the current config."""

    def __init__(
        self,
        on_restart: Callable[[], Awaitable[None]],
        binary: str = FLEET_TELEMETRY_BIN,
        config_path: Path = FLEET_TELEMETRY_CONFIG,
    ) -> None:
        self._on_restart = on_restart
        self._binary = binary
        self._config_path = config_path
        self._config: dict[str, Any] | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._config_changed = asyncio.Event()
        self.started_at: dt.datetime | None = None
        self.restarts = 0
        self.last_exit: int | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def status(self) -> dict[str, object]:
        return {
            "running": self.running,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "restarts": self.restarts,
            "last_exit": self.last_exit,
            "configured": self._config is not None,
        }

    def apply(self, config: dict[str, Any]) -> None:
        """Use ``config``; restarts the process if it changed."""
        if config == self._config:
            return
        self._config = config
        write_config(config, self._config_path)
        self._config_changed.set()
        if self._task is None:
            self._task = asyncio.create_task(self._supervise(), name="fleet-telemetry")

    async def healthy(self, session: aiohttp.ClientSession) -> bool:
        if not self.running:
            return False
        try:
            async with asyncio.timeout(3):
                async with session.get(
                    f"http://127.0.0.1:{TELEMETRY_STATUS_PORT}/status"
                ) as resp:
                    return resp.status == 200
        except (aiohttp.ClientError, TimeoutError):
            return False

    async def _supervise(self) -> None:
        attempt = 0
        while True:
            self._config_changed.clear()
            started = asyncio.get_running_loop().time()
            exit_code = await self._run_once()
            self.last_exit = exit_code
            # Any connection the server held is gone: tell the relay.
            await self._on_restart()
            if self._config_changed.is_set():
                attempt = 0
                continue
            runtime = asyncio.get_running_loop().time() - started
            attempt = 0 if runtime > STABLE_RUNTIME else attempt + 1
            delay = RESTART_BACKOFF[min(attempt, len(RESTART_BACKOFF) - 1)]
            _LOGGER.warning(
                "fleet-telemetry exited with %s; restarting in %.0fs", exit_code, delay
            )
            self.restarts += 1
            try:
                async with asyncio.timeout(delay):
                    await self._config_changed.wait()
            except TimeoutError:
                pass

    async def _run_once(self) -> int | None:
        env = {**os.environ, "SUPPRESS_TLS_HANDSHAKE_ERROR_LOGGING": "true"}
        self._proc = await asyncio.create_subprocess_exec(
            self._binary,
            "-config",
            str(self._config_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        self.started_at = dt.datetime.now(dt.UTC)
        _LOGGER.info("fleet-telemetry started (pid %s)", self._proc.pid)
        reader = asyncio.create_task(self._pump_logs(self._proc))
        waiter = asyncio.create_task(self._proc.wait())
        changed = asyncio.create_task(self._config_changed.wait())
        try:
            await asyncio.wait({waiter, changed}, return_when=asyncio.FIRST_COMPLETED)
            if not waiter.done():
                _LOGGER.info("Configuration changed; restarting fleet-telemetry")
                await self._terminate()
            return await waiter
        finally:
            changed.cancel()
            if self._proc.returncode is None:
                # Leaving by cancellation or error: stop the child first, or the
                # log reader would wait forever for its stdout to close.
                await asyncio.shield(self._terminate())
            waiter.cancel()
            await asyncio.gather(reader, waiter, return_exceptions=True)
            self._proc = None

    async def _pump_logs(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        while line := await proc.stdout.readline():
            log_server_line(line.decode(errors="replace"))

    async def _terminate(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            async with asyncio.timeout(10):
                await proc.wait()
        except TimeoutError:
            proc.kill()
            await proc.wait()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._terminate()
