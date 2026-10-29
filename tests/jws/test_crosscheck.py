"""Cross-check our Python-signed telemetry configs against Tesla's own Go tool.

Needs the ``tesla-jws`` binary from github.com/teslamotors/vehicle-command
(``go build ./cmd/tesla-jws``); set TESLA_JWS_BIN to its path. Skipped otherwise.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
import pytest
from tesla_fleet_api.tesla.jws import sign_fleet_telemetry_config

from custom_components.tesla_bridge.const import TelemetryPreset
from custom_components.tesla_bridge.telemetry_config import (
    TelemetryTarget,
    build_config,
)

TESLA_JWS = os.environ.get("TESLA_JWS_BIN")

pytestmark = pytest.mark.skipif(
    not TESLA_JWS or not Path(TESLA_JWS).exists(), reason="TESLA_JWS_BIN not set"
)

CA = "-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIUW\n-----END CERTIFICATE-----\n"


def _config(preset: TelemetryPreset) -> dict:
    return build_config(TelemetryTarget("myhome.duckdns.org", 8443, CA), preset)


@pytest.fixture
def key_file(tmp_path: Path) -> tuple[ec.EllipticCurvePrivateKey, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path / "fleet-key.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return key, path


@pytest.mark.parametrize("preset", list(TelemetryPreset))
def test_go_verifies_python_signature(
    tmp_path: Path,
    key_file: tuple[ec.EllipticCurvePrivateKey, Path],
    preset: TelemetryPreset,
) -> None:
    key, _ = key_file
    token = sign_fleet_telemetry_config(key, _config(preset))
    jws = tmp_path / "config.jws"
    jws.write_text(token)
    result = subprocess.run(
        [TESLA_JWS, "verify", str(jws)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("preset", list(TelemetryPreset))
def test_python_matches_go_byte_for_byte(
    tmp_path: Path,
    key_file: tuple[ec.EllipticCurvePrivateKey, Path],
    preset: TelemetryPreset,
) -> None:
    key, path = key_file
    config = _config(preset)
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config))
    result = subprocess.run(
        [
            TESLA_JWS,
            "-key-file",
            str(path),
            "-fleet",
            "sign",
            "TelemetryClient",
            str(config_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == sign_fleet_telemetry_config(key, config)
