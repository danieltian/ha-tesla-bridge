"""Regression tests for issues found in the pre-publish review."""

from __future__ import annotations

import copy
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tesla_bridge import async_retry_unpaired
from custom_components.tesla_bridge.const import (
    CONF_BRIDGE,
    CONF_POWERWALL,
    CONF_PRESET,
    CONF_TELEMETRY_PUSHED,
    DOMAIN,
)
from custom_components.tesla_bridge.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.tesla_bridge.telemetry_config import (
    TelemetryTarget,
    build_config,
    config_hash,
)
from homeassistant.components.select import DOMAIN as SELECT_DOMAIN
from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from .conftest import CA_PEM, PRODUCTS, SITE_ID, VIN1, VIN2, FakeBridge


async def test_token_refresh_does_not_reload(
    hass: HomeAssistant, config_entry: MockConfigEntry, setup_integration: FakeBridge
) -> None:
    with patch.object(hass.config_entries, "async_reload") as reload:
        hass.config_entries.async_update_entry(
            config_entry,
            data={
                **config_entry.data,
                "token": {**config_entry.data["token"], "access_token": "new"},
            },
        )
        await hass.async_block_till_done()
    reload.assert_not_called()


async def test_options_change_reloads(
    hass: HomeAssistant, config_entry: MockConfigEntry, setup_integration: FakeBridge
) -> None:
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await hass.config_entries.options.async_init(config_entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_PRESET: "essential", "upnp": False, "powerwall_interval": 10},
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert reload.called


async def test_setup_flow_push_is_not_repeated(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_tesla: MagicMock,
    mock_oauth: MagicMock,
    bridge: FakeBridge,
) -> None:
    config = build_config(
        TelemetryTarget("myhome.duckdns.org", 8443, CA_PEM), "standard"
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="uid",
        data={**config_entry.data, CONF_TELEMETRY_PUSHED: {VIN1: config_hash(config)}},
        options=dict(config_entry.options),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    mock_tesla.vehicle_apis[VIN1].fleet_telemetry_config_jws.assert_not_awaited()
    mock_tesla.vehicle_apis[VIN2].fleet_telemetry_config_jws.assert_awaited_once()


async def test_diagnostics_contain_no_identifiers(
    hass: HomeAssistant, config_entry: MockConfigEntry, setup_integration: FakeBridge
) -> None:
    bridge = setup_integration
    bridge.status_data["relay"]["vehicles"] = {VIN1: {"connected": True}}
    bridge.status_data["selftest"] = {
        "resolved_ip": "203.0.113.7",
        "telemetry_loopback": {
            "ok": False,
            "detail": "no connection to 203.0.113.7:8443",
        },
    }
    await config_entry.runtime_data.bridge.status.async_refresh()
    bridge.send({"type": "hello", "api": 1, "domain": "myhome.duckdns.org"})
    dump = json.dumps(await async_get_config_entry_diagnostics(hass, config_entry))
    for secret in (VIN1, VIN2, "myhome", "203.0.113.7", str(SITE_ID)):
        assert secret not in dump, secret


async def test_steering_wheel_heat_has_no_medium(
    hass: HomeAssistant, setup_integration: FakeBridge, mock_tesla: MagicMock
) -> None:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        SELECT_DOMAIN, DOMAIN, f"{VIN1}-steering_wheel_heat_level"
    )
    registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(setup_integration_entry(hass).entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state.attributes["options"] == ["off", "low", "high"]
    setup_integration.send(
        {
            "type": "snapshot",
            "vehicles": {
                VIN1: {
                    "connected": True,
                    "fields": {
                        "HvacPower": {
                            "v": "HvacPowerStateOn",
                            "ts": "2026-10-29T10:00:00Z",
                        }
                    },
                }
            },
        }
    )
    await hass.services.async_call(
        SELECT_DOMAIN,
        "select_option",
        {ATTR_ENTITY_ID: entity_id, "option": "high"},
        blocking=True,
    )
    mock_tesla.vehicle_apis[
        VIN1
    ].remote_steering_wheel_heat_level_request.assert_awaited_once_with(3)
    assert hass.states.get(entity_id).state == "high"


def setup_integration_entry(hass: HomeAssistant) -> MockConfigEntry:
    return hass.config_entries.async_entries(DOMAIN)[0]


async def test_operation_mode_holds_until_confirmed(
    hass: HomeAssistant, setup_integration: FakeBridge, mock_tesla: MagicMock
) -> None:
    entity_id = "select.home_operation_mode"
    assert hass.states.get(entity_id).state == "self_consumption"
    await hass.services.async_call(
        SELECT_DOMAIN,
        "select_option",
        {ATTR_ENTITY_ID: entity_id, "option": "backup"},
        blocking=True,
    )
    await hass.async_block_till_done()
    # The cloud still reports the old mode right after the change.
    assert hass.states.get(entity_id).state == "backup"
    mock_tesla.site_api.operation.assert_awaited_once_with("backup")


async def test_solar_only_site_has_no_battery_sensors(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_tesla: MagicMock,
    mock_oauth: MagicMock,
    bridge: FakeBridge,
) -> None:
    products = copy.deepcopy(PRODUCTS)
    products[2]["components"] = {"battery": False, "solar": True}
    mock_tesla.products.return_value = {"response": products}
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.home_solar_power") is not None
    assert hass.states.get("sensor.home_percentage_charged") is None
    assert hass.states.get("sensor.home_battery_power") is None


async def test_unpaired_car_is_retried_once_paired(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    setup_integration: FakeBridge,
    mock_tesla: MagicMock,
) -> None:
    api = mock_tesla.vehicle_apis[VIN2]
    telemetry = config_entry.runtime_data.telemetry
    telemetry.vehicle_state(VIN2).update(
        ok=False, hash=None, skipped_reason="missing_key"
    )
    api.fleet_status.return_value = {
        "response": {"key_paired_vins": [VIN1], "unpaired_vins": [VIN2]}
    }
    pushes = api.fleet_telemetry_config_jws.await_count
    await async_retry_unpaired(hass, config_entry)
    assert (
        api.fleet_telemetry_config_jws.await_count == pushes
    )  # still unpaired: no push
    api.fleet_status.return_value = {
        "response": {"key_paired_vins": [VIN1, VIN2], "unpaired_vins": []}
    }
    await async_retry_unpaired(hass, config_entry)
    assert api.fleet_telemetry_config_jws.await_count == pushes + 1


async def test_bridge_details_refreshed_from_supervisor(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_tesla: MagicMock,
    mock_oauth: MagicMock,
    bridge: FakeBridge,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="uid",
        data={**config_entry.data, "app_slug": "abc_tesla_bridge"},
        options=dict(config_entry.options),
    )
    entry.add_to_hass(hass)
    discovery: dict[str, Any] = {
        "host": "abc-tesla-bridge",
        "port": 8099,
        "token": "rotated",
        "domain": "myhome.duckdns.org",
        "telemetry_port": 8443,
    }
    with (
        patch("homeassistant.helpers.hassio.is_hassio", return_value=True),
        patch(
            "custom_components.tesla_bridge.app_manager.async_app_discovery",
            AsyncMock(return_value=discovery),
        ),
        patch(
            "custom_components.tesla_bridge.app_manager.async_app_host_ports",
            AsyncMock(return_value=(10443, 18443)),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.data[CONF_BRIDGE]["token"] == "rotated"
    assert entry.data["telemetry_host_port"] == 18443


async def test_reconfigure_powerwall(
    hass: HomeAssistant, config_entry: MockConfigEntry, setup_integration: FakeBridge
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_RECONFIGURE, "entry_id": config_entry.entry_id},
    )
    # No app slug (standalone bridge): the bridge settings are offered too.
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["reconfigure_powerwall", "reconfigure_bridge"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "reconfigure_powerwall"}
    )
    assert result["step_id"] == "reconfigure_powerwall"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "10.0.0.9"}
    )
    assert result["errors"] == {"password": "required"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "[::1", "password": "ABCDE"}
    )
    assert result["errors"] == {"host": "invalid_host"}
    with (
        patch("custom_components.tesla_bridge.config_flow.LocalPowerwall") as local,
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        local.return_value.async_login = AsyncMock(return_value="DIN")
        local.return_value.async_close = AsyncMock()
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "10.0.0.9", "password": "ABCDE"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_POWERWALL] == {
        "host": "10.0.0.9",
        "password": "ABCDE",
    }
