"""Setup, streaming into entities, commands and telemetry reconciliation."""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry
from tesla_fleet_api.tesla.jws import public_key_bytes, verify

from custom_components.tesla_bridge import (
    async_reconcile_telemetry,
    missing_key_issue_id,
)
from custom_components.tesla_bridge.const import DOMAIN
from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .conftest import CA_PEM, VIN1, VIN2, FakeBridge


async def test_setup_registers_with_bridge(
    hass: HomeAssistant, config_entry: MockConfigEntry, setup_integration: FakeBridge
) -> None:
    bridge = setup_integration
    assert config_entry.state is ConfigEntryState.LOADED
    assert bridge.vins == [VIN1, VIN2]
    assert bridge.public_key and "BEGIN PUBLIC KEY" in bridge.public_key
    assert bridge.on_event is not None  # stream task running
    assert hass.states.get("sensor.model_y_battery_level").state == STATE_UNKNOWN


async def test_stream_updates_entities(
    hass: HomeAssistant, setup_integration: FakeBridge
) -> None:
    bridge = setup_integration
    bridge.on_connection(True)
    bridge.send({"type": "hello", "api": 1})
    bridge.send(
        {
            "type": "snapshot",
            "vehicles": {
                VIN1: {
                    "connected": True,
                    "fields": {
                        "BatteryLevel": {"v": 71.25, "ts": "2026-10-29T10:00:00Z"},
                        "Locked": {"v": True, "ts": "2026-10-29T10:00:00Z"},
                        "DetailedChargeState": {
                            "v": "DetailedChargeStateCharging",
                            "ts": "2026-10-29T10:00:00Z",
                        },
                        "Location": {
                            "v": {"latitude": 37.39, "longitude": -122.15},
                            "ts": "2026-10-29T10:00:00Z",
                        },
                        "EstBatteryRange": {
                            "v": "123.00000",
                            "ts": "2026-10-29T10:00:00Z",
                        },
                    },
                    "signals": {
                        "count": 42,
                        "day": dt_util.utcnow().date().isoformat(),
                        "month_count": 1500,
                        "month": dt_util.utcnow().date().isoformat()[:7],
                    },
                },
                "UNKNOWNVIN": {"connected": True, "fields": {}},
            },
        }
    )
    await hass.async_block_till_done()

    assert hass.states.get("sensor.model_y_battery_level").state == "71.2"
    assert hass.states.get("lock.model_y_doors").state == "locked"
    assert hass.states.get("binary_sensor.model_y_charging").state == "on"
    assert hass.states.get("binary_sensor.model_y_streaming").state == "on"
    assert hass.states.get("sensor.model_y_charging_state").state == "charging"
    tracker = hass.states.get("device_tracker.model_y_location")
    assert tracker.attributes["latitude"] == 37.39
    assert hass.states.get("sensor.model_y_streamed_values_today").state == "42"
    assert (
        hass.states.get("binary_sensor.tesla_bridge_app_stream_connected").state == "on"
    )

    # A live update and the car going to sleep.
    bridge.send(
        {
            "type": "data",
            "vin": VIN1,
            "ts": "2026-10-29T10:00:05Z",
            "fields": {
                "BatteryLevel": 72.0,
                "DetailedChargeState": "DetailedChargeStateComplete",
            },
        }
    )
    bridge.send({"type": "connectivity", "vin": VIN1, "connected": False})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.model_y_battery_level").state == "72.0"
    assert hass.states.get("binary_sensor.model_y_charging").state == "off"
    assert hass.states.get("binary_sensor.model_y_streaming").state == "off"
    # Values are kept while asleep.
    assert hass.states.get("lock.model_y_doors").state == "locked"


async def test_lock_command_optimistic(
    hass: HomeAssistant, setup_integration: FakeBridge, mock_tesla: MagicMock
) -> None:
    bridge = setup_integration
    bridge.send(
        {
            "type": "snapshot",
            "vehicles": {
                VIN1: {
                    "connected": True,
                    "fields": {"Locked": {"v": True, "ts": "2026-10-29T10:00:00Z"}},
                }
            },
        }
    )
    await hass.async_block_till_done()
    await hass.services.async_call(
        LOCK_DOMAIN, "unlock", {ATTR_ENTITY_ID: "lock.model_y_doors"}, blocking=True
    )
    mock_tesla.vehicle_apis[VIN1].door_unlock.assert_awaited_once()
    # The car is connected (streaming), so no wake-up was needed.
    mock_tesla.vehicle_apis[VIN1].wake_up.assert_not_awaited()
    assert hass.states.get("lock.model_y_doors").state == "unlocked"


async def test_command_wakes_sleeping_car(
    hass: HomeAssistant, setup_integration: FakeBridge, mock_tesla: MagicMock
) -> None:
    api = mock_tesla.vehicle_apis[VIN2]
    api.vehicle.return_value = {"response": {"state": "asleep"}}
    await hass.services.async_call(
        NUMBER_DOMAIN,
        "set_value",
        {ATTR_ENTITY_ID: "number.model_3_charge_limit", "value": 85},
        blocking=True,
    )
    api.wake_up.assert_awaited_once()
    api.set_charge_limit.assert_awaited_once_with(85)
    assert float(hass.states.get("number.model_3_charge_limit").state) == 85


async def test_reconcile_pushes_signed_config_once(
    hass: HomeAssistant,
    setup_integration: FakeBridge,
    mock_tesla: MagicMock,
    config_entry: MockConfigEntry,
) -> None:
    for vin in (VIN1, VIN2):
        push = mock_tesla.vehicle_apis[vin].fleet_telemetry_config_jws
        push.assert_awaited_once()
        vins, token = push.await_args.args
        assert vins == [vin]
        header, payload, signature = token.split(".")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
        assert claims["hostname"] == "myhome.duckdns.org"
        assert claims["port"] == 8443
        assert claims["ca"] == CA_PEM
        assert claims["aud"] == "com.tesla.fleet.TelemetryClient"
        assert "Location" in claims["fields"]
        assert verify(
            public_key_bytes(mock_tesla.private_key),
            f"{header}.{payload}".encode(),
            base64.urlsafe_b64decode(signature + "=="),
        )

    # Unchanged config: nothing is sent again.
    await async_reconcile_telemetry(hass, config_entry)
    assert mock_tesla.vehicle_apis[VIN1].fleet_telemetry_config_jws.await_count == 1
    # Forced (the diagnostic button): sent again.
    await async_reconcile_telemetry(hass, config_entry, force=True)
    assert mock_tesla.vehicle_apis[VIN1].fleet_telemetry_config_jws.await_count == 2


async def test_missing_key_raises_issue(
    hass: HomeAssistant,
    setup_integration: FakeBridge,
    mock_tesla: MagicMock,
    config_entry: MockConfigEntry,
) -> None:
    mock_tesla.vehicle_apis[VIN2].fleet_telemetry_config_jws.return_value = {
        "response": {"updated_vehicles": 0, "skipped_vehicles": {"missing_key": [VIN2]}}
    }
    await async_reconcile_telemetry(hass, config_entry, force=True)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, missing_key_issue_id(VIN2))
    assert VIN2 not in missing_key_issue_id(VIN2)
    assert issue is not None
    assert (
        issue.translation_placeholders["url"]
        == "https://www.tesla.com/_ak/myhome.duckdns.org"
    )


async def test_energy_site_cloud_fallback(
    hass: HomeAssistant, setup_integration: FakeBridge, mock_tesla: MagicMock
) -> None:
    assert hass.states.get("sensor.home_percentage_charged").state == "33.6"
    assert hass.states.get("sensor.home_solar_power").state == "4.8"
    assert hass.states.get("binary_sensor.home_grid_connected").state == "on"
    assert float(hass.states.get("number.home_backup_reserve").state) == 20
    assert hass.states.get("switch.home_storm_watch").state == "on"
    assert hass.states.get("switch.home_allow_charging_from_grid").state == "off"
    assert hass.states.get("select.home_energy_exports").state == "pv_only"

    await hass.services.async_call(
        NUMBER_DOMAIN,
        "set_value",
        {ATTR_ENTITY_ID: "number.home_backup_reserve", "value": 30},
        blocking=True,
    )
    mock_tesla.site_api.backup.assert_awaited_once_with(30)


async def test_unload(
    hass: HomeAssistant, config_entry: MockConfigEntry, setup_integration: FakeBridge
) -> None:
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_adopts_old_entities_keeping_entity_ids(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_tesla: MagicMock,
    mock_oauth: MagicMock,
    bridge: FakeBridge,
) -> None:
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    fleet = MockConfigEntry(domain="tesla_fleet", unique_id="uid", title="Tesla Fleet")
    powerwall = MockConfigEntry(
        domain="powerwall", unique_id="TG000000000001", title="Home Powerwall"
    )
    fleet.add_to_hass(hass)
    powerwall.add_to_hass(hass)
    old_battery = registry.async_get_or_create(
        "sensor",
        "tesla_fleet",
        f"{VIN1}-charge_state_battery_level",
        config_entry=fleet,
        suggested_object_id="my_tesla_battery",
    )
    old_lock = registry.async_get_or_create(
        "lock",
        "tesla_fleet",
        f"{VIN1}-vehicle_state_locked",
        config_entry=fleet,
        suggested_object_id="my_tesla_lock",
    )
    old_import = registry.async_get_or_create(
        "sensor",
        "powerwall",
        "TG000000000001_site_import",
        config_entry=powerwall,
        suggested_object_id="grid_imported_kwh",
    )
    old_fleet_solar = registry.async_get_or_create(
        "sensor",
        "tesla_fleet",
        f"{1234567}-solar_power",
        config_entry=fleet,
        suggested_object_id="fleet_solar",
    )
    unrelated = registry.async_get_or_create(
        "sensor", "tesla_fleet", f"{VIN1}-vin", config_entry=fleet
    )

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="uid",
        title=config_entry.title,
        data={**config_entry.data, "adopt": True},
        options=dict(config_entry.options),
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    battery = registry.async_get(old_battery.entity_id)
    assert battery.platform == DOMAIN
    assert battery.unique_id == f"{VIN1}-battery_level"
    assert battery.config_entry_id == config_entry.entry_id
    assert registry.async_get(old_lock.entity_id).platform == DOMAIN
    assert registry.async_get(old_import.entity_id).unique_id == "1234567-grid_imported"
    assert (
        registry.async_get(old_fleet_solar.entity_id).unique_id == "1234567-solar_power"
    )
    assert registry.async_get(unrelated.entity_id).platform == "tesla_fleet"
    # The moved entity keeps its ID and now gets its state from the stream.
    assert hass.states.get("sensor.my_tesla_battery") is not None
    assert hass.states.get("sensor.model_y_battery_level") is None
    assert fleet.disabled_by is not None
    assert powerwall.disabled_by is not None
    assert config_entry.data["adopted"] is True
