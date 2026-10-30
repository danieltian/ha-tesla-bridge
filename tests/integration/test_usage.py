"""Fleet API usage and cost tracking, and the daily streaming-setup re-check."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiohttp import ClientError
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from tesla_fleet_api.const import Method
from tesla_fleet_api.exceptions import TeslaFleetError

from custom_components.tesla_bridge import (
    async_check_budget,
    async_reconcile_telemetry,
)
from custom_components.tesla_bridge.const import DOMAIN
from custom_components.tesla_bridge.sensor import (
    ENERGY_SENSORS,
    _energy_left,
    _uses_local,
)
from custom_components.tesla_bridge.usage import (
    COMMANDS,
    DATA,
    OTHER,
    STREAMING,
    WAKES,
    ApiUsage,
    billing_category,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir

from .conftest import VIN1, FakeBridge


def test_billing_categories() -> None:
    assert billing_category("api/1/vehicles/V/vehicle_data") == DATA
    assert billing_category("api/1/vehicles/V/vehicle_data?endpoints=x") == DATA
    assert billing_category("api/1/vehicles/V/wake_up") == WAKES
    assert billing_category("api/1/vehicles/V/command/door_lock") == COMMANDS
    assert billing_category("api/1/vehicles/V/signed_command") == COMMANDS
    for unbilled in (
        "api/1/vehicles/V",
        "api/1/products",
        "api/1/vehicles/fleet_status",
        "api/1/vehicles/fleet_telemetry_config_jws",
        "api/1/vehicles/V/fleet_telemetry_config",
        "api/1/energy_sites/1/site_info",
        "api/1/energy_sites/1/backup",
    ):
        assert billing_category(unbilled) == OTHER, unbilled


async def test_counts_what_tesla_bills(hass: HomeAssistant) -> None:
    usage = ApiUsage(hass, "e")
    tesla = MagicMock()
    tesla._request = AsyncMock(
        side_effect=[
            {"response": {"result": True}},
            TeslaFleetError(status=408),  # answered: billed
            TeslaFleetError(status=503),  # Tesla does not bill 5xx
            ClientError("no answer"),
        ]
    )
    usage.instrument(tesla)
    await tesla._request(Method.POST, "api/1/vehicles/V/command/door_lock")
    with pytest.raises(TeslaFleetError):
        await tesla._request(Method.POST, "api/1/vehicles/V/wake_up")
    with pytest.raises(TeslaFleetError):
        await tesla._request(Method.POST, "api/1/vehicles/V/wake_up")
    with pytest.raises(ClientError):
        await tesla._request(Method.GET, "api/1/vehicles/V/vehicle_data")
    assert usage.counts == {COMMANDS: 1, WAKES: 1}


def _vehicle(month: str, count: int) -> MagicMock:
    vehicle = MagicMock()
    vehicle.stream.signals = {"month": month, "month_count": count}
    return vehicle


async def test_summary_prices_and_projects(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-10-01T00:00:00+00:00")
    usage = ApiUsage(hass, "e")  # counting since the 1st
    freezer.move_to("2026-10-11T00:00:00+00:00")  # 10 of 31 days gone
    usage.counts = {COMMANDS: 100, DATA: 50, WAKES: 2}
    summary = usage.summary(
        [_vehicle("2026-10", 150_000), _vehicle("2026-09", 999_999)]
    )
    assert summary["counts"][STREAMING] == 150_000  # last month's car ignored
    # $1 streaming + $0.10 commands + $0.10 data + $0.04 wakes
    assert summary["total"] == pytest.approx(1.24)
    assert summary["projected"] == pytest.approx(1.24 * 31 / 10)
    assert summary["full_month"] == pytest.approx(1.24 * 31 / 10)
    assert summary["top"] == STREAMING
    assert summary["requests"] == 152

    # A new month starts from zero, and a few hours in the rate is measured
    # over at least a day.
    freezer.move_to("2026-11-01T02:00:00+00:00")
    assert usage.summary([])["total"] == 0
    usage.counts = {WAKES: 5}
    summary = usage.summary([])
    assert summary["full_month"] == pytest.approx(0.10 * 30)
    assert summary["projected"] == pytest.approx(0.10 + 0.10 * (30 - 2 / 24))


async def test_projects_from_when_counting_started(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    # Installed late in October: the days before must not dilute the rate.
    freezer.move_to("2026-10-29T12:00:00+00:00")
    usage = ApiUsage(hass, "e")
    freezer.move_to("2026-10-30T12:00:00+00:00")  # one day counted
    usage.counts = {WAKES: 5}  # $0.10 in that day
    summary = usage.summary([])
    assert summary["since"] == "2026-10-29T12:00:00+00:00"
    assert summary["full_month"] == pytest.approx(0.10 * 31)
    # This month's bill: $0.10 so far, plus a day and a half more.
    assert summary["projected"] == pytest.approx(0.10 + 0.10 * 1.5)


async def test_counts_persist_within_the_month(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to("2026-10-20T00:00:00+00:00")
    key = f"{DOMAIN}.e.usage"
    hass_storage[key] = {
        "version": 1,
        "key": key,
        "data": {
            "month": "2026-10",
            "counts": {WAKES: 3},
            "since": "2026-10-05T08:00:00+00:00",
        },
    }
    usage = ApiUsage(hass, "e")
    await usage.async_load()
    assert usage.counts == {WAKES: 3}
    assert usage.since.isoformat() == "2026-10-05T08:00:00+00:00"

    # Saved by a version without "since": count from now (a safe, higher rate).
    del hass_storage[key]["data"]["since"]
    legacy = ApiUsage(hass, "e")
    await legacy.async_load()
    assert legacy.since.isoformat() == "2026-10-20T00:00:00+00:00"

    # Counting ran last month, so it covers this whole month.
    hass_storage[key]["data"]["month"] = "2026-09"
    stale = ApiUsage(hass, "e")
    await stale.async_load()
    assert stale.counts == {}
    assert stale.since.isoformat() == "2026-10-01T00:00:00+00:00"


def test_energy_left_comes_from_the_gateway() -> None:
    battery = MagicMock(energy_remaining=6_400)
    data = MagicMock(batteries={"a": battery, "b": MagicMock(energy_remaining=6_100)})
    assert _energy_left(data) == 12.5
    assert _energy_left(MagicMock(batteries={})) is None

    description = next(d for d in ENERGY_SENSORS if d.key == "energy_left")
    site = MagicMock()
    site.local.client.restricted = False
    # Read locally: nothing keeps Tesla's cloud live status polling.
    assert _uses_local(site, description)
    # A Powerwall 3 gateway has no per-battery data: use the cloud.
    site.local.client.restricted = True
    assert not _uses_local(site, description)
    site.local = None
    assert not _uses_local(site, description)


async def test_usage_sensors_follow_requests(
    hass: HomeAssistant, config_entry: MockConfigEntry, setup_integration: FakeBridge
) -> None:
    registry = er.async_get(hass)

    def entity_id(key: str) -> str:
        found = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{config_entry.entry_id}-usage-{key}"
        )
        assert found is not None, key
        return found

    requests = entity_id("api_requests_month")
    before = int(hass.states.get(requests).state)
    await config_entry.runtime_data.tesla._request(
        Method.POST, f"api/1/vehicles/{VIN1}/wake_up"
    )
    await hass.async_block_till_done()
    state = hass.states.get(requests)
    assert int(state.state) == before + 1
    assert state.attributes["wake-ups"] == 1
    cost = hass.states.get(entity_id("api_cost_month"))
    assert float(cost.state) >= 0.02
    assert cost.attributes["wake-ups"] == "1 ($0.02)"
    assert float(hass.states.get(entity_id("api_cost_projected")).state) >= 0.02


async def test_budget_warning(
    hass: HomeAssistant, config_entry: MockConfigEntry, setup_integration: FakeBridge
) -> None:
    usage = config_entry.runtime_data.usage
    usage.counts[WAKES] = 1_000  # $20 of wake-ups
    async_check_budget(hass, config_entry)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "api_budget")
    assert issue is not None
    assert issue.translation_placeholders["top"] == "wake-ups"
    usage.counts.clear()
    async_check_budget(hass, config_entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "api_budget") is None


async def test_streaming_setup_sent_again_when_tesla_removed_it(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    setup_integration: FakeBridge,
    mock_tesla: MagicMock,
) -> None:
    await hass.async_block_till_done()
    api = mock_tesla.vehicle_apis[VIN1]
    state = config_entry.runtime_data.telemetry.vehicle_state(VIN1)
    assert state.get("ok")  # pushed during setup
    pushes = api.fleet_telemetry_config_jws.await_count

    # Tesla answered without a config (suspended app): send it again.
    state.pop("checked", None)
    api.fleet_telemetry_config_get.return_value = {
        "response": {"synced": True, "config": None}
    }
    await async_reconcile_telemetry(hass, config_entry)
    assert api.fleet_telemetry_config_jws.await_count == pushes + 1

    # Just checked: no further check or push for the rest of the day.
    checks = api.fleet_telemetry_config_get.await_count
    await async_reconcile_telemetry(hass, config_entry)
    assert api.fleet_telemetry_config_get.await_count == checks
    assert api.fleet_telemetry_config_jws.await_count == pushes + 1


async def test_unreadable_answer_never_triggers_a_push(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    setup_integration: FakeBridge,
    mock_tesla: MagicMock,
) -> None:
    await hass.async_block_till_done()
    api = mock_tesla.vehicle_apis[VIN1]
    state = config_entry.runtime_data.telemetry.vehicle_state(VIN1)
    pushes = api.fleet_telemetry_config_jws.await_count
    for answer in (
        {"response": {"synced": True}},  # no "config" key at all
        TeslaFleetError(status=403),
        ClientError("no answer"),
    ):
        state.pop("checked", None)
        if isinstance(answer, dict):
            api.fleet_telemetry_config_get.side_effect = None
            api.fleet_telemetry_config_get.return_value = answer
        else:
            api.fleet_telemetry_config_get.side_effect = answer
        await async_reconcile_telemetry(hass, config_entry)
    assert api.fleet_telemetry_config_jws.await_count == pushes

    # A setup pointing somewhere else (an old name) is replaced.
    state.pop("checked", None)
    api.fleet_telemetry_config_get.side_effect = None
    api.fleet_telemetry_config_get.return_value = {
        "response": {"config": {"hostname": "old.duckdns.org", "port": 8443}}
    }
    await async_reconcile_telemetry(hass, config_entry)
    assert api.fleet_telemetry_config_jws.await_count == pushes + 1
