"""Config flow: discovery, app configuration, sign-in, registration, pairing, push."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator
from tesla_fleet_api.exceptions import InvalidToken, PreconditionFailed

from custom_components.tesla_bridge.const import (
    CONF_BRIDGE,
    CONF_PRESET,
    CONF_PUBLIC_DOMAIN,
    DOMAIN,
    TOKEN_URL,
)
from homeassistant.components.application_credentials import (
    DOMAIN as APPLICATION_CREDENTIALS_DOMAIN,
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.config_entries import SOURCE_HASSIO, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from homeassistant.setup import async_setup_component

from .conftest import BRIDGE_STATUS, CA_PEM, VIN1, VIN2, FakeBridge

REDIRECT = "https://example.com/auth/external/callback"
FLOW = "custom_components.tesla_bridge.config_flow"


@pytest.fixture
async def credentials(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, APPLICATION_CREDENTIALS_DOMAIN, {})
    await async_import_client_credential(
        hass, DOMAIN, ClientCredential("client_id", "client_secret"), "cred"
    )


@pytest.fixture
def flow_bridge() -> FakeBridge:
    fake = FakeBridge()
    with patch(f"{FLOW}.BridgeClient", return_value=fake):
        yield fake


@pytest.fixture
def flow_tesla(mock_tesla: MagicMock) -> MagicMock:
    """The partner/user API objects the flow builds."""
    mock_tesla.partner_login = AsyncMock()
    mock_tesla.public_uncompressed_point = "04abcdef"
    mock_tesla.partner.register = AsyncMock(
        return_value={"response": {"public_key": "04ABCDEF"}}
    )
    fleet = MagicMock()
    fleet.fleet_status = AsyncMock(
        return_value={
            "response": {"key_paired_vins": [VIN1, VIN2], "unpaired_vins": []}
        }
    )
    vehicles = mock_tesla.vehicle_apis
    mock_tesla.vehicles.createFleet = lambda vin: (
        vehicles.get(vin, fleet) if vin != "status" else fleet
    )
    for api in vehicles.values():
        api.fleet_status = fleet.fleet_status
    with patch(f"{FLOW}.TeslaFleetApi", return_value=mock_tesla):
        yield mock_tesla


async def _oauth(
    hass: HomeAssistant,
    result: dict[str, Any],
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    access_token: str,
) -> dict[str, Any]:
    """Complete Tesla's external sign-in step."""
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    state = config_entry_oauth2_flow._encode_jwt(
        hass, {"flow_id": result["flow_id"], "redirect_uri": REDIRECT}
    )
    client = await hass_client_no_auth()
    await client.get(f"/auth/external/callback?code=abcd&state={state}")
    aioclient_mock.post(
        TOKEN_URL,
        json={
            "refresh_token": "refresh",
            "access_token": access_token,
            "type": "Bearer",
            "expires_in": 3600,
        },
    )
    return await hass.config_entries.flow.async_configure(result["flow_id"])


async def _through_progress(
    hass: HomeAssistant, result: dict[str, Any]
) -> dict[str, Any]:
    while result["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    if result["type"] is FlowResultType.SHOW_PROGRESS_DONE:
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    return result


@pytest.mark.usefixtures("current_request_with_host", "credentials")
async def test_full_flow_manual_bridge(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    access_token: str,
    flow_bridge: FakeBridge,
    flow_tesla: MagicMock,
) -> None:
    with patch(f"{FLOW}.is_hassio", return_value=False):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    assert result["step_id"] == "manual_bridge"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "10.0.0.5", "port": 8099, "token": "t"}
    )
    assert result["step_id"] == "network"
    assert result["description_placeholders"]["telemetry_port"] == "8443"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"upnp": False}
    )
    result = await _through_progress(hass, result)
    assert result["step_id"] == "origin"
    assert result["description_placeholders"]["origin"] == "https://myhome.duckdns.org"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await _oauth(
        hass, result, hass_client_no_auth, aioclient_mock, access_token
    )

    # Registration succeeded, both cars already trust the key -> streaming preset.
    assert result["step_id"] == "telemetry"
    flow_tesla.partner.register.assert_awaited_once_with("myhome.duckdns.org")
    assert flow_bridge.public_key == flow_tesla.public_pem

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PRESET: "essential"}
    )
    result = await _through_progress(hass, result)
    assert result["step_id"] == "push_result"
    assert "Model Y: configured" in result["description_placeholders"]["results"]
    pushed_vins, token = flow_tesla.vehicle_apis[
        VIN1
    ].fleet_telemetry_config_jws.await_args.args
    assert pushed_vins == [VIN1]
    assert token.count(".") == 2

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "powerwall"
    with patch("custom_components.tesla_bridge.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"skip": True}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_PUBLIC_DOMAIN] == "myhome.duckdns.org"
    assert data[CONF_BRIDGE] == {"host": "10.0.0.5", "port": 8099, "token": "t"}
    assert set(data["telemetry_pushed"]) == {VIN1, VIN2}
    assert result["options"][CONF_PRESET] == "essential"
    assert result["result"].unique_id == "uid"


@pytest.mark.usefixtures("current_request_with_host", "credentials")
async def test_origin_not_allowed_then_retry(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    access_token: str,
    flow_bridge: FakeBridge,
    flow_tesla: MagicMock,
) -> None:
    flow_tesla.partner.register.side_effect = [
        PreconditionFailed({"error": "origin"}),
        {"response": {"public_key": "04abcdef"}},
    ]
    with patch(f"{FLOW}.is_hassio", return_value=False):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "h", "port": 8099, "token": "t"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"upnp": False}
    )
    result = await _through_progress(hass, result)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await _oauth(
        hass, result, hass_client_no_auth, aioclient_mock, access_token
    )
    assert result["step_id"] == "register"
    assert result["errors"] == {"base": "origin_not_allowed"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "telemetry"


@pytest.mark.usefixtures("current_request_with_host", "credentials")
async def test_unpaired_car_shows_qr(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    access_token: str,
    flow_bridge: FakeBridge,
    flow_tesla: MagicMock,
) -> None:
    flow_tesla.vehicle_apis[VIN1].fleet_status.return_value = {
        "response": {"key_paired_vins": [VIN1], "unpaired_vins": [VIN2]}
    }
    with patch(f"{FLOW}.is_hassio", return_value=False):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "h", "port": 8099, "token": "t"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"upnp": False}
    )
    result = await _through_progress(hass, result)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await _oauth(
        hass, result, hass_client_no_auth, aioclient_mock, access_token
    )
    assert result["step_id"] == "vehicles"
    assert result["description_placeholders"]["cars"] == "Model 3"
    assert (
        result["description_placeholders"]["url"]
        == "https://www.tesla.com/_ak/myhome.duckdns.org"
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"skip": False}
    )
    assert result["errors"] == {"base": "still_unpaired"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"skip": True}
    )
    assert result["step_id"] == "telemetry"


def _discovery(slug: str) -> HassioServiceInfo:
    return HassioServiceInfo(
        config={
            "host": "new-host",
            "port": 8099,
            "token": "new",
            "domain": "myhome.duckdns.org",
        },
        name="Tesla Bridge",
        slug=slug,
        uuid="1234",
    )


async def test_hassio_discovery_with_entry_aborts(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=_discovery("abc_tesla_bridge")
    )
    # Single config entry: the entry follows the app by itself instead.
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"
    assert config_entry.data[CONF_BRIDGE]["token"] == "t"


async def test_hassio_discovery_from_another_app_is_ignored(
    hass: HomeAssistant,
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=_discovery("abc_other_app")
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_tesla_bridge"


async def test_hassio_discovery_names_the_app(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_HASSIO}, data=_discovery("abc_tesla_bridge")
    )
    assert result["step_id"] == "hassio_confirm"
    assert result["description_placeholders"] == {"slug": "abc_tesla_bridge"}


@pytest.mark.usefixtures("current_request_with_host", "credentials")
async def test_rejected_developer_credentials_abort(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    access_token: str,
    flow_bridge: FakeBridge,
    flow_tesla: MagicMock,
) -> None:
    flow_tesla.partner_login.side_effect = InvalidToken({"error": "invalid_client"})
    with patch(f"{FLOW}.is_hassio", return_value=False):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "h", "port": 8099, "token": "t"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"upnp": False}
    )
    result = await _through_progress(hass, result)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await _oauth(
        hass, result, hass_client_no_auth, aioclient_mock, access_token
    )
    assert result["type"] is FlowResultType.ABORT
    # Our own message, not core's generic "oauth_error" text.
    assert result["reason"] == "partner_auth_failed"
    assert result.get("translation_domain") in (None, DOMAIN)


async def test_configure_app_validates_input(
    hass: HomeAssistant, flow_bridge: FakeBridge
) -> None:
    from homeassistant.components.hassio import AddonInfo, AddonState

    manager = MagicMock()
    manager.async_get_addon_info = AsyncMock(
        return_value=AddonInfo(
            available=True,
            hostname="abc-tesla-bridge",
            options={"lets_encrypt_email": "old@example.com"},
            state=AddonState.NOT_RUNNING,
            update_available=False,
            version="0.1.0",
        )
    )
    manager.async_set_addon_options = AsyncMock()
    with (
        patch(f"{FLOW}.is_hassio", return_value=True),
        patch(
            f"{FLOW}.async_find_app_slug", AsyncMock(return_value="abc_tesla_bridge")
        ),
        patch(f"{FLOW}.get_app_manager", return_value=manager),
        patch(f"{FLOW}.async_app_host_ports", AsyncMock(return_value=(443, 8443))),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["step_id"] == "configure_app"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "duckdns_domain": "not.a.duckdns.org",
                "duckdns_token": "tok",
                "lets_encrypt_accept_terms": False,
            },
        )
        assert result["errors"] == {
            "duckdns_domain": "invalid_domain",
            "lets_encrypt_accept_terms": "terms_required",
        }
        with patch(
            f"{FLOW}.TeslaBridgeConfigFlow._async_start_app",
            AsyncMock(
                return_value={"host": "abc-tesla-bridge", "port": 8099, "token": "x"}
            ),
        ):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {
                    "duckdns_domain": "https://Myhome.duckdns.org",
                    "duckdns_token": " tok ",
                    "lets_encrypt_accept_terms": True,
                },
            )
            result = await _through_progress(hass, result)
    manager.async_set_addon_options.assert_awaited_once_with(
        {
            "duckdns_domain": "myhome.duckdns.org",
            "duckdns_token": "tok",
            "lets_encrypt_accept_terms": True,
        }
    )
    assert result["step_id"] == "network"


def test_status_fixture_matches_api() -> None:
    assert BRIDGE_STATUS["api"] == 1
    assert CA_PEM.startswith("-----BEGIN CERTIFICATE-----")
