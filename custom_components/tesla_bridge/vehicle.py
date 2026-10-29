"""Vehicle commands: wake when needed, run, and translate errors."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from tesla_fleet_api.exceptions import TeslaFleetError, VehicleOffline

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN, LOGGER

if TYPE_CHECKING:
    from .models import VehicleData

WAKE_TIMEOUT = 45.0
# Responses that mean "nothing to do" rather than failure.
ACCEPTABLE_REASONS = {"already_set", "not_charging", "requested", "is_charging"}


async def ensure_awake(vehicle: VehicleData) -> None:
    """Wake the car unless the stream shows it is connected (awake)."""
    if vehicle.stream.connected:
        return
    async with vehicle.wakelock:
        if vehicle.stream.connected:
            return
        try:
            state = (await vehicle.api.vehicle())["response"]["state"]
            if state == "online":
                return
            LOGGER.debug("Waking %s (state %s)", vehicle.vin, state)
            await vehicle.api.wake_up(wait=True, timeout=WAKE_TIMEOUT)
        except VehicleOffline as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="wake_up_timeout"
            ) from err
        except TeslaFleetError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="wake_up_failed",
                translation_placeholders={"message": err.message},
            ) from err


def check_result(result: dict[str, Any]) -> None:
    """Raise if a vehicle command response reports failure."""
    response = result.get("response")
    if response is None:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_error",
            translation_placeholders={
                "error": str(result.get("error") or "no response")
            },
        )
    if response.get("result") is True:
        return
    reason = response.get("reason") or ""
    if reason in ACCEPTABLE_REASONS:
        return
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="command_reason",
        translation_placeholders={"reason": reason or "unknown"},
    )


async def run_command(
    vehicle: VehicleData,
    command: Callable[[], Awaitable[dict[str, Any]]],
    *,
    wake: bool = True,
) -> dict[str, Any]:
    """Wake if needed, run ``command`` and validate the reply."""
    if wake:
        await ensure_awake(vehicle)
    try:
        result = await command()
    except TeslaFleetError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_failed",
            translation_placeholders={"message": err.message},
        ) from err
    LOGGER.debug("Command result for %s: %s", vehicle.vin, result)
    check_result(result)
    return result


async def run_energy_command(command: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    """Run an energy-site command and translate errors."""
    try:
        result = await command
    except TeslaFleetError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_failed",
            translation_placeholders={"message": err.message},
        ) from err
    response = result.get("response") or {}
    if isinstance(response, dict) and response.get("code") not in (None, 200, 201):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_error",
            translation_placeholders={
                "error": str(response.get("message") or response)
            },
        )
    return result
