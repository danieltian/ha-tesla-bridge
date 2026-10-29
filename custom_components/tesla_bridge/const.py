"""Constants for the Tesla Bridge integration."""

from __future__ import annotations

from enum import StrEnum
import logging

from tesla_fleet_api.const import SERVERS, Scope

DOMAIN = "tesla_bridge"
LOGGER = logging.getLogger(__package__)

MANUFACTURER = "Tesla"

AUTHORIZE_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/authorize"
TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
DEVELOPER_DASHBOARD_URL = "https://developer.tesla.com/dashboard"

SCOPES = [
    Scope.OPENID,
    Scope.OFFLINE_ACCESS,
    Scope.VEHICLE_DEVICE_DATA,
    Scope.VEHICLE_LOCATION,
    Scope.VEHICLE_CMDS,
    Scope.VEHICLE_CHARGING_CMDS,
    Scope.ENERGY_DEVICE_DATA,
    Scope.ENERGY_CMDS,
]

# China uses separate infrastructure that Fleet Telemetry partners cannot reach.
REGION_SERVERS: dict[str, str] = {r: s for r, s in SERVERS.items() if r != "cn"}

# The partner key. Tesla Fleet's key is imported (same key => cars stay paired).
KEY_FILE = "tesla_bridge.key"
LEGACY_TESLA_FLEET_KEY_FILE = "tesla_fleet.key"
TESLA_FLEET_DOMAIN = "tesla_fleet"
POWERWALL_DOMAIN = "powerwall"

# The companion app (formerly "add-on").
APP_NAME = "Tesla Bridge"
APP_SLUG_SUFFIX = "tesla_bridge"
APP_REPOSITORY = "https://github.com/danieltian/ha-tesla-bridge"
BRIDGE_API_VERSION = 1

# Config entry data keys.
CONF_BRIDGE = "bridge"
CONF_PUBLIC_DOMAIN = "public_domain"
CONF_TELEMETRY_PORT = "telemetry_port"
CONF_REGION = "region"
CONF_POWERWALL = "powerwall"
CONF_POWERWALL_HOST = "host"
CONF_POWERWALL_PASSWORD = "password"
CONF_APP_SLUG = "app_slug"
CONF_ADOPT = "adopt"
CONF_ADOPTED = "adopted"
CONF_TELEMETRY_PUSHED = "telemetry_pushed"
# Ports on the Home Assistant host the Supervisor maps to the app's 443/8443.
CONF_KEY_HOST_PORT = "key_host_port"
CONF_TELEMETRY_HOST_PORT = "telemetry_host_port"

# Options.
CONF_PRESET = "telemetry_preset"
CONF_POWERWALL_INTERVAL = "powerwall_interval"
CONF_UPNP = "upnp"
DEFAULT_POWERWALL_INTERVAL = 5
DEFAULT_TELEMETRY_PORT = 8443
KEY_SERVER_PORT = 443

PUBLIC_KEY_PATH = "/.well-known/appspecific/com.tesla.3p.public-key.pem"


class TelemetryPreset(StrEnum):
    """How much the cars stream (and how much it costs)."""

    ESSENTIAL = "essential"
    STANDARD = "standard"
    EVERYTHING = "everything"


DEFAULT_PRESET = TelemetryPreset.STANDARD

# Tesla's monthly discount covers roughly this many streamed signals per
# account ($10 at 150,000 signals per $1); used for the cost estimate sensor.
SIGNALS_PER_DOLLAR = 150_000
MONTHLY_CREDIT_USD = 10.0
