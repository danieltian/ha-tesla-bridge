"""PKI, options, DuckDNS parsing, fleet-telemetry config rendering."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
import subprocess

from cryptography import x509
import pytest

from tesla_bridge import pki
from tesla_bridge.duckdns import DuckDNSError, parse_response
from tesla_bridge.options import Options, OptionsError, normalize_duckdns_domain
from tesla_bridge.telemetry import log_server_line, render_config


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("myhome", "myhome.duckdns.org"),
        ("Myhome.DuckDNS.org", "myhome.duckdns.org"),
        ("https://myhome.duckdns.org/", "myhome.duckdns.org"),
        ("myhome.duckdns.org:443", "myhome.duckdns.org"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_domain(raw: str | None, expected: str | None) -> None:
    assert normalize_duckdns_domain(raw) == expected


@pytest.mark.parametrize("raw", ["a.b.duckdns.org", "bad_name", "-x"])
def test_normalize_domain_rejects(raw: str) -> None:
    with pytest.raises(OptionsError):
        normalize_duckdns_domain(raw)


def test_options_defaults() -> None:
    opts = Options.from_dict(
        {"duckdns_domain": "myhome", "duckdns_token": " t ", "log_level": "LOUD"}
    )
    assert opts.domain == "myhome.duckdns.org"
    assert opts.subdomain == "myhome"
    assert opts.duckdns_token == "t"
    assert opts.log_level == "info"
    assert opts.can_update_dns
    assert not opts.can_issue_certificate
    assert opts.external_telemetry_port == 8443


def test_pki_create_and_reuse(tmp_path: Path) -> None:
    first = pki.ensure_pki(tmp_path, "myhome.duckdns.org")
    again = pki.ensure_pki(tmp_path, "myhome.duckdns.org")
    assert first.ca_sha256 == again.ca_sha256
    assert first.server_not_after == again.server_not_after

    leaf = x509.load_pem_x509_certificate(first.server_cert_path.read_bytes())
    sans = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert sans.value.get_values_for_type(x509.DNSName) == ["myhome.duckdns.org"]
    assert first.server_key_path.stat().st_mode & 0o777 == 0o600

    # The chain must verify against exactly the CA we hand to the vehicles.
    leaf_only = tmp_path / "leaf.pem"
    leaf_only.write_bytes(
        first.server_cert_path.read_bytes().split(b"-----END CERTIFICATE-----")[0]
        + b"-----END CERTIFICATE-----\n"
    )
    ca_file = tmp_path / "vehicle-ca.pem"
    ca_file.write_text(first.ca_pem)
    result = subprocess.run(
        ["openssl", "verify", "-CAfile", str(ca_file), str(leaf_only)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_pki_renews_server_cert_keeps_ca(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = pki.ensure_pki(tmp_path, "myhome.duckdns.org")
    later = dt.datetime.now(dt.UTC) + pki.SERVER_VALIDITY - dt.timedelta(days=30)
    monkeypatch.setattr(pki, "_now", lambda: later)
    renewed = pki.ensure_pki(tmp_path, "myhome.duckdns.org")
    assert renewed.ca_sha256 == first.ca_sha256
    assert renewed.server_not_after > first.server_not_after


def test_pki_hostname_change_reissues(tmp_path: Path) -> None:
    first = pki.ensure_pki(tmp_path, "one.duckdns.org")
    second = pki.ensure_pki(tmp_path, "two.duckdns.org")
    assert first.ca_sha256 == second.ca_sha256
    assert second.hostname == "two.duckdns.org"


def test_duckdns_parse() -> None:
    assert parse_response("OK\n1.2.3.4\n\nUPDATED") == ("1.2.3.4", True)
    assert parse_response("OK\n1.2.3.4\n\nNOCHANGE") == ("1.2.3.4", False)
    with pytest.raises(DuckDNSError):
        parse_response("KO")


def test_render_config(tmp_path: Path) -> None:
    keys = pki.ensure_pki(tmp_path, "myhome.duckdns.org")
    config = render_config(keys, extra_vehicle_ca="/share/sim-ca.pem")
    assert config["port"] == 8443
    assert config["transmit_decoded_records"] is True
    assert config["records"]["V"] == ["zmq"]
    assert config["zmq"]["addr"] == "tcp://127.0.0.1:5284"  # loopback only
    assert config["tls"]["ca_file"] == "/share/sim-ca.pem"
    assert "ca_file" not in render_config(keys)["tls"]


def test_log_server_line(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="tesla_bridge.telemetry"):
        log_server_line('{"level":"error","msg":"boom","vin":"X","time":"t"}')
        log_server_line("plain text")
    assert caplog.records[0].levelno == logging.ERROR
    assert "boom vin=X" in caplog.records[0].getMessage()
    assert "plain text" in caplog.records[1].getMessage()
