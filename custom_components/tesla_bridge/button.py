"""Buttons: wake, flash, honk, HomeLink, boombox, re-push streaming config."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TeslaBridgeConfigEntry, async_reconcile_telemetry
from .const import DOMAIN
from .entity import VehicleStreamEntity
from .helpers import location
from .models import VehicleData
from .vehicle import ensure_awake, run_command

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class VehicleButtonDescription(ButtonEntityDescription):
    """A one-shot vehicle command."""

    press_fn: Callable[[VehicleData], Awaitable[Any]]


async def _homelink(vehicle: VehicleData) -> Any:
    coords = location(vehicle.stream.get("Location"))
    if coords is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_location"
        )
    lat, lon = coords
    return await run_command(
        vehicle, lambda: vehicle.api.trigger_homelink(lat=lat, lon=lon)
    )


VEHICLE_BUTTONS: tuple[VehicleButtonDescription, ...] = (
    VehicleButtonDescription(key="wake", press_fn=ensure_awake),
    VehicleButtonDescription(
        key="flash_lights", press_fn=lambda v: run_command(v, v.api.flash_lights)
    ),
    VehicleButtonDescription(
        key="honk", press_fn=lambda v: run_command(v, v.api.honk_horn)
    ),
    VehicleButtonDescription(key="homelink", press_fn=_homelink),
    VehicleButtonDescription(
        key="boombox",
        press_fn=lambda v: run_command(v, lambda: v.api.remote_boombox(0)),
        entity_registry_enabled_default=False,
    ),
)


class VehicleButton(VehicleStreamEntity, ButtonEntity):
    """A vehicle command button."""

    entity_description: VehicleButtonDescription

    def __init__(
        self, vehicle: VehicleData, description: VehicleButtonDescription
    ) -> None:
        self.entity_description = description
        super().__init__(vehicle, description.key, ("__never__",))

    @callback
    def _async_update_attrs(self) -> None:
        return

    async def async_press(self) -> None:
        await self.entity_description.press_fn(self.vehicle)


class PushTelemetryButton(ButtonEntity):
    """Re-send the streaming config to every car (diagnostic)."""

    _attr_has_entity_name = True
    _attr_translation_key = "push_telemetry"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: TeslaBridgeConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}-bridge-push_telemetry"
        self._attr_device_info = entry.runtime_data.bridge.device

    async def async_press(self) -> None:
        await async_reconcile_telemetry(self.hass, self._entry, force=True)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TeslaBridgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    entities: list[ButtonEntity] = [
        VehicleButton(vehicle, description)
        for vehicle in entry.runtime_data.vehicles
        for description in VEHICLE_BUTTONS
    ]
    entities.append(PushTelemetryButton(entry))
    async_add_entities(entities)
