"""Small persistent state shared by the app's subsystems (``/data/state.json``)."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict, dataclass, field
import json
import logging
import os
from pathlib import Path
import secrets
import tempfile
from typing import Any

from .const import STATE_FILE

_LOGGER = logging.getLogger(__name__)

SAVE_DELAY = 5.0


@dataclass
class PersistedState:
    """What survives an app restart."""

    api_token: str = ""
    public_key_pem: str | None = None
    # VINs the integration expects; records from any other VIN are dropped.
    # Empty means "accept every vehicle" (before the integration has synced).
    vins: list[str] = field(default_factory=list)
    # vin -> {"day": "YYYY-MM-DD", "count": int, "month": "YYYY-MM", "month_count": int}
    signals: dict[str, dict[str, Any]] = field(default_factory=dict)
    # vin -> {"fields": {name: {"v": value, "ts": iso}}, "last_seen": iso}
    vehicles: dict[str, dict[str, Any]] = field(default_factory=dict)


class StateStore:
    """Loads, mutates and lazily persists :class:`PersistedState`."""

    def __init__(self, path: Path = STATE_FILE) -> None:
        self._path = path
        self.data = PersistedState()
        self._save_handle: asyncio.TimerHandle | None = None

    def load(self) -> None:
        try:
            raw = json.loads(self._path.read_text())
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError):
            _LOGGER.exception("State file %s is unreadable; starting fresh", self._path)
            raw = {}
        known = PersistedState.__dataclass_fields__
        self.data = PersistedState(**{k: v for k, v in raw.items() if k in known})
        if not self.data.api_token:
            self.data.api_token = secrets.token_urlsafe(32)
            self.save_now()

    def schedule_save(self) -> None:
        """Persist soon; coalesces bursts of updates from the telemetry stream."""
        if self._save_handle is not None:
            return
        loop = asyncio.get_running_loop()
        self._save_handle = loop.call_later(SAVE_DELAY, self._delayed_save)

    def _delayed_save(self) -> None:
        self._save_handle = None
        self.save_now()

    def save_now(self) -> None:
        if self._save_handle is not None:
            self._save_handle.cancel()
            self._save_handle = None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".state.")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(asdict(self.data), handle, separators=(",", ":"))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
