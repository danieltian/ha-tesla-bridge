"""The app's ingress page: status, and the way back into the setup."""

from __future__ import annotations

from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
import pytest

from tesla_bridge.bridge import Bridge
from tesla_bridge.options import Options
from tesla_bridge.pki import ensure_pki
from tesla_bridge.state import StateStore
from tesla_bridge.webui import (
    INTEGRATION_URL,
    PROBLEM,
    SETUP_URL,
    create_webui_app,
    render_page,
    status_rows,
)


@pytest.fixture
def bridge(tmp_path: Path, state: StateStore) -> Bridge:
    options = Options.from_dict({"duckdns_domain": "myhome"})
    b = Bridge(options, state, pki_dir=tmp_path / "pki", acme_dir=tmp_path / "acme")
    b.pki = ensure_pki(tmp_path / "pki", "myhome.duckdns.org")
    return b


async def test_only_the_ingress_proxy_gets_the_page(bridge: Bridge) -> None:
    async with TestClient(TestServer(create_webui_app(bridge.status))) as client:
        assert (await client.get("/")).status == 403


async def test_not_connected_offers_to_continue_setup(bridge: Bridge) -> None:
    app = create_webui_app(bridge.status, frozenset({"127.0.0.1"}))
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/")
        page = await resp.text()
    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "no-store"
    # target=_top: the page runs in an iframe inside Home Assistant.
    assert f'href="{SETUP_URL}" target="_top">Continue setup' in page
    assert "myhome.duckdns.org" in page


async def test_connected_links_to_the_integration(bridge: Bridge) -> None:
    bridge.relay.subscribe()  # the integration's stream
    app = create_webui_app(bridge.status, frozenset({"127.0.0.1"}))
    async with TestClient(TestServer(app)) as client:
        page = await (await client.get("/")).text()
    assert "Continue setup" not in page
    assert f'href="{INTEGRATION_URL}" target="_top">Open Tesla Bridge' in page


def test_problems_are_reported_and_escaped(bridge: Bridge) -> None:
    status = bridge.status()
    status["key_server"]["last_error"] = "<script>alert(1)</script>"
    status["selftest"] = {
        "ran_at": "2026-10-29T10:00:00+00:00",
        "key_loopback": {"ok": True, "detail": "ok"},
        "telemetry_loopback": {"ok": False, "detail": "no connection on port 8443"},
    }
    rows = {title: (state, detail) for state, title, detail in status_rows(status)}
    assert rows["Certificate"][0] == PROBLEM
    assert rows["Router ports"][0] == PROBLEM
    assert "no connection on port 8443" in rows["Router ports"][1]

    page = render_page(status)
    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page
