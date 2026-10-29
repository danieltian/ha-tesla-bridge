"""App options as configured in the Supervisor (``/data/options.json``)."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from .const import DUCKDNS_SUFFIX, OPTIONS_FILE, TELEMETRY_PORT

_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class OptionsError(ValueError):
    """Raised for an option value the app cannot use."""


def normalize_duckdns_domain(raw: str | None) -> str | None:
    """Turn 'myhome', 'myhome.duckdns.org' or a URL into 'myhome.duckdns.org'."""
    if not raw:
        return None
    value = raw.strip().lower()
    value = re.sub(r"^[a-z]+://", "", value).split("/", 1)[0].split(":", 1)[0]
    value = value.rstrip(".")
    if not value:
        return None
    if value.endswith(DUCKDNS_SUFFIX):
        value = value[: -len(DUCKDNS_SUFFIX)]
    labels = value.split(".")
    # Only the account subdomain is registered with DuckDNS; deeper labels
    # resolve to the same address but cannot be updated individually.
    if len(labels) != 1 or not _LABEL.match(labels[0]):
        raise OptionsError(f"'{raw}' is not a valid DuckDNS name")
    return f"{labels[0]}{DUCKDNS_SUFFIX}"


@dataclass(frozen=True, slots=True)
class Options:
    """Validated app options."""

    domain: str | None = None
    duckdns_token: str | None = None
    accept_terms: bool = False
    email: str | None = None
    staging: bool = False
    external_telemetry_port: int = TELEMETRY_PORT
    log_level: str = "info"
    dev_extra_vehicle_ca: str | None = None

    @property
    def subdomain(self) -> str | None:
        """The DuckDNS account label, e.g. 'myhome'."""
        return self.domain.split(".", 1)[0] if self.domain else None

    @property
    def can_update_dns(self) -> bool:
        return bool(self.domain and self.duckdns_token)

    @property
    def can_issue_certificate(self) -> bool:
        return self.can_update_dns and self.accept_terms

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Options:
        token = (raw.get("duckdns_token") or "").strip() or None
        email = (raw.get("lets_encrypt_email") or "").strip() or None
        level = str(raw.get("log_level") or "info").lower()
        if level not in {"debug", "info", "warning", "error"}:
            level = "info"
        port = raw.get("external_telemetry_port") or TELEMETRY_PORT
        return cls(
            domain=normalize_duckdns_domain(raw.get("duckdns_domain")),
            duckdns_token=token,
            accept_terms=bool(raw.get("lets_encrypt_accept_terms")),
            email=email,
            staging=bool(raw.get("lets_encrypt_staging")),
            external_telemetry_port=int(port),
            log_level=level,
            dev_extra_vehicle_ca=(raw.get("dev_extra_vehicle_ca") or "").strip()
            or None,
        )

    @classmethod
    def load(cls, path: Path = OPTIONS_FILE) -> Options:
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            return cls()
        return cls.from_dict(raw)
