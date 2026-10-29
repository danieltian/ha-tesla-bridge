"""Fixtures for the app (``apps/tesla_bridge``) tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tesla_bridge.state import StateStore

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def zmq_records() -> list[dict[str, Any]]:
    """Records captured from fleet-telemetry v0.9.5 driven by tools/vehicle-sim."""
    lines = (FIXTURES / "zmq_capture_v0.9.5.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


@pytest.fixture
def state(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "state.json")
    store.load()
    return store
