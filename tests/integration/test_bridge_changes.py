"""The entry follows changes to the app: new token, ports, slug or DuckDNS name."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import contextmanager
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_bridge import (
    _async_refresh_bridge,
    async_retry_unpaired,
    port_mappings,
)
from custom_components.tesla_bridge.bridge_client import BridgeAuthError
from custom_components.tesla_bridge.const import (
    CONF_APP_SLUG,
    CONF_BRIDGE,
    CONF_KEY_HOST_PORT,
    CONF_PUBLIC_DOMAIN,
    CONF_TELEMETRY_HOST_PORT,
    CONF_TELEMETRY_PORT,
    DOMAIN,
)
from custom_components.tesla_bridge.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.tesla_bridge.upnp import PortMapping
from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_entry_oauth2_flow, issue_registry as ir

from .conftest import VIN2, FakeBridge

INIT = "custom_components.tesla_bridge"
APP = "custom_components.tesla_bridge.app_manager"
FLOW = "custom_components.tesla_bridge.config_flow"
SLUG = "abc_tesla_bridge"
DISCOVERY: dict[str, Any] = {
    "host": "local-tesla-bridge",
    "port": 8099,
    "token": "t",
    "telemetry_port": 8443,
}


def _app_entry(config_entry: MockConfigEntry, **data: Any) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="uid",
        title=config_entry.title,
        data={**config_entry.data, CONF_APP_SLUG: SLUG, **data},
        options=dict(config_entry.options),
    )


@contextmanager
def _supervisor(
    discovery: AsyncMock,
    host_ports: tuple[int | None, int | None] | None = (443, 8443),
    slug: str = SLUG,
):
    with (
        patch("homeassistant.helpers.hassio.is_hassio", return_value=True),
        patch(f"{APP}.async_app_discovery", discovery),
        patch(f"{APP}.async_find_app_slug", AsyncMock(return_value=slug)),
        patch(f"{APP}.async_app_host_ports", AsyncMock(return_value=host_ports)),
    ):
        yield discovery


async def _settle(hass: HomeAssistant) -> None:
    """Let background tasks (the stream loop) run a few rounds."""
    for _ in range(20):
        await asyncio.sleep(0)
    await hass.async_block_till_done()


async def test_rejected_token_is_retried_until_the_app_announces_a_new_one(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_tesla: MagicMock,
    mock_oauth: MagicMock,
    bridge: FakeBridge,
) -> None:
    entry = _app_entry(config_entry)
    entry.add_to_hass(hass)
    attempts = 0

    async def rejected(*_args: Any) -> None:
        nonlocal attempts
        attempts += 1
        raise BridgeAuthError("rejected")

    bridge.stream_forever = rejected
    # Setup still sees the old token; the app announces its new one only after
    # its API is already up (and rejecting the old token).
    announced = AsyncMock(side_effect=[DISCOVERY, None, {**DISCOVERY, "token": "new"}])
    with (
        _supervisor(announced),
        patch(f"{INIT}.BRIDGE_RETRY", (0,)),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await _settle(hass)
    assert attempts == 2
    assert entry.data[CONF_BRIDGE]["token"] == "new"
    reload.assert_called_once_with(entry.entry_id)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "bridge_auth")
    assert issue is not None
    assert issue.translation_key == "bridge_auth"


async def test_standalone_bridge_auth_issue_points_to_reconfigure(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_tesla: MagicMock,
    mock_oauth: MagicMock,
    bridge: FakeBridge,
) -> None:
    attempts = 0
    seen: list[ir.IssueEntry | None] = []
    stop = asyncio.Event()

    async def rejected(_on_event: Any, on_connection: Callable[[bool], None]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 3:
            seen.append(ir.async_get(hass).async_get_issue(DOMAIN, "bridge_auth"))
            on_connection(True)  # the user fixed the token: back online
            await stop.wait()
        raise BridgeAuthError("rejected")

    bridge.stream_forever = rejected
    config_entry.add_to_hass(hass)
    with patch(f"{INIT}.BRIDGE_RETRY", (0,)):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await _settle(hass)
    assert attempts == 3
    assert seen[0] is not None
    assert seen[0].translation_key == "bridge_auth_manual"
    assert ir.async_get(hass).async_get_issue(DOMAIN, "bridge_auth") is None


async def test_new_duckdns_name_is_flagged_then_registered_in_reconfigure(
    hass: HomeAssistant, config_entry: MockConfigEntry, setup_integration: FakeBridge
) -> None:
    bridge = setup_integration
    status = config_entry.runtime_data.bridge.status
    bridge.status_data["domain"] = "newhome.duckdns.org"
    await status.async_refresh()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "domain_changed")
    assert issue is not None
    assert issue.translation_placeholders == {
        "old": "myhome.duckdns.org",
        "new": "newhome.duckdns.org",
    }
    # Not switched silently: Tesla does not know the new name yet.
    assert config_entry.data[CONF_PUBLIC_DOMAIN] == "myhome.duckdns.org"

    partner = MagicMock()
    partner.get_private_key = AsyncMock()
    partner.public_pem = "PEM"
    partner.public_uncompressed_point = "04ab"
    partner.partner_login = AsyncMock()
    partner.partner.register = AsyncMock(
        return_value={"response": {"public_key": "04AB"}}
    )
    implementation = MagicMock(client_id="id", client_secret="secret")
    with (
        patch(f"{FLOW}.TeslaFleetApi", return_value=partner),
        patch(f"{FLOW}.BridgeClient", return_value=bridge),
        patch.object(
            config_entry_oauth2_flow,
            "async_get_config_entry_implementation",
            AsyncMock(return_value=implementation),
        ),
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_RECONFIGURE, "entry_id": config_entry.entry_id},
        )
        assert result["type"] is FlowResultType.MENU
        assert result["menu_options"][0] == "reconfigure_domain"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "reconfigure_domain"}
        )
        assert result["step_id"] == "reconfigure_domain"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    partner.partner_login.assert_awaited_once()
    partner.partner.register.assert_awaited_once_with("newhome.duckdns.org")
    assert bridge.public_key == "PEM"
    assert config_entry.data[CONF_PUBLIC_DOMAIN] == "newhome.duckdns.org"
    assert config_entry.title == "newhome.duckdns.org"
    await status.async_refresh()
    assert ir.async_get(hass).async_get_issue(DOMAIN, "domain_changed") is None


async def test_public_telemetry_port_follows_the_app(
    hass: HomeAssistant, config_entry: MockConfigEntry, setup_integration: FakeBridge
) -> None:
    setup_integration.status_data["telemetry_port"] = 9443
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await config_entry.runtime_data.bridge.status.async_refresh()
    assert config_entry.data[CONF_TELEMETRY_PORT] == 9443
    reload.assert_called_once_with(config_entry.entry_id)


async def test_supervisor_error_keeps_stored_host_ports(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    entry = _app_entry(
        config_entry, **{CONF_KEY_HOST_PORT: 10443, CONF_TELEMETRY_HOST_PORT: 18443}
    )
    entry.add_to_hass(hass)
    with _supervisor(AsyncMock(return_value=DISCOVERY), host_ports=None):
        assert not await _async_refresh_bridge(hass, entry)
    assert entry.data[CONF_KEY_HOST_PORT] == 10443
    assert entry.data[CONF_TELEMETRY_HOST_PORT] == 18443


async def test_disabled_host_port_raises_issue_and_gets_no_mapping(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    entry = _app_entry(config_entry)
    entry.add_to_hass(hass)
    with _supervisor(AsyncMock(return_value=DISCOVERY), host_ports=(None, 18443)):
        assert await _async_refresh_bridge(hass, entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "app_ports_disabled")
    # Never a guessed 443: that could expose whatever else listens there.
    assert port_mappings(entry.data) == [PortMapping(8443, 18443)]
    with _supervisor(AsyncMock(return_value=DISCOVERY), host_ports=(443, 18443)):
        assert await _async_refresh_bridge(hass, entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "app_ports_disabled") is None
    assert port_mappings(entry.data) == [
        PortMapping(443, 443),
        PortMapping(8443, 18443),
    ]


async def test_app_reinstalled_under_a_new_slug_is_followed(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    entry = _app_entry(config_entry)
    entry.add_to_hass(hass)
    by_slug = {"local_tesla_bridge": {**DISCOVERY, "token": "new"}}
    discovery = AsyncMock(side_effect=lambda _hass, slug: by_slug.get(slug))
    with _supervisor(discovery, slug="local_tesla_bridge"):
        assert await _async_refresh_bridge(hass, entry)
    assert entry.data[CONF_APP_SLUG] == "local_tesla_bridge"
    assert entry.data[CONF_BRIDGE]["token"] == "new"


async def test_reconfigure_standalone_bridge(
    hass: HomeAssistant, config_entry: MockConfigEntry, setup_integration: FakeBridge
) -> None:
    with (
        patch(f"{FLOW}.BridgeClient", return_value=setup_integration),
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_RECONFIGURE, "entry_id": config_entry.entry_id},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "reconfigure_bridge"}
        )
        assert result["step_id"] == "reconfigure_bridge"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "10.0.0.7", "port": 8099, "token": "fresh"}
        )
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_BRIDGE] == {
        "host": "10.0.0.7",
        "port": 8099,
        "token": "fresh",
    }


async def test_powerwall_issue_cleared_when_local_updates_are_off(
    hass: HomeAssistant, config_entry: MockConfigEntry, setup_integration: FakeBridge
) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        "powerwall_auth",
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="powerwall_auth",
    )
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, "powerwall_auth") is None


async def test_retry_unpaired_survives_network_errors(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    setup_integration: FakeBridge,
    mock_tesla: MagicMock,
) -> None:
    api = mock_tesla.vehicle_apis[VIN2]
    config_entry.runtime_data.telemetry.vehicle_state(VIN2).update(
        ok=False, hash=None, skipped_reason="missing_key"
    )
    for error in (ClientError("down"), TimeoutError()):
        api.fleet_status.side_effect = error
        await async_retry_unpaired(hass, config_entry)  # must not raise


async def test_diagnostics_scrub_the_apps_current_name_and_site_ids(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    setup_integration: FakeBridge,
    access_token: str,
) -> None:
    bridge = setup_integration
    bridge.status_data["domain"] = "newhome.duckdns.org"
    bridge.status_data["key_server"]["last_error"] = (
        "[newhome.duckdns.org] acme: error presenting token"
    )
    await config_entry.runtime_data.bridge.status.async_refresh()
    # A DuckDNS label that is also a key name must not unmask that key.
    bridge.send({"type": "hello", "api": 1, "domain": "token.duckdns.org"})
    site = config_entry.runtime_data.energy_sites[0]
    site.info.data = {
        **site.info.data,
        "id": "STE20231201-00042",
        "components_gateways": [
            {
                "device_id": "0b6f3c1e-5f7a-4c3e-9d1a-000000000001",
                "din": "1232100-00-E--TG000000000001",
                "site_id": "8f2a7c55-1b1e-4c4b-a1d2-000000000002",
            }
        ],
    }
    dump = json.dumps(await async_get_config_entry_diagnostics(hass, config_entry))
    for secret in (
        "newhome",
        access_token,
        "STE20231201-00042",
        "0b6f3c1e-5f7a-4c3e-9d1a-000000000001",
        "TG000000000001",
        "8f2a7c55-1b1e-4c4b-a1d2-000000000002",
    ):
        assert secret not in dump, secret
