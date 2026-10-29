"""Config flow: install the app, sign in to Tesla, register, pair, stream."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping
import logging
import os
from typing import Any

import jwt
from tesla_fleet_api import TeslaFleetApi
from tesla_fleet_api.const import Scope
from tesla_fleet_api.exceptions import (
    InvalidToken,
    LoginRequired,
    OAuthExpired,
    PreconditionFailed,
    TeslaFleetError,
)
import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.hassio import is_hassio
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    QrCodeSelector,
    QrCodeSelectorConfig,
    QrErrorCorrectionLevel,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .app_manager import (
    AppUnavailable,
    async_app_host_ports,
    async_find_app_slug,
    get_app_manager,
)
from .bridge_client import BridgeClient, BridgeError
from .const import (
    APP_REPOSITORY,
    APP_SLUG_SUFFIX,
    CONF_ADOPT,
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
    DEVELOPER_DASHBOARD_URL,
    DOMAIN,
    KEY_FILE,
    KEY_SERVER_PORT,
    LEGACY_TESLA_FLEET_KEY_FILE,
    LOGGER,
    POWERWALL_DOMAIN,
    PUBLIC_KEY_PATH,
    REGION_SERVERS,
    SIGNALS_PER_DOLLAR,
    TESLA_FLEET_DOMAIN,
    TelemetryPreset,
)
from .fields import estimate_signals_per_hour
from .helpers import normalize_duckdns_domain
from .powerwall_local import LocalPowerwall, PowerwallAuthError
from .telemetry_config import (
    PushResult,
    TelemetryTarget,
    build_config,
    config_hash,
    push_config,
)

APP_START_TIMEOUT = 180
CERTIFICATE_TIMEOUT = 600
VIRTUAL_KEY_URL = "https://www.tesla.com/_ak/{domain}"
REDIRECT_URI = "https://my.home-assistant.io/redirect/oauth"
DUCKDNS_URL = "https://www.duckdns.org"

CONF_DUCKDNS_DOMAIN = "duckdns_domain"
CONF_DUCKDNS_TOKEN = "duckdns_token"
CONF_ACCEPT_TERMS = "lets_encrypt_accept_terms"
CONF_EMAIL = "lets_encrypt_email"
CONF_SKIP = "skip"
CONF_HOST = "host"
CONF_PORT = "port"
CONF_TOKEN = "token"

PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


class TeslaBridgeConfigFlow(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Guides the whole setup; each step does as much as it can by itself."""

    DOMAIN = DOMAIN
    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._app_slug: str | None = None
        self._bridge: dict[str, Any] | None = None
        self._public_domain: str | None = None
        self._telemetry_port = DEFAULT_TELEMETRY_PORT
        self._upnp = False
        self._oauth_data: dict[str, Any] = {}
        self._uid: str | None = None
        self._region: str = next(iter(REGION_SERVERS))
        self._vehicles: list[dict[str, Any]] = []
        self._paired: list[str] = []
        self._has_battery = False
        self._preset: str = DEFAULT_PRESET
        self._pushed: dict[str, str] = {}
        self._push_results: list[PushResult] = []
        self._powerwall: dict[str, Any] | None = None
        self._task: asyncio.Task[Any] | None = None
        self._last_error: str | None = None
        self._adopt = False
        self._host_ports: tuple[int | None, int | None] | None = None

    @property
    def logger(self) -> logging.Logger:
        return LOGGER

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return TeslaBridgeOptionsFlow()

    # ---- entry points -------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not is_hassio(self.hass):
            return await self.async_step_manual_bridge()
        return await self.async_step_app()

    async def async_step_hassio(
        self, discovery_info: HassioServiceInfo
    ) -> ConfigFlowResult:
        """The app announced itself (installed by hand, or restarted).

        Once Tesla Bridge is set up, Home Assistant aborts this before it runs
        (single config entry); the entry follows app changes by itself.
        """
        if not discovery_info.slug.endswith(f"_{APP_SLUG_SUFFIX}"):
            return self.async_abort(reason="not_tesla_bridge")
        await self._async_handle_discovery_without_unique_id()
        self._app_slug = discovery_info.slug
        self.context["title_placeholders"] = {"name": discovery_info.name}
        config = discovery_info.config
        if config.get("domain"):
            self._public_domain = config["domain"]
            self._telemetry_port = int(
                config.get("telemetry_port") or DEFAULT_TELEMETRY_PORT
            )
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_app()
        self._set_confirm_only()
        return self.async_show_form(
            step_id="hassio_confirm",
            description_placeholders={"slug": self._app_slug or ""},
        )

    async def async_step_manual_bridge(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Without the Supervisor: connect to a standalone Tesla Bridge container."""
        errors: dict[str, str] = {}
        if user_input is not None:
            bridge = {
                CONF_HOST: user_input[CONF_HOST],
                CONF_PORT: int(user_input[CONF_PORT]),
                CONF_TOKEN: user_input[CONF_TOKEN],
            }
            try:
                status = await self._client(bridge).status()
            except BridgeError:
                errors["base"] = "bridge_unreachable"
            else:
                if not status.get("domain"):
                    errors["base"] = "bridge_no_domain"
                else:
                    self._bridge = bridge
                    self._public_domain = status["domain"]
                    self._telemetry_port = int(status.get("telemetry_port") or 8443)
                    return await self.async_step_network()
        return self.async_show_form(
            step_id="manual_bridge",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Required(CONF_PORT, default=8099): int,
                    vol.Required(CONF_TOKEN): PASSWORD,
                }
            ),
            errors=errors,
        )

    # ---- the app ------------------------------------------------------------

    async def async_step_app(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        from homeassistant.components.hassio import AddonError, AddonState

        try:
            slug = self._app_slug or await async_find_app_slug(
                self.hass, add_repository=True
            )
        except AppUnavailable as err:
            return self.async_abort(
                reason="app_store_error", description_placeholders={"error": str(err)}
            )
        if slug is None:
            return self.async_abort(
                reason="app_not_found",
                description_placeholders={"repository": APP_REPOSITORY},
            )
        self._app_slug = slug
        try:
            info = await get_app_manager(self.hass, slug).async_get_addon_info()
        except AddonError as err:
            return self.async_abort(
                reason="app_store_error", description_placeholders={"error": str(err)}
            )
        if info.state is AddonState.NOT_INSTALLED:
            return await self.async_step_install_app()
        return await self.async_step_configure_app()

    async def async_step_install_app(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        from homeassistant.components.hassio import AddonError

        assert self._app_slug
        manager = get_app_manager(self.hass, self._app_slug)
        if (
            progress := self._progress("install_app", manager.async_install_addon)
        ) is not None:
            return progress
        task, self._task = self._task, None
        try:
            await task
        except AddonError as err:
            self._last_error = str(err)
            return self.async_show_progress_done(next_step_id="app_failed")
        return self.async_show_progress_done(next_step_id="configure_app")

    async def async_step_app_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_abort(
            reason="app_failed",
            description_placeholders={"error": self._last_error or ""},
        )

    async def async_step_configure_app(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        from homeassistant.components.hassio import AddonError

        assert self._app_slug
        manager = get_app_manager(self.hass, self._app_slug)
        try:
            info = await manager.async_get_addon_info()
        except AddonError as err:
            return self.async_abort(
                reason="app_store_error", description_placeholders={"error": str(err)}
            )
        current: dict[str, Any] = dict(info.options)
        errors: dict[str, str] = {}
        error_detail = ""
        if user_input is not None:
            email = (user_input.get(CONF_EMAIL) or "").strip()
            if email:
                try:
                    vol.Email()(email)
                except vol.Invalid:
                    errors[CONF_EMAIL] = "invalid_email"
            try:
                domain = normalize_duckdns_domain(user_input[CONF_DUCKDNS_DOMAIN])
            except ValueError:
                errors[CONF_DUCKDNS_DOMAIN] = "invalid_domain"
            if not user_input.get(CONF_ACCEPT_TERMS):
                errors[CONF_ACCEPT_TERMS] = "terms_required"
            if not errors:
                options = {
                    **current,
                    CONF_DUCKDNS_DOMAIN: domain,
                    CONF_DUCKDNS_TOKEN: user_input[CONF_DUCKDNS_TOKEN].strip(),
                    CONF_ACCEPT_TERMS: True,
                }
                if email:
                    options[CONF_EMAIL] = email
                else:
                    options.pop(CONF_EMAIL, None)
                try:
                    await manager.async_set_addon_options(options)
                except AddonError as err:
                    errors["base"] = "app_options_rejected"
                    error_detail = str(err)
                else:
                    self._public_domain = domain
                    return await self.async_step_start_app()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DUCKDNS_DOMAIN,
                    default=current.get(CONF_DUCKDNS_DOMAIN) or vol.UNDEFINED,
                ): str,
                vol.Required(
                    CONF_DUCKDNS_TOKEN,
                    default=current.get(CONF_DUCKDNS_TOKEN) or vol.UNDEFINED,
                ): PASSWORD,
                vol.Required(
                    CONF_ACCEPT_TERMS, default=bool(current.get(CONF_ACCEPT_TERMS))
                ): BooleanSelector(),
                # A suggestion, not a default: a cleared field must stay cleared.
                vol.Optional(
                    CONF_EMAIL, description={"suggested_value": current.get(CONF_EMAIL)}
                ): str,
            }
        )
        return self.async_show_form(
            step_id="configure_app",
            data_schema=schema,
            errors=errors,
            description_placeholders={"duckdns": DUCKDNS_URL, "error": error_detail},
        )

    async def async_step_start_app(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if (progress := self._progress("start_app", self._async_start_app)) is not None:
            return progress
        task, self._task = self._task, None
        try:
            self._bridge = await task
        except Exception as err:
            self._last_error = str(err) or type(err).__name__
            return self.async_show_progress_done(next_step_id="app_failed")
        return self.async_show_progress_done(next_step_id="network")

    async def _async_start_app(self) -> dict[str, Any]:
        """(Re)start the app and wait until it announces the expected domain."""
        from homeassistant.components.hassio import AddonError, AddonState

        assert self._app_slug
        manager = get_app_manager(self.hass, self._app_slug)
        info = await manager.async_get_addon_info()
        if info.state is AddonState.RUNNING:
            await manager.async_restart_addon()
        else:
            await manager.async_start_addon()
        deadline = asyncio.get_running_loop().time() + APP_START_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(3)
            try:
                config = await manager.async_get_addon_discovery_info()
            except AddonError:
                continue
            bridge = {
                CONF_HOST: config[CONF_HOST],
                CONF_PORT: int(config[CONF_PORT]),
                CONF_TOKEN: config[CONF_TOKEN],
            }
            try:
                status = await self._client(bridge).status()
            except BridgeError:
                continue
            if status.get("domain") == self._public_domain:
                self._telemetry_port = int(
                    status.get("telemetry_port") or DEFAULT_TELEMETRY_PORT
                )
                return bridge
        raise TimeoutError(
            "the Tesla Bridge app did not come up in time; check its log"
        )

    # ---- network --------------------------------------------------------------

    async def async_step_network(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Port forwarding: show exact instructions or let UPnP do it."""
        from homeassistant.components import network

        from . import port_mappings

        if self._app_slug and self._host_ports is None:
            self._host_ports = await async_app_host_ports(self.hass, self._app_slug)
        key_host, telemetry_host = self._host_ports or (KEY_SERVER_PORT, 8443)
        if key_host is None or telemetry_host is None:
            return self.async_abort(reason="app_ports_disabled")
        mappings = port_mappings(self._port_data())
        errors: dict[str, str] = {}
        placeholders = {
            "domain": self._public_domain or "",
            "ip": await network.async_get_source_ip(self.hass)
            or "your Home Assistant IP",
            "key_port": str(mappings[0].external_port),
            "key_host_port": str(mappings[0].internal_port),
            "telemetry_port": str(mappings[1].external_port),
            "telemetry_host_port": str(mappings[1].internal_port),
            "detail": "",
        }
        if user_input is not None:
            self._upnp = bool(user_input.get(CONF_UPNP))
            if self._upnp:
                from .upnp import UpnpPortMapper, is_cgnat

                mapper = UpnpPortMapper(self.hass, mappings)
                status = await mapper.async_refresh()
                if status.error:
                    errors["base"] = "upnp_failed"
                    placeholders["detail"] = status.error
                elif is_cgnat(status.external_ip):
                    errors["base"] = "cgnat"
                    placeholders["detail"] = status.external_ip or ""
            if not errors:
                return await self.async_step_certificate()
        return self.async_show_form(
            step_id="network",
            data_schema=vol.Schema(
                {vol.Optional(CONF_UPNP, default=self._upnp): BooleanSelector()}
            ),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_certificate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait until the app has a Let's Encrypt certificate for the key server."""
        if (
            progress := self._progress(
                "certificate",
                self._async_wait_for_certificate,
                {"domain": self._public_domain or ""},
            )
        ) is not None:
            return progress
        task, self._task = self._task, None
        try:
            await task
        except (TimeoutError, BridgeError) as err:
            self._last_error = str(err)
            return self.async_show_progress_done(next_step_id="certificate_failed")
        return self.async_show_progress_done(next_step_id="credentials")

    async def _async_wait_for_certificate(self) -> None:
        assert self._bridge
        client = self._client(self._bridge)
        deadline = asyncio.get_running_loop().time() + CERTIFICATE_TIMEOUT
        last_error = "no certificate yet"
        while asyncio.get_running_loop().time() < deadline:
            status = await client.status()
            key_server = status.get("key_server") or {}
            if key_server.get("listening") and key_server.get("certificate"):
                return
            last_error = key_server.get("last_error") or last_error
            await asyncio.sleep(10)
        raise TimeoutError(last_error)

    async def async_step_certificate_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_certificate()
        return self.async_show_form(
            step_id="certificate_failed",
            description_placeholders={"error": self._last_error or ""},
        )

    # ---- Tesla credentials and sign-in ----------------------------------------

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reuse Tesla Fleet's developer app and key so the cars stay paired."""
        await self._async_import_tesla_fleet_credentials()
        await self.hass.async_add_executor_job(self._copy_legacy_key)
        return await self.async_step_origin()

    async def async_step_origin(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_pick_implementation()
        return self.async_show_form(
            step_id="origin",
            description_placeholders={
                "origin": f"https://{self._public_domain}",
                "redirect": REDIRECT_URI,
                "dashboard": DEVELOPER_DASHBOARD_URL,
            },
        )

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        claims = jwt.decode(
            data["token"]["access_token"], options={"verify_signature": False}
        )
        self._oauth_data = data
        self._uid = claims["sub"]
        await self.async_set_unique_id(self._uid)
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="reauth_account_mismatch")
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                data_updates={
                    "token": data["token"],
                    "auth_implementation": data["auth_implementation"],
                },
            )
        self._abort_if_unique_id_configured()
        region = str(claims.get("ou_code", "")).lower()
        if region in REGION_SERVERS:
            self._region = region
        return await self.async_step_register()

    async def async_step_register(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Publish the key through the app and register the domain with Tesla."""
        assert self._bridge and self._public_domain
        error, detail = await self._async_register_domain(
            self.flow_impl, self._region, self._public_domain, self._bridge
        )
        if error is None:
            return await self.async_step_vehicles()
        if error == "partner_auth_failed":
            return self.async_abort(reason=error)
        return self.async_show_form(
            step_id="register",
            errors={"base": error},
            description_placeholders={
                "key_url": f"https://{self._public_domain}{PUBLIC_KEY_PATH}",
                "origin": f"https://{self._public_domain}",
                "error": detail,
            },
        )

    async def _async_register_domain(
        self,
        implementation: Any,
        region: str,
        domain: str,
        bridge: dict[str, Any],
    ) -> tuple[str | None, str]:
        """Publish the key through the app and register ``domain`` with Tesla.

        Returns (error key, detail); the error is None on success.
        """
        api = TeslaFleetApi(
            session=async_get_clientsession(self.hass),
            access_token="",
            server=REGION_SERVERS[region],
            partner_scope=True,
            charging_scope=False,
            energy_scope=False,
            user_scope=False,
            vehicle_scope=False,
        )
        await api.get_private_key(self.hass.config.path(KEY_FILE))
        try:
            await self._client(bridge).put_public_key(api.public_pem)
            await api.partner_login(
                implementation.client_id,
                implementation.client_secret,
                [Scope.OPENID],
            )
            response = await api.partner.register(domain)
        except BridgeError as err:
            return "bridge_unreachable", str(err)
        except PreconditionFailed:
            return "origin_not_allowed", ""
        except InvalidToken, OAuthExpired, LoginRequired:
            # Not "oauth_error": core translates that one generically.
            return "partner_auth_failed", ""
        except TeslaFleetError as err:
            return "register_failed", err.message
        registered = (response.get("response") or {}).get("public_key", "")
        if registered.lower() != api.public_uncompressed_point.lower():
            return "public_key_mismatch", ""
        return None, ""

    # ---- vehicles -----------------------------------------------------------

    def _user_api(self) -> TeslaFleetApi:
        return TeslaFleetApi(
            session=async_get_clientsession(self.hass),
            access_token=self._oauth_data["token"]["access_token"],
            server=REGION_SERVERS[self._region],
            charging_scope=False,
            partner_scope=False,
        )

    async def async_step_vehicles(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Make sure each car accepts our key (virtual key paired)."""
        assert self._public_domain
        api = self._user_api()
        errors: dict[str, str] = {}
        try:
            if not self._vehicles:
                products = (await api.products())["response"]
                self._vehicles = [p for p in products if "vin" in p]
                self._has_battery = any(
                    "energy_site_id" in p and (p.get("components") or {}).get("battery")
                    for p in products
                )
            vins = [v["vin"] for v in self._vehicles]
            if vins:
                status = (await api.vehicles.createFleet(vins[0]).fleet_status(vins))[
                    "response"
                ]
                self._paired = list(status.get("key_paired_vins") or [])
        except TeslaFleetError as err:
            self._last_error = err.message
            return self.async_abort(
                reason="cannot_connect", description_placeholders={"error": err.message}
            )
        unpaired = [v for v in self._vehicles if v["vin"] not in self._paired]
        if not unpaired or (user_input or {}).get(CONF_SKIP):
            return await self.async_step_telemetry()
        if user_input is not None:
            errors["base"] = "still_unpaired"
        url = VIRTUAL_KEY_URL.format(domain=self._public_domain)
        return self.async_show_form(
            step_id="vehicles",
            data_schema=vol.Schema(
                {
                    vol.Optional("qr_code"): QrCodeSelector(
                        QrCodeSelectorConfig(
                            data=url,
                            scale=6,
                            error_correction_level=QrErrorCorrectionLevel.QUARTILE,
                        )
                    ),
                    vol.Optional(CONF_SKIP, default=False): BooleanSelector(),
                }
            ),
            errors=errors,
            description_placeholders={
                "url": url,
                "cars": ", ".join(v.get("display_name") or v["vin"] for v in unpaired),
            },
        )

    async def async_step_telemetry(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not self._paired:
            return await self.async_step_powerwall()
        if user_input is not None:
            self._preset = user_input[CONF_PRESET]
            return await self.async_step_push()
        estimates = {
            f"{p.value}_signals": f"{estimate_signals_per_hour(p):,}"
            for p in TelemetryPreset
        }
        estimates.update(
            {
                f"{p.value}_cost": f"{estimate_signals_per_hour(p) / SIGNALS_PER_DOLLAR:.3f}"
                for p in TelemetryPreset
            }
        )
        return self.async_show_form(
            step_id="telemetry",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PRESET, default=self._preset): SelectSelector(
                        SelectSelectorConfig(
                            options=[p.value for p in TelemetryPreset],
                            translation_key="preset",
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            description_placeholders=estimates,
        )

    async def async_step_push(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if (progress := self._progress("push", self._async_push_all)) is not None:
            return progress
        task, self._task = self._task, None
        try:
            self._push_results = await task
        except (BridgeError, TeslaFleetError) as err:
            self._last_error = (
                err.message if isinstance(err, TeslaFleetError) else str(err)
            )
            self._push_results = []
        return self.async_show_progress_done(next_step_id="push_result")

    async def _async_push_all(self) -> list[PushResult]:
        assert self._bridge and self._public_domain
        ca_pem = await self._client(self._bridge).ca_pem()
        target = TelemetryTarget(self._public_domain, self._telemetry_port, ca_pem)
        config = build_config(target, self._preset)
        api = self._user_api()
        await api.get_private_key(self.hass.config.path(KEY_FILE))
        assert api.private_key is not None
        results = []
        for vin in self._paired:
            result = await push_config(
                api.vehicles.createFleet(vin), vin, api.private_key, config
            )
            if result.ok:
                self._pushed[vin] = config_hash(config)
            results.append(result)
        return results

    async def async_step_push_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_powerwall()
        names = {v["vin"]: v.get("display_name") or v["vin"] for v in self._vehicles}
        lines = []
        for result in self._push_results:
            if result.ok:
                lines.append(f"- {names.get(result.vin, result.vin)}: configured")
            else:
                lines.append(
                    f"- {names.get(result.vin, result.vin)}: "
                    f"{result.skipped_reason or result.error or 'failed'}"
                )
        if not lines and self._last_error:
            lines.append(f"- {self._last_error}")
        return self.async_show_form(
            step_id="push_result",
            description_placeholders={"results": "\n".join(lines)},
        )

    # ---- Powerwall --------------------------------------------------------------

    async def async_step_powerwall(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Local gateway access for fast Powerwall updates (optional)."""
        if not self._has_battery:
            return await self.async_step_finish()
        errors: dict[str, str] = {}
        defaults: dict[str, Any] = {}
        for entry in self.hass.config_entries.async_entries(POWERWALL_DOMAIN):
            defaults = {
                CONF_POWERWALL_HOST: entry.data.get("ip_address"),
                CONF_POWERWALL_PASSWORD: entry.data.get("password"),
            }
            break
        if user_input is not None:
            if user_input.get(CONF_SKIP):
                return await self.async_step_finish()
            powerwall, errors = await self._async_validate_powerwall(user_input)
            if powerwall is not None:
                self._powerwall = powerwall
                return await self.async_step_finish()
        return self.async_show_form(
            step_id="powerwall",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_POWERWALL_HOST,
                        default=defaults.get(CONF_POWERWALL_HOST) or vol.UNDEFINED,
                    ): str,
                    vol.Optional(
                        CONF_POWERWALL_PASSWORD,
                        default=defaults.get(CONF_POWERWALL_PASSWORD) or vol.UNDEFINED,
                    ): PASSWORD,
                    vol.Optional(CONF_SKIP, default=False): BooleanSelector(),
                }
            ),
            errors=errors,
            description_placeholders={"imported": "yes" if defaults else "no"},
        )

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        from .migration import old_integrations

        if old_integrations(self.hass):
            return await self.async_step_adopt()
        return await self._async_create()

    async def async_step_adopt(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer to take over the Tesla Fleet / Tesla Powerwall entities."""
        from .migration import old_integrations

        if user_input is not None:
            self._adopt = bool(user_input.get(CONF_ADOPT))
            return await self._async_create()
        names = sorted({e.title or e.domain for e in old_integrations(self.hass)})
        return self.async_show_form(
            step_id="adopt",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADOPT, default=True): BooleanSelector()}
            ),
            description_placeholders={"integrations": ", ".join(names)},
        )

    async def _async_create(self) -> ConfigFlowResult:
        assert self._bridge and self._public_domain
        data = {
            **self._oauth_data,
            CONF_REGION: self._region,
            CONF_PUBLIC_DOMAIN: self._public_domain,
            CONF_TELEMETRY_PORT: self._telemetry_port,
            CONF_BRIDGE: self._bridge,
            CONF_APP_SLUG: self._app_slug,
            CONF_POWERWALL: self._powerwall,
            CONF_TELEMETRY_PUSHED: self._pushed,
            **self._port_data(),
            CONF_ADOPT: self._adopt,
        }
        options = {
            CONF_PRESET: self._preset,
            CONF_UPNP: self._upnp,
            CONF_POWERWALL_INTERVAL: DEFAULT_POWERWALL_INTERVAL,
        }
        return self.async_create_entry(
            title=self._public_domain, data=data, options=options
        )

    # ---- reauth ---------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_pick_implementation()

    # ---- reconfigure ------------------------------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick what to change; straight to the Powerwall when that is all."""
        entry = self._get_reconfigure_entry()
        options = ["reconfigure_powerwall"]
        if not entry.data.get(CONF_APP_SLUG):
            options.append("reconfigure_bridge")
        if self._app_domain(entry) not in (None, entry.data[CONF_PUBLIC_DOMAIN]):
            options.insert(0, "reconfigure_domain")
        if len(options) == 1:
            return await self.async_step_reconfigure_powerwall()
        return self.async_show_menu(step_id="reconfigure", menu_options=options)

    async def async_step_reconfigure_domain(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The app's DuckDNS name changed: register the new one with Tesla."""
        entry = self._get_reconfigure_entry()
        domain = self._app_domain(entry)
        if not domain:
            return self.async_abort(reason="bridge_unreachable")
        errors: dict[str, str] = {}
        placeholders = {
            "old": entry.data[CONF_PUBLIC_DOMAIN],
            "origin": f"https://{domain}",
            "key_url": f"https://{domain}{PUBLIC_KEY_PATH}",
            "dashboard": DEVELOPER_DASHBOARD_URL,
            "error": "",
        }
        if user_input is not None:
            implementation = (
                await config_entry_oauth2_flow.async_get_config_entry_implementation(
                    self.hass, entry
                )
            )
            error, placeholders["error"] = await self._async_register_domain(
                implementation,
                entry.data.get(CONF_REGION) or next(iter(REGION_SERVERS)),
                domain,
                entry.data[CONF_BRIDGE],
            )
            if error is None:
                # The reload pushes every car a config for the new name.
                return self.async_update_reload_and_abort(
                    entry, title=domain, data_updates={CONF_PUBLIC_DOMAIN: domain}
                )
            if error == "partner_auth_failed":
                return self.async_abort(reason=error)
            errors["base"] = error
        return self.async_show_form(
            step_id="reconfigure_domain",
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_reconfigure_bridge(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Without the Supervisor: where the Tesla Bridge container is now."""
        entry = self._get_reconfigure_entry()
        current: dict[str, Any] = entry.data.get(CONF_BRIDGE) or {}
        errors: dict[str, str] = {}
        if user_input is not None:
            bridge = {
                CONF_HOST: user_input[CONF_HOST],
                CONF_PORT: int(user_input[CONF_PORT]),
                CONF_TOKEN: user_input[CONF_TOKEN],
            }
            try:
                await self._client(bridge).status()
            except BridgeError:
                errors["base"] = "bridge_unreachable"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_BRIDGE: bridge}
                )
        return self.async_show_form(
            step_id="reconfigure_bridge",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=current.get(CONF_HOST)): str,
                    vol.Required(
                        CONF_PORT, default=int(current.get(CONF_PORT) or 8099)
                    ): int,
                    vol.Required(CONF_TOKEN): PASSWORD,
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure_powerwall(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the Powerwall gateway address/password (or turn local polling off)."""
        entry = self._get_reconfigure_entry()
        current: dict[str, Any] = entry.data.get(CONF_POWERWALL) or {}
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(CONF_SKIP):
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_POWERWALL: None}
                )
            powerwall, errors = await self._async_validate_powerwall(user_input)
            if powerwall is not None:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_POWERWALL: powerwall}
                )
        return self.async_show_form(
            step_id="reconfigure_powerwall",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_POWERWALL_HOST,
                        default=current.get(CONF_POWERWALL_HOST) or vol.UNDEFINED,
                    ): str,
                    vol.Optional(CONF_POWERWALL_PASSWORD): PASSWORD,
                    vol.Optional(CONF_SKIP, default=False): BooleanSelector(),
                }
            ),
            errors=errors,
        )

    # ---- helpers --------------------------------------------------------------

    async def _async_validate_powerwall(
        self, user_input: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, str]]:
        """Log in to the gateway; returns (config, errors)."""
        host = (user_input.get(CONF_POWERWALL_HOST) or "").strip()
        password = user_input.get(CONF_POWERWALL_PASSWORD) or ""
        if not host:
            return None, {CONF_POWERWALL_HOST: "required"}
        if not password:
            return None, {CONF_POWERWALL_PASSWORD: "required"}
        try:
            client = LocalPowerwall(self.hass, host, password)
        except ValueError:
            return None, {CONF_POWERWALL_HOST: "invalid_host"}
        try:
            await client.async_login()
        except PowerwallAuthError:
            return None, {"base": "invalid_auth"}
        except Exception:
            return None, {"base": "powerwall_unreachable"}
        finally:
            await client.async_close()
        return {CONF_POWERWALL_HOST: host, CONF_POWERWALL_PASSWORD: password}, {}

    @staticmethod
    def _app_domain(entry: ConfigEntry) -> str | None:
        """The DuckDNS name the running app uses now, if it is known."""
        runtime = getattr(entry, "runtime_data", None)
        if runtime is None:
            return None
        return (runtime.bridge.status.data or {}).get("domain")

    def _port_data(self) -> dict[str, Any]:
        key_host, telemetry_host = self._host_ports or (KEY_SERVER_PORT, 8443)
        return {
            CONF_TELEMETRY_PORT: self._telemetry_port,
            CONF_KEY_HOST_PORT: key_host,
            CONF_TELEMETRY_HOST_PORT: telemetry_host,
        }

    def _progress(
        self,
        step_id: str,
        start: Callable[[], Coroutine[Any, Any, Any]],
        placeholders: dict[str, str] | None = None,
    ) -> ConfigFlowResult | None:
        """Run ``start()`` in the background; a progress result while it runs.

        Progress is always shown on the first call, even if the task finished
        instantly: otherwise Home Assistant would pass this call's form input on
        to the next step.
        """
        if self._task is None or not self._task.done():
            if self._task is None:
                self._task = self.hass.async_create_task(start())
            return self.async_show_progress(
                step_id=step_id,
                progress_action=step_id,
                progress_task=self._task,
                description_placeholders=placeholders,
            )
        return None

    def _client(self, bridge: dict[str, Any]) -> BridgeClient:
        return BridgeClient(
            async_get_clientsession(self.hass),
            bridge[CONF_HOST],
            int(bridge[CONF_PORT]),
            bridge[CONF_TOKEN],
        )

    async def _async_import_tesla_fleet_credentials(self) -> None:
        """Copy Tesla Fleet's client ID/secret so the user does not retype them."""
        from homeassistant.components import application_credentials as ac

        try:
            storage = self.hass.data[ac.DATA_COMPONENT]
        except KeyError:
            return
        if storage.async_client_credentials(DOMAIN):
            return
        for credential in storage.async_client_credentials(TESLA_FLEET_DOMAIN).values():
            await ac.async_import_client_credential(
                self.hass,
                DOMAIN,
                ac.ClientCredential(
                    credential.client_id,
                    credential.client_secret,
                    name="Tesla (imported from Tesla Fleet)",
                ),
            )
            LOGGER.info("Imported Tesla developer app credentials from Tesla Fleet")
            return

    def _copy_legacy_key(self) -> None:
        """Reuse tesla_fleet.key: the cars already trust it (virtual key)."""
        ours = self.hass.config.path(KEY_FILE)
        legacy = self.hass.config.path(LEGACY_TESLA_FLEET_KEY_FILE)
        if os.path.exists(ours) or not os.path.exists(legacy):
            return
        with open(legacy, "rb") as src:
            key = src.read()
        # Created owner-only from the start; never briefly world-readable.
        fd = os.open(ours, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as dst:
            dst.write(key)
        LOGGER.info("Reusing the Tesla Fleet partner key")


class TeslaBridgeOptionsFlow(OptionsFlowWithReload):
    """Streaming preset, UPnP and local polling interval."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_PRESET, default=options.get(CONF_PRESET, DEFAULT_PRESET)
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[p.value for p in TelemetryPreset],
                        translation_key="preset",
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(
                    CONF_UPNP, default=bool(options.get(CONF_UPNP, False))
                ): BooleanSelector(),
                vol.Required(
                    CONF_POWERWALL_INTERVAL,
                    default=options.get(
                        CONF_POWERWALL_INTERVAL, DEFAULT_POWERWALL_INTERVAL
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=2,
                        max=300,
                        step=1,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
