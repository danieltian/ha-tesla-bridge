"""Private CA and server certificate for the Fleet Telemetry listener.

Vehicles trust exactly the CA PEM sent in their ``fleet_telemetry_config``, so a
long-lived private CA means the server certificate can be renewed at any time
without re-pushing configs to the cars. The CA signs the server certificate
directly (no intermediate), matching Tesla's own test tooling.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import hashlib
import logging
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

_LOGGER = logging.getLogger(__name__)

CA_VALIDITY = dt.timedelta(days=365 * 20)
SERVER_VALIDITY = dt.timedelta(days=365 * 5)
# Renew the server certificate once it has less than this left.
SERVER_RENEW_BEFORE = dt.timedelta(days=180)
KEY_SIZE = 2048


@dataclass(frozen=True, slots=True)
class TelemetryPki:
    """Paths and fingerprints of the telemetry CA and server certificate."""

    ca_cert_path: Path
    server_cert_path: Path
    server_key_path: Path
    ca_pem: str
    ca_sha256: str
    server_not_after: dt.datetime
    hostname: str


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)


def _write_public(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def _new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)


def _key_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _load_cert(path: Path) -> x509.Certificate | None:
    try:
        return x509.load_pem_x509_certificate(path.read_bytes())
    except (FileNotFoundError, ValueError):
        return None


def _load_key(path: Path) -> rsa.RSAPrivateKey | None:
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (FileNotFoundError, ValueError):
        return None
    return key if isinstance(key, rsa.RSAPrivateKey) else None


def _create_ca(
    cert_path: Path, key_path: Path
) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    key = _new_key()
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Tesla Bridge"),
            x509.NameAttribute(
                NameOID.COMMON_NAME,
                f"Tesla Bridge Telemetry CA {os.urandom(4).hex()}",
            ),
        ]
    )
    now = _now()
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + CA_VALIDITY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(ski, critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_private(key_path, _key_pem(key))
    _write_public(cert_path, cert.public_bytes(serialization.Encoding.PEM))
    _LOGGER.info("Created telemetry CA %s", cert.subject.rfc4514_string())
    return cert, key


def _create_server_cert(
    hostname: str,
    ca_cert: x509.Certificate,
    ca_key: rsa.RSAPrivateKey,
    cert_path: Path,
    key_path: Path,
) -> x509.Certificate:
    key = _new_key()
    now = _now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + SERVER_VALIDITY)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    _write_private(key_path, _key_pem(key))
    # Serve leaf + CA so clients that want the full chain get it.
    _write_public(
        cert_path,
        cert.public_bytes(serialization.Encoding.PEM)
        + ca_cert.public_bytes(serialization.Encoding.PEM),
    )
    _LOGGER.info("Issued telemetry server certificate for %s", hostname)
    return cert


def _server_cert_usable(
    cert: x509.Certificate | None, hostname: str, ca_cert: x509.Certificate
) -> bool:
    if cert is None or cert.issuer != ca_cert.subject:
        return False
    if cert.not_valid_after_utc - _now() < SERVER_RENEW_BEFORE:
        return False
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return False
    return hostname in san.value.get_values_for_type(x509.DNSName)


def ensure_pki(directory: Path, hostname: str) -> TelemetryPki:
    """Create or reuse the CA and a server certificate valid for ``hostname``."""
    ca_cert_path = directory / "ca.crt"
    ca_key_path = directory / "ca.key"
    server_cert_path = directory / "server.crt"
    server_key_path = directory / "server.key"

    ca_cert = _load_cert(ca_cert_path)
    ca_key = _load_key(ca_key_path)
    if ca_cert is None or ca_key is None or ca_cert.not_valid_after_utc < _now():
        ca_cert, ca_key = _create_ca(ca_cert_path, ca_key_path)

    server_cert = _load_cert(server_cert_path)
    if _load_key(server_key_path) is None or not _server_cert_usable(
        server_cert, hostname, ca_cert
    ):
        server_cert = _create_server_cert(
            hostname, ca_cert, ca_key, server_cert_path, server_key_path
        )
    assert server_cert is not None

    ca_der = ca_cert.public_bytes(serialization.Encoding.DER)
    return TelemetryPki(
        ca_cert_path=ca_cert_path,
        server_cert_path=server_cert_path,
        server_key_path=server_key_path,
        ca_pem=ca_cert.public_bytes(serialization.Encoding.PEM).decode(),
        ca_sha256=hashlib.sha256(ca_der).hexdigest(),
        server_not_after=server_cert.not_valid_after_utc,
        hostname=hostname,
    )
