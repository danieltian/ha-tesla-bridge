"""Vehicle software updates."""

from __future__ import annotations

from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TeslaBridgeConfigEntry
from .entity import VehicleStreamEntity
from .helpers import as_bool, as_float
from .models import VehicleData
from .vehicle import run_command

PARALLEL_UPDATES = 0


def _short(version: Any) -> str | None:
    """'2026.38.4 1a2b3c' -> '2026.38.4'."""
    if not isinstance(version, str) or not version.strip():
        return None
    return version.split(" ")[0]


class SoftwareUpdate(VehicleStreamEntity, UpdateEntity):
    """Installed vs. available firmware; installing schedules it now."""

    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
    )

    def __init__(self, vehicle: VehicleData) -> None:
        super().__init__(
            vehicle,
            "software_update",
            (
                "Version",
                "SoftwareUpdateVersion",
                "SoftwareUpdateInProgress",
                "SoftwareUpdateInstallationPercentComplete",
                "SoftwareUpdateDownloadPercentComplete",
            ),
        )

    @callback
    def _async_update_attrs(self) -> None:
        installed = _short(self.get("Version"))
        available = _short(self.get("SoftwareUpdateVersion"))
        self._attr_installed_version = installed
        self._attr_latest_version = available or installed
        installing = as_bool(self.get("SoftwareUpdateInProgress"))
        percent = as_float(self.get("SoftwareUpdateInstallationPercentComplete"))
        if installing or (percent is not None and 0 < percent < 100):
            self._attr_in_progress = True
            self._attr_update_percentage = None if percent is None else int(percent)
        else:
            self._attr_in_progress = False
            self._attr_update_percentage = None

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        await run_command(
            self.vehicle, lambda: self.vehicle.api.schedule_software_update(0)
        )
        self._attr_in_progress = True
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TeslaBridgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(SoftwareUpdate(v) for v in entry.runtime_data.vehicles)
