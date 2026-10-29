"""Runtime data for a Tesla Bridge config entry."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tesla_fleet_api.const import Scope
from tesla_fleet_api.tesla import EnergySite, VehicleFleet

from homeassistant.helpers.device_registry import DeviceInfo

if TYPE_CHECKING:
    from tesla_fleet_api import TeslaFleetApi

    from .bridge_client import BridgeClient
    from .coordinator import (
        BridgeStatusCoordinator,
        EnergySiteInfoCoordinator,
        EnergySiteLiveCoordinator,
    )
    from .powerwall_local import LocalPowerwallCoordinator
    from .stream import StreamHub, VehicleStream
    from .telemetry_config import TelemetryManager
    from .upnp import UpnpPortMapper


@dataclass
class VehicleData:
    """One car."""

    api: VehicleFleet
    vin: str
    name: str
    signing: bool
    device: DeviceInfo
    stream: VehicleStream
    wakelock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class EnergySiteData:
    """One energy site (Powerwall gateway)."""

    api: EnergySite
    site_id: int
    name: str
    device: DeviceInfo
    info: EnergySiteInfoCoordinator
    live: EnergySiteLiveCoordinator
    local: LocalPowerwallCoordinator | None
    has_battery: bool
    has_solar: bool
    gateway_din: str | None = None


@dataclass
class BridgeRuntime:
    """Connection to the companion app."""

    client: BridgeClient
    hub: StreamHub
    status: BridgeStatusCoordinator
    device: DeviceInfo
    stream_task: asyncio.Task[None] | None = None


@dataclass
class TeslaBridgeData:
    """Everything a config entry owns at runtime."""

    tesla: TeslaFleetApi
    scopes: list[Scope]
    vehicles: list[VehicleData]
    energy_sites: list[EnergySiteData]
    bridge: BridgeRuntime
    telemetry: TelemetryManager
    upnp: UpnpPortMapper | None = None
