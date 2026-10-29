"""Regression tests for issues found in the pre-publish review."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from pathlib import Path
import stat
import sys

from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from tesla_bridge import acme, duckdns, telemetry
from tesla_bridge.acme import CertificateManager
from tesla_bridge.duckdns import DuckDNSUpdater
from tesla_bridge.options import Options
from tesla_bridge.relay import Relay
from tesla_bridge.state import StateStore
from tesla_bridge.telemetry import TelemetryServer

TOKEN = "f8a2c0de-1234-4abc-9def-0123456789ab"


async def test_duckdns_http_error_does_not_leak_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def unavailable(request: web.Request) -> web.Response:
        return web.Response(status=503, text="down")

    app = web.Application()
    app.router.add_get("/update", unavailable)
    async with TestServer(app) as server:
        monkeypatch.setattr(
            duckdns, "DUCKDNS_UPDATE_URL", str(server.make_url("/update"))
        )
        import aiohttp

        async with aiohttp.ClientSession() as session:
            updater = DuckDNSUpdater(session, "myhome", TOKEN)
            with caplog.at_level(logging.DEBUG):
                status = await updater.update()
    assert status.last_error == "DuckDNS returned HTTP 503"
    assert TOKEN not in str(status.as_dict())
    # Only the app's own logs matter (the fake server logs its request URL).
    app_logs = [
        r.getMessage() for r in caplog.records if r.name.startswith("tesla_bridge")
    ]
    assert app_logs
    assert not any(TOKEN in line for line in app_logs)


async def test_lego_output_is_scrubbed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake_lego = tmp_path / "lego"
    fake_lego.write_text(
        "#!/bin/sh\n"
        'echo "[myhome.duckdns.org] error: does not match expectation (OK) used url '
        '[https://www.duckdns.org/update?domains=myhome&token=$DUCKDNS_TOKEN&txt=x]"\n'
        "exit 1\n"
    )
    fake_lego.chmod(fake_lego.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(acme, "LEGO_BIN", str(fake_lego))
    options = Options.from_dict(
        {
            "duckdns_domain": "myhome",
            "duckdns_token": TOKEN,
            "lets_encrypt_accept_terms": True,
        }
    )

    async def on_change(_cert: object) -> None:
        return None

    manager = CertificateManager(
        options, on_change, tmp_path / "acme", tmp_path / "ssl"
    )
    with caplog.at_level(logging.DEBUG):
        await manager.ensure()
    assert manager.last_error is not None
    assert "**REDACTED**" in manager.last_error
    assert TOKEN not in manager.last_error
    assert TOKEN not in caplog.text


async def test_stop_does_not_hang_while_running(tmp_path: Path) -> None:
    fake = tmp_path / "fleet-telemetry"
    fake.write_text(
        f"#!{sys.executable}\nimport time\nprint('started', flush=True)\ntime.sleep(600)\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    restarted: list[bool] = []

    async def on_restart() -> None:
        restarted.append(True)

    server = TelemetryServer(on_restart, str(fake), tmp_path / "ft.json")
    server.apply({"port": 1})
    for _ in range(50):
        if server.running:
            break
        await asyncio.sleep(0.05)
    assert server.running
    async with asyncio.timeout(15):
        await server.stop()
    assert not server.running


async def test_signal_counters_roll_over_without_new_data(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.json")
    state.load()
    relay = Relay(state)
    relay._now = lambda: dt.datetime(2026, 10, 31, 23, 0, tzinfo=dt.UTC)
    relay.handle_data(
        {
            "vin": "VIN1",
            "createdAt": "2026-10-31T23:00:00Z",
            "data": [{"key": "BatteryLevel", "value": {"doubleValue": 50.0}}],
        }
    )
    assert relay.snapshot()["vehicles"]["VIN1"]["signals"]["count"] == 1
    relay._now = lambda: dt.datetime(2026, 11, 1, 1, 0, tzinfo=dt.UTC)
    signals = relay.snapshot()["vehicles"]["VIN1"]["signals"]
    assert signals == {
        "day": "2026-11-01",
        "count": 0,
        "month": "2026-11",
        "month_count": 0,
    }
    assert state.data.signals["VIN1"]["count"] == 1  # stored history untouched


def test_render_binds_zmq_to_loopback(tmp_path: Path) -> None:
    from tesla_bridge.pki import ensure_pki

    config = telemetry.render_config(ensure_pki(tmp_path, "myhome.duckdns.org"))
    assert config["zmq"]["addr"].startswith("tcp://127.0.0.1:")


async def test_selftest_messages_contain_no_ip(tmp_path: Path) -> None:
    from tesla_bridge.pki import ensure_pki
    from tesla_bridge.selftest import check_telemetry_port

    ca = ensure_pki(tmp_path, "myhome.duckdns.org").ca_pem
    ok, detail = await check_telemetry_port(
        "127.0.0.1", 1, "myhome.duckdns.org", ca, timeout=2
    )
    assert not ok
    assert "127.0.0.1" not in detail
    assert "public address" in detail
