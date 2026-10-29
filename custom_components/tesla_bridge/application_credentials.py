"""Application credentials platform for Tesla Bridge."""

from __future__ import annotations

from homeassistant.components.application_credentials import ClientCredential
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow

from .oauth import TeslaUserImplementation


async def async_get_auth_implementation(
    hass: HomeAssistant, auth_domain: str, credential: ClientCredential
) -> config_entry_oauth2_flow.AbstractOAuth2Implementation:
    """Return the Tesla OAuth implementation."""
    return TeslaUserImplementation(hass, auth_domain, credential)


async def async_get_description_placeholders(hass: HomeAssistant) -> dict[str, str]:
    """Placeholders for the 'add application credentials' dialog."""
    return {
        "developer_dashboard": "https://developer.tesla.com/dashboard",
        "redirect_uri": "https://my.home-assistant.io/redirect/oauth",
    }
