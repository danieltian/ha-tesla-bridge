"""Paths, ports and fixed names used across the app."""

from __future__ import annotations

import os
from pathlib import Path

# Everything persistent lives under /data (overridable for tests and local runs).
DATA_DIR = Path(os.environ.get("TESLA_BRIDGE_DATA", "/data"))
OPTIONS_FILE = DATA_DIR / "options.json"
STATE_FILE = DATA_DIR / "state.json"
PKI_DIR = DATA_DIR / "pki"
ACME_DIR = DATA_DIR / "acme"
# Must not be named config.json/config.yaml: the Supervisor treats any such file
# in the repository as an app definition.
FLEET_TELEMETRY_CONFIG = DATA_DIR / "fleet-telemetry.json"

SSL_DIR = Path(os.environ.get("TESLA_BRIDGE_SSL", "/ssl"))

FLEET_TELEMETRY_BIN = os.environ.get(
    "TESLA_BRIDGE_FLEET_TELEMETRY_BIN", "/usr/bin/fleet-telemetry"
)
LEGO_BIN = os.environ.get("TESLA_BRIDGE_LEGO_BIN", "/usr/bin/lego")

# Ports inside the container. The Supervisor maps 443/8443 to the host; the API
# port stays on the internal hassio network.
KEY_SERVER_PORT = int(os.environ.get("TESLA_BRIDGE_KEY_PORT", "443"))
TELEMETRY_PORT = int(os.environ.get("TESLA_BRIDGE_TELEMETRY_PORT", "8443"))
TELEMETRY_STATUS_PORT = 8080
API_PORT = int(os.environ.get("TESLA_BRIDGE_API_PORT", "8099"))
# The app's page in Home Assistant, reached only through the ingress proxy.
WEBUI_PORT = int(os.environ.get("TESLA_BRIDGE_WEBUI_PORT", "8098"))
ZMQ_ADDR = "tcp://127.0.0.1:5284"
ZMQ_NAMESPACE = "tb"

PUBLIC_KEY_PATH = "/.well-known/appspecific/com.tesla.3p.public-key.pem"

DISCOVERY_SERVICE = "tesla_bridge"
SUPERVISOR_URL = "http://supervisor"

DUCKDNS_SUFFIX = ".duckdns.org"
DUCKDNS_UPDATE_URL = "https://www.duckdns.org/update"
