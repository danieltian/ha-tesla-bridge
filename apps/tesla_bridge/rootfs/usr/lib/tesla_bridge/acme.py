"""Publicly trusted certificate for the key server on port 443.

Tesla fetches the partner public key over normal HTTPS, so this certificate
must chain to a public root. It is obtained from Let's Encrypt with a DNS-01
challenge through DuckDNS (no inbound port 80 needed). If the official DuckDNS
app already maintains a certificate for the same name in ``/ssl``, that one is
reused instead: two ACME clients would fight over DuckDNS's single TXT record.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import datetime as dt
import logging
import os
from pathlib import Path

from cryptography import x509

from .const import ACME_DIR, LEGO_BIN, SSL_DIR
from .options import Options

_LOGGER = logging.getLogger(__name__)

CHECK_INTERVAL = 12 * 3600.0
RETRY_INTERVAL = 3600.0
LEGO_TIMEOUT = 600.0
# Treat a cert as usable only if it has at least this long left.
MIN_REMAINING = dt.timedelta(days=7)


@dataclass(frozen=True, slots=True)
class CertInfo:
    """Location and expiry of the active key-server certificate."""

    cert_path: Path
    key_path: Path
    not_after: dt.datetime
    source: str  # "acme" | "ssl"

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "not_after": self.not_after.isoformat(),
            "days_left": (self.not_after - dt.datetime.now(dt.UTC)).days,
        }


def cert_matches(cert_path: Path, domain: str) -> dt.datetime | None:
    """Return the expiry if ``cert_path`` is valid for ``domain`` and not near expiry."""
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (OSError, ValueError):
        return None
    try:
        names = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        return None
    wildcard = "*." + domain.split(".", 1)[1] if "." in domain else None
    if domain not in names and (wildcard is None or wildcard not in names):
        return None
    not_after = cert.not_valid_after_utc
    if not_after - dt.datetime.now(dt.UTC) < MIN_REMAINING:
        return None
    return not_after


def find_ssl_share_cert(domain: str, ssl_dir: Path = SSL_DIR) -> CertInfo | None:
    """A certificate for ``domain`` maintained by another app in ``/ssl``."""
    cert_path = ssl_dir / "fullchain.pem"
    key_path = ssl_dir / "privkey.pem"
    if not key_path.exists():
        return None
    not_after = cert_matches(cert_path, domain)
    if not_after is None:
        return None
    return CertInfo(cert_path, key_path, not_after, "ssl")


def find_lego_cert(domain: str, acme_dir: Path = ACME_DIR) -> CertInfo | None:
    """Locate lego's certificate for ``domain`` under its storage path."""
    certs_dir = acme_dir / "certificates"
    candidates = [certs_dir / f"{domain}.crt"]
    if certs_dir.is_dir():
        candidates += sorted(
            p for p in certs_dir.rglob("*.crt") if not p.name.endswith(".issuer.crt")
        )
    for cert_path in candidates:
        key_path = cert_path.with_suffix(".key")
        if not key_path.exists():
            continue
        not_after = cert_matches(cert_path, domain)
        if not_after is not None:
            return CertInfo(cert_path, key_path, not_after, "acme")
    return None


class CertificateManager:
    """Obtains/renews the key-server certificate and reports changes."""

    def __init__(
        self,
        options: Options,
        on_change: Callable[[CertInfo], Awaitable[None]],
        acme_dir: Path = ACME_DIR,
        ssl_dir: Path = SSL_DIR,
    ) -> None:
        self._options = options
        self._on_change = on_change
        self._acme_dir = acme_dir
        self._ssl_dir = ssl_dir
        self.current: CertInfo | None = None
        self.last_error: str | None = None
        self.last_attempt: dt.datetime | None = None

    def status(self) -> dict[str, object]:
        return {
            "certificate": self.current.as_dict() if self.current else None,
            "last_error": self.last_error,
            "last_attempt": self.last_attempt.isoformat()
            if self.last_attempt
            else None,
        }

    async def ensure(self) -> CertInfo | None:
        """Make sure a usable certificate exists; returns it (or None)."""
        domain = self._options.domain
        if not domain:
            return None

        info = find_ssl_share_cert(domain, self._ssl_dir)
        if info is None and self._options.can_issue_certificate:
            await self._run_lego()
            info = find_lego_cert(domain, self._acme_dir)
        elif info is None:
            info = find_lego_cert(domain, self._acme_dir)
            if info is None:
                self.last_error = (
                    "Accept the Let's Encrypt terms and set the DuckDNS token so the "
                    "app can obtain a certificate"
                )

        if info is not None and info != self.current:
            self.current = info
            _LOGGER.info(
                "Key server certificate from %s, valid until %s",
                info.source,
                info.not_after.date(),
            )
            await self._on_change(info)
        return self.current

    async def _run_lego(self) -> None:
        opts = self._options
        assert opts.domain and opts.duckdns_token
        self.last_attempt = dt.datetime.now(dt.UTC)
        self._acme_dir.mkdir(parents=True, exist_ok=True)
        args = [
            LEGO_BIN,
            "run",
            "--accept-tos",
            "--dns",
            "duckdns",
            "--domains",
            opts.domain,
            "--path",
            str(self._acme_dir),
            "--key-type",
            "EC256",
            "--no-random-sleep",
            "--server",
            "letsencrypt-staging" if opts.staging else "letsencrypt",
        ]
        if opts.email:
            args += ["--email", opts.email]
        env = {
            **os.environ,
            "DUCKDNS_TOKEN": opts.duckdns_token,
            # DuckDNS answers TXT updates instantly but its NS can lag a little.
            "DUCKDNS_PROPAGATION_TIMEOUT": "300",
        }
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        try:
            async with asyncio.timeout(LEGO_TIMEOUT):
                output, _ = await proc.communicate()
        except TimeoutError:
            proc.kill()
            await proc.wait()
            self.last_error = "Let's Encrypt request timed out"
            _LOGGER.error(self.last_error)
            return
        text = output.decode(errors="replace").strip()
        # lego's DuckDNS errors include the update URL, and with it the token.
        text = text.replace(opts.duckdns_token, "**REDACTED**")
        for line in text.splitlines():
            _LOGGER.debug("lego: %s", line)
        if proc.returncode == 0:
            self.last_error = None
        else:
            # Keep the most informative line (lego prints the ACME problem last).
            self.last_error = (
                text.splitlines()[-1] if text else f"lego exited {proc.returncode}"
            )
            _LOGGER.error("Let's Encrypt request failed: %s", self.last_error)

    async def run(self) -> None:
        while True:
            try:
                await self.ensure()
            except Exception:
                _LOGGER.exception("Certificate check failed")
            delay = (
                CHECK_INTERVAL
                if self.current and not self.last_error
                else RETRY_INTERVAL
            )
            await asyncio.sleep(delay)
