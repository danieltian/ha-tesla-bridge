"""Fixtures for the Tesla Bridge integration tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import copy
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
import jwt
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_bridge.const import (
    CONF_BRIDGE,
    CONF_POWERWALL,
    CONF_POWERWALL_INTERVAL,
    CONF_PRESET,
    CONF_PUBLIC_DOMAIN,
    CONF_REGION,
    CONF_TELEMETRY_PORT,
    CONF_UPNP,
    DOMAIN,
)
from homeassistant.core import HomeAssistant

VIN1 = "5YJ3E1EA0KF000001"
VIN2 = "7SAYGDEE0PF000002"
SITE_ID = 1234567

PRODUCTS: list[dict[str, Any]] = [
    {"vin": VIN1, "display_name": "Model Y", "command_signing": "required"},
    {"vin": VIN2, "display_name": "Model 3", "command_signing": "required"},
    {
        "energy_site_id": SITE_ID,
        "site_name": "Home",
        "components": {"battery": True, "solar": True},
    },
]

SITE_INFO = {
    "backup_reserve_percent": 20,
    "default_real_mode": "self_consumption",
    "user_settings": {"storm_mode_enabled": True},
    "components": {
        "disallow_charge_from_grid_with_solar_installed": True,
        "customer_preferred_export_rule": "pv_only",
    },
    "off_grid_vehicle_charging_reserve_percent": 50,
}
LIVE_STATUS = {
    "percentage_charged": 33.6,
    "battery_power": -1200,
    "solar_power": 4800,
    "load_power": 900,
    "grid_power": -2700,
    "energy_left": 4200,
    "island_status": "on_grid",
}

BRIDGE_STATUS: dict[str, Any] = {
    "api": 1,
    "version": "0.1.0",
    "domain": "myhome.duckdns.org",
    "telemetry_port": 8443,
    "ca_sha256": "abc",
    "duckdns": {"ip": "203.0.113.7", "ok": True},
    "key_server": {
        "listening": True,
        "certificate": {"not_after": "2027-01-01T00:00:00+00:00"},
        "public_key": {"served": True},
    },
    "telemetry": {"running": True},
    "relay": {"vehicles": {VIN1: {"connected": True}}},
    "selftest": {"telemetry_loopback": {"ok": True}},
}

CA_PEM = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


class FakeBridge:
    """Stands in for the app's REST + WebSocket API."""

    def __init__(self) -> None:
        self.status_data = copy.deepcopy(BRIDGE_STATUS)
        self.vins: list[str] | None = None
        self.public_key: str | None = None
        self.on_event: Callable[[dict[str, Any]], None] | None = None
        self.on_connection: Callable[[bool], None] | None = None
        self._stop = asyncio.Event()

    async def status(self) -> dict[str, Any]:
        return self.status_data

    async def ca_pem(self) -> str:
        return CA_PEM

    async def put_vins(self, vins: list[str]) -> None:
        self.vins = vins

    async def put_public_key(self, pem: str) -> dict[str, Any]:
        self.public_key = pem
        return {"present": True}

    async def stream_forever(
        self,
        on_event: Callable[[dict[str, Any]], None],
        on_connection: Callable[[bool], None],
    ) -> None:
        self.on_event = on_event
        self.on_connection = on_connection
        await self._stop.wait()

    def send(self, event: dict[str, Any]) -> None:
        assert self.on_event is not None
        self.on_event(event)


def _vehicle_api(vin: str) -> MagicMock:
    ok = {"response": {"result": True, "reason": ""}}
    api = MagicMock(name=f"vehicle {vin}")
    api.vin = vin
    api.model = "Model Y" if vin == VIN1 else "Model 3"
    for name in (
        "door_lock",
        "door_unlock",
        "charge_start",
        "charge_stop",
        "set_charge_limit",
        "set_charging_amps",
        "auto_conditioning_start",
        "auto_conditioning_stop",
        "set_temps",
        "set_sentry_mode",
        "flash_lights",
        "honk_horn",
        "actuate_trunk",
        "window_control",
        "remote_seat_heater_request",
        "remote_steering_wheel_heat_level_request",
        "charge_port_door_open",
        "charge_port_door_close",
    ):
        setattr(api, name, AsyncMock(return_value=ok))
    api.vehicle = AsyncMock(return_value={"response": {"state": "online"}})
    api.wake_up = AsyncMock(return_value={"response": {"state": "online"}})
    api.fleet_telemetry_config_jws = AsyncMock(
        return_value={"response": {"updated_vehicles": 1, "skipped_vehicles": {}}}
    )
    api.fleet_telemetry_config_get = AsyncMock(
        return_value={"response": {"synced": True}}
    )
    api.fleet_status = AsyncMock(
        return_value={
            "response": {"key_paired_vins": [VIN1, VIN2], "unpaired_vins": []}
        }
    )
    return api


@pytest.fixture
def private_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def mock_tesla(private_key: ec.EllipticCurvePrivateKey):
    """A fake TeslaFleetApi with two cars and one Powerwall site."""
    vehicles = {VIN1: _vehicle_api(VIN1), VIN2: _vehicle_api(VIN2)}
    site = MagicMock(name="energy site")
    site.site_info = AsyncMock(return_value={"response": copy.deepcopy(SITE_INFO)})
    site.live_status = AsyncMock(return_value={"response": copy.deepcopy(LIVE_STATUS)})
    for name in ("backup", "operation", "storm_mode", "grid_import_export"):
        setattr(site, name, AsyncMock(return_value={"response": {"code": 201}}))

    tesla = MagicMock(name="TeslaFleetApi")
    tesla.products = AsyncMock(return_value={"response": copy.deepcopy(PRODUCTS)})
    tesla.get_private_key = AsyncMock()
    tesla.private_key = private_key
    tesla.public_pem = (
        private_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )
    tesla.vehicles.createSigned = lambda vin: vehicles[vin]
    tesla.vehicles.createFleet = lambda vin: vehicles[vin]
    tesla.energySites.create = lambda site_id: site
    tesla._request = AsyncMock(
        return_value={"response": {"fleet_telemetry_errors": []}}
    )
    tesla.vehicle_apis = vehicles
    tesla.site_api = site
    with patch("custom_components.tesla_bridge.TeslaFleetApi", return_value=tesla):
        yield tesla


@pytest.fixture
def access_token() -> str:
    return jwt.encode(
        {
            "sub": "uid",
            "ou_code": "NA",
            "scp": [
                "openid",
                "offline_access",
                "vehicle_device_data",
                "vehicle_location",
                "vehicle_cmds",
                "vehicle_charging_cmds",
                "energy_device_data",
                "energy_cmds",
            ],
        },
        "a-test-signing-secret-that-is-32-bytes!",
        algorithm="HS256",
    )


@pytest.fixture
def mock_oauth(access_token: str):
    session = MagicMock()
    session.token = {"access_token": access_token}
    session.async_ensure_token_valid = AsyncMock()
    with (
        patch(
            "custom_components.tesla_bridge.async_get_config_entry_implementation",
            AsyncMock(),
        ),
        patch("custom_components.tesla_bridge.OAuth2Session", return_value=session),
    ):
        yield session


@pytest.fixture
def bridge():
    fake = FakeBridge()
    with patch("custom_components.tesla_bridge.BridgeClient", return_value=fake):
        yield fake


@pytest.fixture
def config_entry(access_token: str) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="uid",
        title="myhome.duckdns.org",
        data={
            "auth_implementation": "tesla_bridge",
            "token": {"access_token": access_token, "expires_at": 9e12},
            CONF_REGION: "na",
            CONF_PUBLIC_DOMAIN: "myhome.duckdns.org",
            CONF_TELEMETRY_PORT: 8443,
            CONF_BRIDGE: {"host": "local-tesla-bridge", "port": 8099, "token": "t"},
            CONF_POWERWALL: None,
            "telemetry_pushed": {},
        },
        options={CONF_PRESET: "standard", CONF_UPNP: False, CONF_POWERWALL_INTERVAL: 5},
    )


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_tesla: MagicMock,
    mock_oauth: MagicMock,
    bridge: FakeBridge,
) -> FakeBridge:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return bridge
