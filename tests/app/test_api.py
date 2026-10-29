"""Internal API: auth, public key, VIN allowlist, stream, key server."""

from __future__ import annotations

from pathlib import Path

from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
import pytest

from tesla_bridge.api import create_api_app
from tesla_bridge.bridge import Bridge
from tesla_bridge.const import PUBLIC_KEY_PATH
from tesla_bridge.keyserver import create_key_app
from tesla_bridge.options import Options
from tesla_bridge.pki import ensure_pki
from tesla_bridge.state import StateStore


def _pem(key: ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey) -> str:
    return (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )


@pytest.fixture
def bridge(tmp_path: Path, state: StateStore) -> Bridge:
    options = Options.from_dict({"duckdns_domain": "myhome"})
    b = Bridge(options, state, pki_dir=tmp_path / "pki", acme_dir=tmp_path / "acme")
    b.pki = ensure_pki(tmp_path / "pki", "myhome.duckdns.org")
    return b


@pytest.fixture
async def client(bridge: Bridge):
    async with TestClient(TestServer(create_api_app(bridge))) as client:
        yield client


def _auth(bridge: Bridge) -> dict[str, str]:
    return {"Authorization": f"Bearer {bridge.state.data.api_token}"}


async def test_health_is_open(client: TestClient) -> None:
    resp = await client.get("/api/v1/health")
    assert resp.status == 200
    assert (await resp.json())["ok"] is True


@pytest.mark.parametrize("path", ["/api/v1/status", "/api/v1/ca", "/api/v1/stream"])
async def test_requires_token(client: TestClient, path: str) -> None:
    assert (await client.get(path)).status == 401
    resp = await client.get(path, headers={"Authorization": "Bearer nope"})
    assert resp.status == 401


async def test_status_and_ca(client: TestClient, bridge: Bridge) -> None:
    status = await (await client.get("/api/v1/status", headers=_auth(bridge))).json()
    assert status["domain"] == "myhome.duckdns.org"
    assert status["ca_sha256"] == bridge.pki.ca_sha256
    assert status["configured"]["duckdns_token"] is False
    ca = await (await client.get("/api/v1/ca", headers=_auth(bridge))).text()
    assert ca == bridge.pki.ca_pem


async def test_public_key_validation(client: TestClient, bridge: Bridge) -> None:
    rsa_pem = _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    resp = await client.put("/api/v1/public-key", data=rsa_pem, headers=_auth(bridge))
    assert resp.status == 400
    resp = await client.put("/api/v1/public-key", data="junk", headers=_auth(bridge))
    assert resp.status == 400

    pem = _pem(ec.generate_private_key(ec.SECP256R1()))
    resp = await client.put("/api/v1/public-key", data=pem, headers=_auth(bridge))
    assert resp.status == 200
    body = await resp.json()
    assert body["present"] is True
    assert body["served"] is False  # no certificate yet
    assert bridge.state.data.public_key_pem == pem.strip() + "\n"


async def test_vins(client: TestClient, bridge: Bridge) -> None:
    resp = await client.put(
        "/api/v1/vins", json=["5yj1", " 5YJ2 ", "5YJ1"], headers=_auth(bridge)
    )
    assert (await resp.json()) == {"vins": ["5YJ1", "5YJ2"]}
    resp = await client.put("/api/v1/vins", json={"a": 1}, headers=_auth(bridge))
    assert resp.status == 400


async def test_stream_hello_snapshot_and_events(
    client: TestClient, bridge: Bridge
) -> None:
    async with client.ws_connect("/api/v1/stream", headers=_auth(bridge)) as ws:
        hello = await ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["api"] == 1
        assert hello["ca_sha256"] == bridge.pki.ca_sha256
        snapshot = await ws.receive_json()
        assert snapshot == {"type": "snapshot", "vehicles": {}}

        bridge.relay.handle_data(
            {
                "vin": "VIN1",
                "createdAt": "2026-10-26T10:00:00Z",
                "data": [{"key": "BatteryLevel", "value": {"doubleValue": 55.5}}],
            }
        )
        event = await ws.receive_json()
        assert event["type"] == "data"
        assert event["fields"] == {"BatteryLevel": 55.5}

        bridge.relay._broadcast({"type": "overflow"})
        msg = await ws.receive()
        assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING)


async def test_key_server_only_serves_key() -> None:
    current: dict[str, str | None] = {"pem": None}
    app = create_key_app(lambda: current["pem"])
    async with TestClient(TestServer(app)) as client:
        assert (await client.get(PUBLIC_KEY_PATH)).status == 404
        current["pem"] = "-----BEGIN PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----\n"
        resp = await client.get(PUBLIC_KEY_PATH)
        assert resp.status == 200
        assert resp.content_type == "application/x-pem-file"
        assert await resp.text() == current["pem"]
        for path in ("/", "/api/v1/status", "/.well-known/other"):
            assert (await client.get(path)).status == 404
