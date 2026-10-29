"""OAuth2 implementation for Tesla's Fleet API."""

from __future__ import annotations

from typing import Any

from homeassistant.components.application_credentials import (
    AuthImplementation,
    AuthorizationServer,
    ClientCredential,
)
from homeassistant.core import HomeAssistant

from .const import AUTHORIZE_URL, SCOPES, TOKEN_URL


class TeslaUserImplementation(AuthImplementation):
    """Tesla Fleet API user OAuth2 implementation."""

    def __init__(
        self, hass: HomeAssistant, auth_domain: str, credential: ClientCredential
    ) -> None:
        super().__init__(
            hass,
            auth_domain,
            credential,
            AuthorizationServer(AUTHORIZE_URL, TOKEN_URL),
        )

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        """Ask Tesla to prompt for any scope the user has not granted yet."""
        return {
            "prompt": "login",
            "prompt_missing_scopes": "true",
            "scope": " ".join(SCOPES),
        }
