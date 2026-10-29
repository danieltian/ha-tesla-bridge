"""Fast local polling of the Powerwall gateway (customer password login)."""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
from typing import Any

from aiohttp import CookieJar
from tesla_powerwall import (
    AccessDeniedError,
    ApiError,
    MeterType,
    MissingAttributeError,
    Powerwall,
    PowerwallUnreachableError,
)
from yarl import URL

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import LOGGER


def app_percentage(raw: float | None) -> float | None:
    """Convert a gateway percentage to what the Tesla app shows.

    The gateway reserves 5% of the pack; the app shows (raw - 5) / 0.95.
    """
    if raw is None:
        return None
    return round(max(0.0, min(100.0, (raw - 5) / 0.95)), 1)


def raw_percentage(app_value: float) -> float:
    """Inverse of :func:`app_percentage` (for the local reserve value)."""
    return round(app_value * 0.95 + 5, 1)


@dataclass
class LocalMeter:
    """One meter (site, battery, load, solar, generator)."""

    instant_power: float
    energy_exported: float
    energy_imported: float
    frequency: float | None
    voltage: float | None
    current: float | None


@dataclass
class LocalBattery:
    """One Powerwall unit."""

    serial: str
    part_number: str
    capacity: int | None
    energy_remaining: int | None
    p_out: float | None
    v_out: float | None
    f_out: float | None
    energy_charged: int | None
    energy_discharged: int | None
    grid_state: str | None


@dataclass
class LocalPowerwallData:
    """Snapshot of the gateway."""

    charge_raw: float
    meters: dict[str, LocalMeter]
    grid_status: str
    grid_services_active: bool
    backup_reserve_raw: float | None
    operation_mode: str | None
    batteries: dict[str, LocalBattery] = field(default_factory=dict)
    sitemaster_running: bool | None = None
    connected_to_tesla: bool | None = None
    updated: dt.datetime | None = None

    @property
    def charge(self) -> float | None:
        return app_percentage(self.charge_raw)

    @property
    def backup_reserve(self) -> float | None:
        return app_percentage(self.backup_reserve_raw)


class PowerwallAuthError(Exception):
    """The customer password was rejected."""


class LocalPowerwall:
    """Owns the gateway session and re-logs in when the cookie expires."""

    def __init__(self, hass: HomeAssistant, host: str, password: str) -> None:
        self.host = host
        self._password = password
        # A malformed host raises ValueError here, before there is a session
        # that nothing would close.
        URL(host if host.startswith("http") else f"https://{host}")
        self._session = async_create_clientsession(
            hass, verify_ssl=False, cookie_jar=CookieJar(unsafe=True)
        )
        self.api = Powerwall(host, http_session=self._session, verify_ssl=False)
        self.din: str | None = None
        self.restricted = False

    async def async_login(self) -> str:
        """Log in and return the gateway DIN."""
        try:
            await self.api.login(self._password)
            self.din = await self.api.get_gateway_din()
        except AccessDeniedError as err:
            raise PowerwallAuthError from err
        try:
            await self.api.get_batteries()
        except ApiError:
            # Powerwall 3 exposes a restricted local API without per-battery data.
            self.restricted = True
        return self.din

    async def _fetch(self) -> LocalPowerwallData:
        api = self.api
        charge = await api.get_charge()
        meters_resp = await api.get_meters()
        meters: dict[str, LocalMeter] = {}
        for meter_type in MeterType:
            meter = meters_resp.get_meter(meter_type)
            if meter is None:
                continue
            meters[meter_type.value] = LocalMeter(
                instant_power=meter.instant_power,
                energy_exported=meter.energy_exported,
                energy_imported=meter.energy_imported,
                frequency=meter.frequency,
                voltage=meter.instant_average_voltage,
                current=meter.instant_total_current,
            )
        data = LocalPowerwallData(
            charge_raw=charge,
            meters=meters,
            grid_status=(await api.get_grid_status()).value,
            grid_services_active=await api.is_grid_services_active(),
            backup_reserve_raw=None,
            operation_mode=None,
        )
        if not self.restricted:
            data.backup_reserve_raw = await api.get_backup_reserve_percentage()
            data.operation_mode = (await api.get_operation_mode()).value
            sitemaster = await api.get_sitemaster()
            data.sitemaster_running = sitemaster.is_running
            data.connected_to_tesla = sitemaster.is_connected_to_tesla
            for battery in await api.get_batteries():
                data.batteries[battery.serial_number] = LocalBattery(
                    serial=battery.serial_number,
                    part_number=battery.part_number,
                    capacity=battery.capacity,
                    energy_remaining=battery.energy_remaining,
                    p_out=battery.p_out,
                    v_out=battery.v_out,
                    f_out=battery.f_out,
                    energy_charged=battery.energy_charged,
                    energy_discharged=battery.energy_discharged,
                    grid_state=battery.grid_state.value if battery.grid_state else None,
                )
        data.updated = dt.datetime.now(dt.UTC)
        return data

    async def async_fetch(self) -> LocalPowerwallData:
        for attempt in range(2):
            try:
                return await self._fetch()
            except AccessDeniedError:
                if attempt:
                    raise
                LOGGER.debug("Powerwall session expired; logging in again")
                await self.async_login()
        raise RuntimeError("unreachable")

    async def async_close(self) -> None:
        # Powerwall.close() leaves a session it was given open; close ours too,
        # or every reload would leak one.
        await self.api.close()
        await self._session.close()


class LocalPowerwallCoordinator(DataUpdateCoordinator[LocalPowerwallData]):
    """Polls the gateway every few seconds."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: LocalPowerwall,
        interval: int,
    ) -> None:
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"Powerwall {client.host}",
            update_interval=dt.timedelta(seconds=interval),
            always_update=False,
        )
        self.client = client

    async def _async_update_data(self) -> LocalPowerwallData:
        try:
            return await self.client.async_fetch()
        except (TimeoutError, PowerwallUnreachableError) as err:
            raise UpdateFailed(f"Powerwall gateway unreachable: {err}") from err
        except (AccessDeniedError, PowerwallAuthError) as err:
            raise UpdateFailed("Powerwall rejected the customer password") from err
        except (MissingAttributeError, ApiError) as err:
            raise UpdateFailed(f"Powerwall API error: {err}") from err


def meter_or_none(data: LocalPowerwallData | None, meter: str) -> LocalMeter | None:
    if data is None:
        return None
    return data.meters.get(meter)


def as_dict(data: LocalPowerwallData | None) -> dict[str, Any]:
    if data is None:
        return {}
    return {
        "charge_raw": data.charge_raw,
        "grid_status": data.grid_status,
        "operation_mode": data.operation_mode,
        "backup_reserve_raw": data.backup_reserve_raw,
        "batteries": len(data.batteries),
    }
