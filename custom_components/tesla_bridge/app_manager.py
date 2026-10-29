"""Find, install and configure the Tesla Bridge app through the Supervisor."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .const import APP_NAME, APP_REPOSITORY, APP_SLUG_SUFFIX, LOGGER

DATA_APP_MANAGERS = f"{APP_SLUG_SUFFIX}_app_managers"


class AppUnavailable(Exception):
    """The Supervisor could not provide the app."""


async def async_find_app_slug(
    hass: HomeAssistant, *, add_repository: bool
) -> str | None:
    """Return the app's slug, adding its repository to the store if asked.

    A repository app's slug is ``<hash of repo url>_tesla_bridge``; a copy in
    the local ``/addons`` folder is ``local_tesla_bridge``.
    """
    from aiohasupervisor import SupervisorError
    from aiohasupervisor.models import StoreAddRepository

    from homeassistant.components.hassio.handler import (
        get_supervisor_client,
    )

    client = get_supervisor_client(hass)
    try:
        slug = _match(await client.store.addons_list())
        if slug or not add_repository:
            return slug
        repositories = await client.store.repositories_list()
        if not any(r.source.rstrip("/") == APP_REPOSITORY for r in repositories):
            LOGGER.info("Adding the Tesla Bridge app repository %s", APP_REPOSITORY)
            await client.store.add_repository(
                StoreAddRepository(repository=APP_REPOSITORY)
            )
        else:
            await client.store.reload()
        return _match(await client.store.addons_list())
    except SupervisorError as err:
        raise AppUnavailable(str(err)) from err


def _match(addons: list[Any]) -> str | None:
    slugs = [a.slug for a in addons if a.slug.endswith(f"_{APP_SLUG_SUFFIX}")]
    # Prefer a local development copy over the repository version.
    slugs.sort(key=lambda s: not s.startswith("local_"))
    return slugs[0] if slugs else None


def get_app_manager(hass: HomeAssistant, slug: str) -> Any:
    """One AddonManager per slug (it tracks running install/start tasks)."""
    from homeassistant.components.hassio import AddonManager

    managers: dict[str, Any] = hass.data.setdefault(DATA_APP_MANAGERS, {})
    if slug not in managers:
        managers[slug] = AddonManager(hass, LOGGER, APP_NAME, slug)
    return managers[slug]


async def async_app_discovery(hass: HomeAssistant, slug: str) -> dict[str, Any] | None:
    """The app's current discovery payload (host, port, token, domain, ports)."""
    from homeassistant.components.hassio import AddonError

    try:
        return dict(await get_app_manager(hass, slug).async_get_addon_discovery_info())
    except AddonError as err:
        LOGGER.debug("No discovery info from the Tesla Bridge app: %s", err)
        return None


async def async_app_host_ports(
    hass: HomeAssistant, slug: str
) -> tuple[int | None, int | None] | None:
    """Host ports the Supervisor maps to the app's 443/tcp and 8443/tcp.

    Users can change these in the app's Network settings; the router must
    forward the public ports to these, not necessarily to 443/8443. A port
    turned off there is None. None overall means the Supervisor could not
    say, which callers must not mistake for the defaults.
    """
    from aiohasupervisor import SupervisorError

    from homeassistant.components.hassio.handler import get_supervisor_client

    try:
        info = await get_supervisor_client(hass).addons.addon_info(slug)
    except (SupervisorError, KeyError) as err:  # KeyError: hassio not loaded
        LOGGER.debug("Could not read the app's port mapping: %s", err)
        return None
    network = info.network or {}
    return network.get("443/tcp"), network.get("8443/tcp")
