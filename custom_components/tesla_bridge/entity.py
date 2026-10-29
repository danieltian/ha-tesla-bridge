"""Base entities for vehicles (stream-driven), energy sites and the app."""

from __future__ import annotations

from collections.abc import Iterable
import datetime as dt
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.util import dt as dt_util

from .models import BridgeRuntime, EnergySiteData, VehicleData

PENDING_TIMEOUT = dt.timedelta(seconds=90)


class VehicleStreamEntity(Entity):
    """An entity whose state comes from the car's telemetry stream.

    It never polls. It updates when one of its ``stream_fields`` changes and
    keeps the last value while the car sleeps.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    stream_fields: tuple[str, ...] = ()

    def __init__(
        self, vehicle: VehicleData, key: str, stream_fields: Iterable[str] = ()
    ) -> None:
        self.vehicle = vehicle
        self.stream = vehicle.stream
        self.key = key
        if stream_fields:
            self.stream_fields = tuple(stream_fields)
        self._attr_unique_id = f"{vehicle.vin}-{key}"
        self._attr_translation_key = key
        self._attr_device_info = vehicle.device

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._async_update_attrs()
        # No declared fields means "update on anything" (e.g. composite states).
        self.async_on_remove(
            self.stream.async_add_listener(
                self._handle_stream_update, self.stream_fields or None
            )
        )

    @callback
    def _handle_stream_update(self) -> None:
        self._async_update_attrs()
        self.async_write_ha_state()

    @callback
    def _async_update_attrs(self) -> None:
        """Copy stream values into ``_attr_*``."""
        raise NotImplementedError

    def get(self, field: str, default: Any = None) -> Any:
        return self.stream.get(field, default)


class EnergySiteEntity[DataT](CoordinatorEntity[DataUpdateCoordinator[DataT]]):
    """An energy-site entity backed by one coordinator.

    Controls write through the cloud but may display the gateway's (faster)
    local reading; ``follow_local`` makes them update on local polls too.
    """

    _attr_has_entity_name = True
    follow_local = False

    def __init__(
        self,
        site: EnergySiteData,
        coordinator: DataUpdateCoordinator[DataT],
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self.site = site
        self.key = key
        self._pending: tuple[Any, dt.datetime] | None = None
        self._attr_unique_id = f"{site.site_id}-{key}"
        self._attr_translation_key = key
        self._attr_device_info = site.device
        self._async_update_attrs()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        local = self.site.local
        if self.follow_local and local is not None and local is not self.coordinator:
            self.async_on_remove(
                local.async_add_listener(self._handle_coordinator_update)
            )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._async_update_attrs()
        super()._handle_coordinator_update()

    @callback
    def _async_update_attrs(self) -> None:
        raise NotImplementedError

    def _set_pending(self, value: Any) -> None:
        """Remember a value just written through the cloud."""
        self._pending = (value, dt_util.utcnow())

    def _confirmed(self, reported: Any) -> Any:
        """Show a just-written value until a source reports it (or it times out).

        Without this, a refresh that runs before Tesla or the gateway has applied
        the change would flip the control back to the old value.
        """
        if self._pending is None:
            return reported
        value, since = self._pending
        if reported == value or dt_util.utcnow() - since > PENDING_TIMEOUT:
            self._pending = None
            return reported
        return value


class BridgeAppEntity(CoordinatorEntity[DataUpdateCoordinator[dict[str, Any]]]):
    """A diagnostic entity describing the companion app."""

    _attr_has_entity_name = True

    def __init__(self, bridge: BridgeRuntime, key: str, unique_prefix: str) -> None:
        super().__init__(bridge.status)
        self.bridge = bridge
        self.key = key
        self._attr_unique_id = f"{unique_prefix}-bridge-{key}"
        self._attr_translation_key = key
        self._attr_device_info: DeviceInfo = bridge.device

    def status_value(self, *path: str) -> Any:
        node: Any = self.coordinator.data or {}
        for part in path:
            if not isinstance(node, dict):
                return None
            node = node.get(part)
        return node
