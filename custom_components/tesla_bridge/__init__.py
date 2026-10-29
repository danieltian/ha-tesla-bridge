"""Tesla Bridge: Tesla vehicles and Powerwall with self-hosted Fleet Telemetry."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import datetime as dt
import hashlib
from typing import TYPE_CHECKING, Any, Final

from aiohttp import ClientError
import jwt
from tesla_fleet_api import TeslaFleetApi, is_valid_region
from tesla_fleet_api.const import Scope
from tesla_fleet_api.exceptions import (
    InvalidRegion,
    InvalidToken,
    LibraryError,
    LoginRequired,
    OAuthExpired,
    TeslaFleetError,
)
from tesla_fleet_api.tesla import VehicleFleet

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ACCESS_TOKEN, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    OAuth2TokenRequestError,
    OAuth2TokenRequestReauthError,
)
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import (
    OAuth2Session,
    async_get_config_entry_implementation,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

from .bridge_client import BridgeAuthError, BridgeClient, BridgeError
from .const import (
    CONF_ADOPT,
    CONF_ADOPTED,
    CONF_APP_SLUG,
    CONF_BRIDGE,
    CONF_KEY_HOST_PORT,
    CONF_POWERWALL,
    CONF_POWERWALL_HOST,
    CONF_POWERWALL_INTERVAL,
    CONF_POWERWALL_PASSWORD,
    CONF_PRESET,
    CONF_PUBLIC_DOMAIN,
    CONF_REGION,
    CONF_TELEMETRY_HOST_PORT,
    CONF_TELEMETRY_PORT,
    CONF_TELEMETRY_PUSHED,
    CONF_UPNP,
    DEFAULT_POWERWALL_INTERVAL,
    DEFAULT_PRESET,
    DEFAULT_TELEMETRY_PORT,
    DOMAIN,
    KEY_FILE,
    KEY_SERVER_PORT,
    LOGGER,
    MANUFACTURER,
    REGION_SERVERS,
)
from .coordinator import (
    BridgeStatusCoordinator,
    EnergySiteInfoCoordinator,
    EnergySiteLiveCoordinator,
)
from .models import BridgeRuntime, EnergySiteData, TeslaBridgeData, VehicleData
from .powerwall_local import (
    LocalPowerwall,
    LocalPowerwallCoordinator,
    PowerwallAuthError,
)
from .stream import StreamHub
from .telemetry_config import TelemetryManager, TelemetryTarget, build_config

PLATFORMS: Final = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.COVER,
    Platform.DEVICE_TRACKER,
    Platform.LOCK,
    Platform.MEDIA_PLAYER,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
]

RECONCILE_INTERVAL = dt.timedelta(hours=24)
UNPAIRED_RETRY_INTERVAL = dt.timedelta(minutes=10)
# Seconds between attempts while the app rejects the token or is unusable.
BRIDGE_RETRY = (5, 15, 60, 300)

if TYPE_CHECKING:
    from .upnp import PortMapping

type TeslaBridgeConfigEntry = ConfigEntry[TeslaBridgeData]


def register_device(
    registry: dr.DeviceRegistry, entry_id: str, info: DeviceInfo
) -> None:
    """Register a device from DeviceInfo (TypedDict before 2026.11, dataclass after)."""
    if hasattr(registry, "async_get_or_create_from_info"):
        registry.async_get_or_create_from_info(
            config_entry_id=entry_id, device_info=info
        )
    else:
        registry.async_get_or_create(config_entry_id=entry_id, **info)


async def _async_products(tesla: TeslaFleetApi) -> list[dict[str, Any]]:
    """Fetch products, retrying once on the correct region server."""
    for attempt in range(2):
        try:
            return (await tesla.products())["response"]
        except InvalidRegion:
            if attempt:
                raise
            LOGGER.warning("Region looks wrong; looking up the right Fleet API server")
            await tesla.find_server()
        except (InvalidToken, OAuthExpired, LoginRequired) as err:
            raise ConfigEntryAuthFailed from err
    raise RuntimeError("unreachable")


async def async_setup_entry(hass: HomeAssistant, entry: TeslaBridgeConfigEntry) -> bool:
    """Set up Tesla Bridge from a config entry."""
    implementation = await async_get_config_entry_implementation(hass, entry)
    oauth_session = OAuth2Session(hass, entry, implementation)
    try:
        await oauth_session.async_ensure_token_valid()
    except OAuth2TokenRequestReauthError as err:
        raise ConfigEntryAuthFailed from err
    except (OAuth2TokenRequestError, ClientError) as err:
        raise ConfigEntryNotReady from err

    claims = jwt.decode(
        oauth_session.token[CONF_ACCESS_TOKEN], options={"verify_signature": False}
    )
    scopes = [
        Scope(s) for s in claims.get("scp", []) if s in Scope.__members__.values()
    ]
    region_code = str(claims.get("ou_code", "")).lower()
    if not is_valid_region(region_code) or region_code not in REGION_SERVERS:
        region_code = entry.data.get(CONF_REGION) or next(iter(REGION_SERVERS))

    async def _access_token() -> str:
        await oauth_session.async_ensure_token_valid()
        return str(oauth_session.token[CONF_ACCESS_TOKEN])

    session = async_get_clientsession(hass)
    tesla = TeslaFleetApi(
        session=session,
        access_token=_access_token,
        region=region_code,
        charging_scope=False,
        partner_scope=False,
        energy_scope=Scope.ENERGY_DEVICE_DATA in scopes,
        vehicle_scope=Scope.VEHICLE_DEVICE_DATA in scopes,
    )
    await tesla.get_private_key(hass.config.path(KEY_FILE))

    try:
        products = await _async_products(tesla)
    except (TeslaFleetError, LibraryError, ClientError, TimeoutError) as err:
        raise ConfigEntryNotReady(f"Tesla Fleet API unavailable: {err}") from err

    # ---- companion app ------------------------------------------------------
    # The app can be reinstalled or reconfigured (new token, host, ports). With a
    # single config entry Home Assistant never re-delivers its discovery, so ask
    # the Supervisor directly.
    await _async_refresh_bridge(hass, entry)
    bridge_conf: dict[str, Any] = entry.data[CONF_BRIDGE]
    client = BridgeClient(
        session, bridge_conf["host"], int(bridge_conf["port"]), bridge_conf["token"]
    )
    hub = StreamHub()
    status = BridgeStatusCoordinator(hass, entry, client)
    await status.async_refresh()
    if not status.last_update_success:
        _raise_bridge_issue(hass, str(status.last_exception))
    else:
        ir.async_delete_issue(hass, DOMAIN, "bridge_unreachable")
        ir.async_delete_issue(hass, DOMAIN, "bridge_auth")
    _async_check_app_endpoint(hass, entry, status.data, reload=False)
    entry.async_on_unload(
        status.async_add_listener(
            lambda: _async_check_app_endpoint(hass, entry, status.data, reload=True)
        )
    )
    bridge_device = DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}-bridge")},
        name="Tesla Bridge app",
        manufacturer="Tesla Bridge",
        model="Fleet Telemetry server",
        sw_version=(status.data or {}).get("version"),
        entry_type=dr.DeviceEntryType.SERVICE,
    )
    bridge = BridgeRuntime(client=client, hub=hub, status=status, device=bridge_device)

    # ---- products -----------------------------------------------------------
    vehicles: list[VehicleData] = []
    energy_sites: list[EnergySiteData] = []
    local_config: dict[str, Any] | None = entry.data.get(CONF_POWERWALL)
    if not local_config:
        # Local updates were turned off (Reconfigure): the password no longer matters.
        ir.async_delete_issue(hass, DOMAIN, "powerwall_auth")
    local_used = False
    device_registry = dr.async_get(hass)

    for product in products:
        if "vin" in product and Scope.VEHICLE_DEVICE_DATA in scopes:
            vin: str = product["vin"]
            signing = product.get("command_signing") == "required"
            api: VehicleFleet = (
                tesla.vehicles.createSigned(vin)
                if signing
                else tesla.vehicles.createFleet(vin)
            )
            name = product.get("display_name") or vin
            device = DeviceInfo(
                identifiers={(DOMAIN, vin)},
                manufacturer=MANUFACTURER,
                name=name,
                model=api.model,
                serial_number=vin,
            )
            vehicles.append(
                VehicleData(
                    api=api,
                    vin=vin,
                    name=name,
                    signing=signing,
                    device=device,
                    stream=hub.vehicle(vin),
                )
            )
        elif "energy_site_id" in product and Scope.ENERGY_DEVICE_DATA in scopes:
            components = product.get("components") or {}
            if not (components.get("battery") or components.get("solar")):
                continue
            site_id = int(product["energy_site_id"])
            site_api = tesla.energySites.create(site_id)
            info = EnergySiteInfoCoordinator(hass, entry, site_api, product)
            live = EnergySiteLiveCoordinator(hass, entry, site_api)
            await info.async_config_entry_first_refresh()
            local: LocalPowerwallCoordinator | None = None
            din: str | None = None
            if local_config and components.get("battery") and not local_used:
                local, din = await _async_setup_local(hass, entry, local_config)
                local_used = local is not None
            if local is None:
                # Without the gateway, fall back to cloud live data.
                await live.async_config_entry_first_refresh()
            device = DeviceInfo(
                identifiers={(DOMAIN, str(site_id))},
                manufacturer=MANUFACTURER,
                name=product.get("site_name") or "Energy site",
                model="Powerwall" if components.get("battery") else "Solar",
                serial_number=din or str(site_id),
            )
            register_device(device_registry, entry.entry_id, device)
            energy_sites.append(
                EnergySiteData(
                    api=site_api,
                    site_id=site_id,
                    name=product.get("site_name") or "Energy site",
                    device=device,
                    info=info,
                    live=live,
                    local=local,
                    has_battery=bool(components.get("battery")),
                    has_solar=bool(components.get("solar")),
                    gateway_din=din,
                )
            )

    telemetry = TelemetryManager(hass, entry.entry_id, tesla)
    await telemetry.async_load()
    telemetry.seed(entry.data.get(CONF_TELEMETRY_PUSHED) or {})
    entry.runtime_data = TeslaBridgeData(
        tesla=tesla,
        scopes=scopes,
        vehicles=vehicles,
        energy_sites=energy_sites,
        bridge=bridge,
        telemetry=telemetry,
    )

    # Keep the app in sync: which VINs to accept and which key to publish.
    try:
        await client.put_vins([v.vin for v in vehicles])
        await client.put_public_key(tesla.public_pem)
    except BridgeError as err:
        LOGGER.warning("Could not update the Tesla Bridge app: %s", err)

    bridge.stream_task = entry.async_create_background_task(
        hass,
        _async_stream(hass, entry, client, hub),
        name=f"{DOMAIN} stream",
    )

    if entry.options.get(CONF_UPNP):
        from .upnp import UpnpPortMapper

        mapper = UpnpPortMapper(hass, port_mappings(entry.data))
        entry.runtime_data.upnp = mapper
        entry.async_create_background_task(hass, mapper.async_start(), f"{DOMAIN} upnp")
        entry.async_on_unload(mapper.async_stop)

    entry.async_create_background_task(
        hass, async_reconcile_telemetry(hass, entry), name=f"{DOMAIN} telemetry"
    )

    async def _periodic(_now: dt.datetime) -> None:
        await async_reconcile_telemetry(hass, entry)

    async def _retry_unpaired(_now: dt.datetime) -> None:
        await async_retry_unpaired(hass, entry)

    entry.async_on_unload(
        async_track_time_interval(hass, _periodic, RECONCILE_INTERVAL)
    )
    entry.async_on_unload(
        async_track_time_interval(hass, _retry_unpaired, UNPAIRED_RETRY_INTERVAL)
    )
    if entry.data.get(CONF_ADOPT) and not entry.data.get(CONF_ADOPTED):
        await _async_adopt(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_adopt(hass: HomeAssistant, entry: TeslaBridgeConfigEntry) -> None:
    """One-time takeover of Tesla Fleet / Tesla Powerwall entities (keeps history)."""
    from homeassistant.components import persistent_notification

    from .migration import async_adopt_entities

    data = entry.runtime_data
    sites = [
        (
            site.site_id,
            list(site.local.data.batteries) if site.local and site.local.data else [],
        )
        for site in data.energy_sites
    ]
    result = await async_adopt_entities(
        hass, entry, [v.vin for v in data.vehicles], sites
    )
    # Persist so a later reload does not run this twice.
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_ADOPTED: True}
    )
    persistent_notification.async_create(
        hass,
        f"Tesla Bridge took over {len(result.moved)} entities (with their history) "
        f"and disabled: {', '.join(result.disabled_entries) or 'nothing'}. "
        "Delete the old integrations once you're happy with Tesla Bridge.",
        title="Tesla Bridge",
        notification_id=f"{DOMAIN}_adopted",
    )


async def _async_setup_local(
    hass: HomeAssistant, entry: TeslaBridgeConfigEntry, config: dict[str, Any]
) -> tuple[LocalPowerwallCoordinator | None, str | None]:
    """Log in to the gateway; a failure only disables local polling."""
    client = LocalPowerwall(
        hass, config[CONF_POWERWALL_HOST], config[CONF_POWERWALL_PASSWORD]
    )
    try:
        din = await client.async_login()
    except PowerwallAuthError:
        ir.async_create_issue(
            hass,
            DOMAIN,
            "powerwall_auth",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="powerwall_auth",
        )
        await client.async_close()
        return None, None
    except Exception as err:
        LOGGER.warning("Powerwall gateway not reachable locally: %s", err)
        await client.async_close()
        return None, None
    ir.async_delete_issue(hass, DOMAIN, "powerwall_auth")
    interval = int(
        entry.options.get(CONF_POWERWALL_INTERVAL, DEFAULT_POWERWALL_INTERVAL)
    )
    coordinator = LocalPowerwallCoordinator(hass, entry, client, interval)
    await coordinator.async_refresh()
    entry.async_on_unload(client.async_close)
    return coordinator, din


async def _async_stream(
    hass: HomeAssistant,
    entry: TeslaBridgeConfigEntry,
    client: BridgeClient,
    hub: StreamHub,
) -> None:
    attempt = 0
    connections = 0

    def on_connection(connected: bool) -> None:
        nonlocal attempt, connections
        if connected:
            attempt = 0
            connections += 1
            ir.async_delete_issue(hass, DOMAIN, "bridge_unreachable")
            ir.async_delete_issue(hass, DOMAIN, "bridge_auth")
            if connections > 1:
                # The app restarted (new options, ports or version): check
                # what it announces now.
                entry.async_create_background_task(
                    hass, _async_refresh_and_reload(hass, entry), f"{DOMAIN} refresh"
                )
        hub.set_bridge_connected(connected)

    while True:
        try:
            await client.stream_forever(hub.handle, on_connection)
        except BridgeAuthError:
            # Most likely the app was reinstalled or its data reset, so it has a
            # new token. It announces that through the Supervisor a little after
            # its API is up, so keep checking rather than giving up.
            if await _async_refresh_and_reload(hass, entry):
                return
            ir.async_create_issue(
                hass,
                DOMAIN,
                "bridge_auth",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="bridge_auth"
                if entry.data.get(CONF_APP_SLUG)
                else "bridge_auth_manual",
            )
        except BridgeError as err:
            _raise_bridge_issue(hass, str(err))
        await asyncio.sleep(BRIDGE_RETRY[min(attempt, len(BRIDGE_RETRY) - 1)])
        attempt += 1


async def _async_refresh_and_reload(
    hass: HomeAssistant, entry: TeslaBridgeConfigEntry
) -> bool:
    if not await _async_refresh_bridge(hass, entry):
        return False
    hass.config_entries.async_schedule_reload(entry.entry_id)
    return True


async def _async_refresh_bridge(
    hass: HomeAssistant, entry: TeslaBridgeConfigEntry
) -> bool:
    """Update the stored app connection details; True if anything changed.

    The public domain is deliberately left alone: a new DuckDNS name is not
    registered with Tesla yet, so switching to it needs the user (see
    _async_check_app_endpoint).
    """
    from homeassistant.helpers.hassio import is_hassio

    from .app_manager import (
        AppUnavailable,
        async_app_discovery,
        async_app_host_ports,
        async_find_app_slug,
    )

    slug = entry.data.get(CONF_APP_SLUG)
    if not slug or not is_hassio(hass):
        return False
    discovery = await async_app_discovery(hass, slug)
    if not discovery:
        # Reinstalled from another repository or as a local copy: new slug.
        try:
            current = await async_find_app_slug(hass, add_repository=False)
        except AppUnavailable:
            current = None
        if current and current != slug:
            slug = current
            discovery = await async_app_discovery(hass, slug)
    if not discovery:
        return False
    updates: dict[str, Any] = {
        CONF_APP_SLUG: slug,
        CONF_BRIDGE: {
            "host": discovery["host"],
            "port": int(discovery["port"]),
            "token": discovery["token"],
        },
    }
    if discovery.get("telemetry_port"):
        updates[CONF_TELEMETRY_PORT] = int(discovery["telemetry_port"])
    # None means the Supervisor could not say; keep what is stored then.
    if (host_ports := await async_app_host_ports(hass, slug)) is not None:
        updates[CONF_KEY_HOST_PORT], updates[CONF_TELEMETRY_HOST_PORT] = host_ports
        if None in host_ports:
            ir.async_create_issue(
                hass,
                DOMAIN,
                "app_ports_disabled",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="app_ports_disabled",
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, "app_ports_disabled")
    if all(entry.data.get(k) == v for k, v in updates.items()):
        return False
    LOGGER.info("Tesla Bridge app connection details changed; updating")
    hass.config_entries.async_update_entry(entry, data={**entry.data, **updates})
    return True


@callback
def _async_check_app_endpoint(
    hass: HomeAssistant,
    entry: TeslaBridgeConfigEntry,
    status: Mapping[str, Any] | None,
    *,
    reload: bool,
) -> None:
    """Follow the app's public telemetry port; flag a changed DuckDNS name."""
    if not status:
        return
    domain = status.get("domain")
    if domain and domain != entry.data[CONF_PUBLIC_DOMAIN]:
        # Tesla only lets cars stream to a domain registered for the developer
        # app, so the switch has to go through Reconfigure.
        ir.async_create_issue(
            hass,
            DOMAIN,
            "domain_changed",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="domain_changed",
            translation_placeholders={
                "old": entry.data[CONF_PUBLIC_DOMAIN],
                "new": domain,
            },
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, "domain_changed")
    port = status.get("telemetry_port")
    stored = int(entry.data.get(CONF_TELEMETRY_PORT) or DEFAULT_TELEMETRY_PORT)
    if port and int(port) != stored:
        LOGGER.info("The app's public telemetry port is now %s", port)
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_TELEMETRY_PORT: int(port)}
        )
        if reload:
            # Re-creates the UPnP mappings and re-pushes the car configs.
            hass.config_entries.async_schedule_reload(entry.entry_id)


def port_mappings(data: Mapping[str, Any]) -> list[PortMapping]:
    """Router forwards: public port -> the app's port on the Home Assistant host.

    A host port stored as None is turned off in the app's Network settings;
    it gets no mapping rather than a guess that could expose another service.
    """
    from .upnp import PortMapping

    telemetry = int(data.get(CONF_TELEMETRY_PORT) or DEFAULT_TELEMETRY_PORT)
    wanted = (
        (KEY_SERVER_PORT, data.get(CONF_KEY_HOST_PORT, KEY_SERVER_PORT)),
        (telemetry, data.get(CONF_TELEMETRY_HOST_PORT, DEFAULT_TELEMETRY_PORT)),
    )
    return [
        PortMapping(external, int(internal))
        for external, internal in wanted
        if internal is not None
    ]


def _raise_bridge_issue(hass: HomeAssistant, detail: str) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        "bridge_unreachable",
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="bridge_unreachable",
        translation_placeholders={"detail": detail},
    )


def missing_key_issue_id(vin: str) -> str:
    """Issue IDs end up in diagnostics, so they must not contain the VIN."""
    return f"missing_key_{hashlib.sha256(vin.encode()).hexdigest()[:12]}"


async def async_retry_unpaired(
    hass: HomeAssistant, entry: TeslaBridgeConfigEntry
) -> None:
    """Push the config to cars Tesla skipped for a missing key once they are paired.

    Checks pairing with the cheap fleet_status call first, so cars still
    waiting for the user cost nothing more than that.
    """
    data = entry.runtime_data
    waiting = [
        v
        for v in data.vehicles
        if data.telemetry.vehicle_state(v.vin).get("skipped_reason") == "missing_key"
    ]
    if not waiting:
        return
    try:
        status = await waiting[0].api.fleet_status([v.vin for v in waiting])
    except (TeslaFleetError, ClientError, TimeoutError, HomeAssistantError) as err:
        # Tried again in 10 minutes; Tesla or the internet may just be down.
        LOGGER.debug("fleet_status failed: %s", err)
        return
    paired = set((status.get("response") or {}).get("key_paired_vins") or [])
    if any(v.vin in paired for v in waiting):
        await async_reconcile_telemetry(hass, entry)


async def async_reconcile_telemetry(
    hass: HomeAssistant, entry: TeslaBridgeConfigEntry, *, force: bool = False
) -> None:
    """Make sure every car streams to the app with the configured preset."""
    data = entry.runtime_data
    try:
        ca_pem = await data.bridge.client.ca_pem()
    except BridgeError as err:
        LOGGER.warning("Cannot configure streaming; app unavailable: %s", err)
        return
    target = TelemetryTarget(
        hostname=entry.data[CONF_PUBLIC_DOMAIN],
        port=int(entry.data.get(CONF_TELEMETRY_PORT, DEFAULT_TELEMETRY_PORT)),
        ca_pem=ca_pem,
    )
    preset = entry.options.get(CONF_PRESET, DEFAULT_PRESET)
    for vehicle in data.vehicles:
        config = build_config(target, preset)
        result = await data.telemetry.async_push(vehicle, config, force=force)
        issue_id = missing_key_issue_id(vehicle.vin)
        if result.skipped_reason == "missing_key":
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="missing_key",
                translation_placeholders={
                    "name": vehicle.name,
                    "url": f"https://www.tesla.com/_ak/{target.hostname}",
                },
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, issue_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: TeslaBridgeConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
