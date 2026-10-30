"""Diagnostics download (with secrets and personal data redacted).

Redacting by key name is not enough here: VINs are used as dictionary keys and
IPs, the DuckDNS name and serials appear inside free-text status messages. So
after the usual redaction by key, every known identifier is also scrubbed from
all strings, and keys that are identifiers are replaced.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import TeslaBridgeConfigEntry
from .const import CONF_PUBLIC_DOMAIN, CONF_TELEMETRY_PUSHED
from .powerwall_local import as_dict as local_as_dict

REDACTED = "**REDACTED**"

TO_REDACT = {
    # credentials
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "password",
    "api_token",
    # identities, addresses, places
    "vin",
    "vins",
    "Location",
    "DestinationLocation",
    "OriginLocation",
    "DestinationName",
    "latitude",
    "longitude",
    "resolved_ip",
    "public_ip",
    "ip",
    "external_ip",
    "internal_ip",
    "router",
    "public_domain",
    "domain",
    "host",
    # devices and sites
    "id",
    "site_id",
    "device_id",
    "site_name",
    "gateway_din",
    "din",
    "gateway_id",
    "asset_site_id",
    "warp_site_number",
    "installation_date",
    "serial_number",
    "energy_site_id",
}


def _identifiers(entry: TeslaBridgeConfigEntry) -> list[str]:
    """Values that identify the user wherever they appear."""
    data = entry.runtime_data
    found: set[str] = {v.vin for v in data.vehicles}
    status = data.bridge.status.data or {}
    # The app's current name can differ from the registered one (see the
    # domain_changed issue), and both show up in status messages.
    for domain in (
        entry.data.get(CONF_PUBLIC_DOMAIN),
        status.get("domain"),
        data.bridge.hub.hello.get("domain"),
    ):
        if domain:
            found |= {domain, domain.split(".", 1)[0]}
    found.add(str((status.get("duckdns") or {}).get("ip") or ""))
    selftest = status.get("selftest") or {}
    found |= {
        str(selftest.get("resolved_ip") or ""),
        str(selftest.get("public_ip") or ""),
    }
    if data.upnp:
        found |= {
            str(data.upnp.status.external_ip or ""),
            str(data.upnp.status.internal_ip or ""),
        }
    for site in data.energy_sites:
        found |= {str(site.site_id), str(site.gateway_din or "")}
        if site.local is not None:
            found.add(site.local.client.host)
    # Longest first so a VIN is not half-replaced by a shorter match.
    return sorted((v for v in found if len(v) >= 4), key=len, reverse=True)


def _scrub(value: Any, identifiers: list[str]) -> Any:
    """Replace identifiers inside strings, and keys that are identifiers.

    Keys are only replaced on an exact match (VIN-keyed dicts), so a DuckDNS
    name like "token" leaves key names alone.
    """
    if isinstance(value, str):
        for ident in identifiers:
            value = value.replace(ident, REDACTED)
        return value
    if isinstance(value, dict):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            name = f"{REDACTED} {len(result) + 1}" if key in identifiers else key
            result[name] = _scrub(item, identifiers)
        return result
    if isinstance(value, (list, tuple)):
        return [_scrub(v, identifiers) for v in value]
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TeslaBridgeConfigEntry
) -> dict[str, Any]:
    data = entry.runtime_data
    status = dict(data.bridge.status.data or {})
    relay = dict(status.get("relay") or {})
    if isinstance(relay.get("vehicles"), dict):
        # Keyed by VIN in the app's status; keep only the values.
        relay["vehicles"] = list(relay["vehicles"].values())
        status["relay"] = relay
    entry_data = {k: v for k, v in entry.data.items() if k != CONF_TELEMETRY_PUSHED}
    vehicles = [
        {
            "signing": v.signing,
            "connected": v.stream.connected,
            "fields": dict(v.stream.values),
            "signals": v.stream.signals,
            "telemetry": data.telemetry.vehicle_state(v.vin),
        }
        for v in data.vehicles
    ]
    sites = [
        {
            "has_battery": s.has_battery,
            "has_solar": s.has_solar,
            "local": local_as_dict(s.local.data) if s.local else None,
            "info": s.info.data,
            "live": s.live.data,
        }
        for s in data.energy_sites
    ]
    report = {
        "entry": {"data": entry_data, "options": dict(entry.options)},
        "scopes": [str(s) for s in data.scopes],
        "bridge": {
            "stream_connected": data.bridge.hub.bridge_connected,
            "hello": data.bridge.hub.hello,
            "status": status,
        },
        "upnp": data.upnp.status.as_dict() if data.upnp else None,
        "api_usage": data.usage.summary(data.vehicles),
        "vehicles": vehicles,
        "energy_sites": sites,
    }
    # Redact by key first: a scrubbed key would no longer match TO_REDACT.
    return _scrub(async_redact_data(report, TO_REDACT), _identifiers(entry))
