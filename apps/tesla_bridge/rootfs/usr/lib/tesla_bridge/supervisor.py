"""Talk to the Home Assistant Supervisor (discovery)."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import aiohttp

from .const import DISCOVERY_SERVICE, SUPERVISOR_URL

_LOGGER = logging.getLogger(__name__)


def supervisor_token() -> str | None:
    return os.environ.get("SUPERVISOR_TOKEN")


async def send_discovery(
    session: aiohttp.ClientSession, config: dict[str, Any]
) -> bool:
    """Announce the app to the integration (``async_step_hassio``)."""
    token = supervisor_token()
    if not token:
        _LOGGER.debug("No SUPERVISOR_TOKEN; skipping discovery (not running as an app)")
        return False
    try:
        async with asyncio.timeout(15):
            async with session.post(
                f"{SUPERVISOR_URL}/discovery",
                json={"service": DISCOVERY_SERVICE, "config": config},
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status >= 400:
                    _LOGGER.warning(
                        "Discovery rejected by the Supervisor: HTTP %s %s",
                        resp.status,
                        await resp.text(),
                    )
                    return False
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.warning("Could not send discovery: %s", err)
        return False
    _LOGGER.info("Announced Tesla Bridge to Home Assistant")
    return True
