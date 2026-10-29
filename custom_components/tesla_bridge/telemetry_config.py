"""Build, sign, push and reconcile each vehicle's ``fleet_telemetry_config``."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import TYPE_CHECKING, Any

from tesla_fleet_api.const import Method
from tesla_fleet_api.exceptions import TeslaFleetError
from tesla_fleet_api.tesla.jws import sign_fleet_telemetry_config

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LOGGER, TelemetryPreset
from .fields import fields_for_preset

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric import ec
    from tesla_fleet_api import TeslaFleetApi
    from tesla_fleet_api.tesla import VehicleFleet

    from .models import VehicleData

STORE_VERSION = 1


@dataclass(frozen=True, slots=True)
class TelemetryTarget:
    """Where the cars should stream to."""

    hostname: str
    port: int
    ca_pem: str


def build_config(
    target: TelemetryTarget,
    preset: TelemetryPreset | str,
    overrides: dict[str, int | None] | None = None,
) -> dict[str, Any]:
    """The ``config`` object (signed into the JWS) for one vehicle."""
    return {
        "hostname": target.hostname,
        "port": target.port,
        "ca": target.ca_pem,
        # Ask the car to resend anything the server did not acknowledge
        # (e.g. while the app was restarting).
        "delivery_policy": "latest",
        "alert_types": ["service"],
        "fields": fields_for_preset(preset, overrides),
    }


def config_hash(config: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass
class PushResult:
    """Outcome of configuring one vehicle."""

    vin: str
    ok: bool
    skipped_reason: str | None = None
    error: str | None = None


async def push_config(
    api: VehicleFleet,
    vin: str,
    private_key: ec.EllipticCurvePrivateKey,
    config: dict[str, Any],
) -> PushResult:
    """Sign ``config`` as a Tesla.SS256 JWS and send it for one vehicle.

    Always the JWS endpoint: only apps registered with a legacy CSR may post an
    unsigned config. This mirrors what Tesla's vehicle-command proxy does.
    """
    token = sign_fleet_telemetry_config(private_key, config)
    try:
        response = await api.fleet_telemetry_config_jws([vin], token)
    except TeslaFleetError as err:
        LOGGER.warning("Configuring streaming for %s failed: %s", vin, err.message)
        return PushResult(vin, False, error=err.message)
    body = response.get("response") or {}
    reason: str | None = None
    for key, vins in (body.get("skipped_vehicles") or {}).items():
        if vin in (vins or []):
            reason = key
    ok = reason is None and int(body.get("updated_vehicles", 0)) >= 1
    if ok:
        LOGGER.info("Streaming configured for %s", vin)
    else:
        LOGGER.warning("Tesla skipped the streaming config for %s: %s", vin, reason)
    return PushResult(
        vin, ok, skipped_reason=reason, error=None if ok else (reason or "not updated")
    )


class TelemetryManager:
    """Keeps every vehicle's streaming config in line with the desired one."""

    def __init__(
        self, hass: HomeAssistant, entry_id: str, tesla: TeslaFleetApi
    ) -> None:
        self.hass = hass
        self._tesla = tesla
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, f"{DOMAIN}.{entry_id}.telemetry"
        )
        self.data: dict[str, Any] = {}

    async def async_load(self) -> None:
        self.data = await self._store.async_load() or {}

    def vehicle_state(self, vin: str) -> dict[str, Any]:
        return self.data.setdefault(vin, {})

    async def async_push(
        self, vehicle: VehicleData, config: dict[str, Any], *, force: bool = False
    ) -> PushResult:
        """Sign and send ``config`` unless the car already has it."""
        state = self.vehicle_state(vehicle.vin)
        digest = config_hash(config)
        if not force and state.get("hash") == digest and state.get("ok"):
            return PushResult(vehicle.vin, True)
        if self._tesla.private_key is None:
            return PushResult(vehicle.vin, False, error="no partner key loaded")
        result = await push_config(
            vehicle.api, vehicle.vin, self._tesla.private_key, config
        )
        state.update(
            hash=digest if result.ok else None,
            ok=result.ok,
            skipped_reason=result.skipped_reason,
            error=result.error,
            pushed=dt_util.utcnow().isoformat(),
            synced=False,
        )
        await self._store.async_save(self.data)
        return result

    def seed(self, pushed: dict[str, str]) -> None:
        """Remember configs the setup flow already pushed (no second push)."""
        for vin, digest in pushed.items():
            if vin not in self.data:  # the store's own record wins
                self.data[vin] = {"hash": digest, "ok": True}

    async def async_check(self, vehicle: VehicleData) -> dict[str, Any]:
        """Ask Tesla whether the car has adopted the config (``synced``)."""
        state = self.vehicle_state(vehicle.vin)
        try:
            response = await vehicle.api.fleet_telemetry_config_get()
        except TeslaFleetError as err:
            return {"error": err.message}
        body = response.get("response") or {}
        state["synced"] = bool(body.get("synced"))
        state["limit_reached"] = bool(body.get("limit_reached"))
        state["checked"] = dt_util.utcnow().isoformat()
        await self._store.async_save(self.data)
        return body

    async def async_errors(self, vin: str) -> list[dict[str, Any]]:
        """Recent errors the car reported about our server (not wrapped by the library)."""
        try:
            response = await self._tesla._request(
                Method.GET, f"api/1/vehicles/{vin}/fleet_telemetry_errors"
            )
        except TeslaFleetError as err:
            LOGGER.debug("fleet_telemetry_errors for %s failed: %s", vin, err.message)
            return []
        errors = (response.get("response") or {}).get("fleet_telemetry_errors") or []
        return [e for e in errors if isinstance(e, dict)]
