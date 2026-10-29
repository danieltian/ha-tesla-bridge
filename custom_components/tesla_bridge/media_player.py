"""The car's media player (streamed now-playing info)."""

from __future__ import annotations

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import TeslaBridgeConfigEntry
from .entity import VehicleStreamEntity
from .helpers import as_float, enum_state
from .models import VehicleData
from .vehicle import run_command

PARALLEL_UPDATES = 0

STATES = {
    "playing": MediaPlayerState.PLAYING,
    "paused": MediaPlayerState.PAUSED,
    "stopped": MediaPlayerState.IDLE,
}


class VehicleMedia(VehicleStreamEntity, MediaPlayerEntity):
    """Now playing, volume and transport controls."""

    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = (
        MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.NEXT_TRACK
        | MediaPlayerEntityFeature.PREVIOUS_TRACK
        | MediaPlayerEntityFeature.VOLUME_SET
    )

    def __init__(self, vehicle: VehicleData) -> None:
        super().__init__(
            vehicle,
            "media",
            (
                "MediaPlaybackStatus",
                "MediaPlaybackSource",
                "MediaAudioVolume",
                "MediaAudioVolumeMax",
                "MediaNowPlayingTitle",
                "MediaNowPlayingArtist",
                "MediaNowPlayingAlbum",
                "MediaNowPlayingStation",
                "MediaNowPlayingDuration",
                "MediaNowPlayingElapsed",
                "__connected__",
            ),
        )

    @callback
    def _async_update_attrs(self) -> None:
        status = enum_state(self.get("MediaPlaybackStatus"), "MediaStatus")
        self._attr_state = (
            MediaPlayerState.OFF
            if self.stream.connected is False
            else STATES.get(status or "")
        )
        volume = as_float(self.get("MediaAudioVolume"))
        maximum = as_float(self.get("MediaAudioVolumeMax")) or 11.0
        self._attr_volume_level = None if volume is None else round(volume / maximum, 3)
        self._attr_media_title = self.get("MediaNowPlayingTitle") or None
        self._attr_media_artist = self.get("MediaNowPlayingArtist") or None
        self._attr_media_album_name = self.get("MediaNowPlayingAlbum") or None
        self._attr_media_channel = self.get("MediaNowPlayingStation") or None
        self._attr_source = self.get("MediaPlaybackSource") or None
        duration = as_float(self.get("MediaNowPlayingDuration"))
        elapsed = as_float(self.get("MediaNowPlayingElapsed"))
        # The car reports milliseconds.
        self._attr_media_duration = None if duration is None else duration / 1000
        self._attr_media_position = None if elapsed is None else elapsed / 1000

    async def async_media_play(self) -> None:
        if self.state != MediaPlayerState.PLAYING:
            await run_command(self.vehicle, self.vehicle.api.media_toggle_playback)

    async def async_media_pause(self) -> None:
        if self.state == MediaPlayerState.PLAYING:
            await run_command(self.vehicle, self.vehicle.api.media_toggle_playback)

    async def async_media_next_track(self) -> None:
        await run_command(self.vehicle, self.vehicle.api.media_next_track)

    async def async_media_previous_track(self) -> None:
        await run_command(self.vehicle, self.vehicle.api.media_prev_track)

    async def async_set_volume_level(self, volume: float) -> None:
        maximum = as_float(self.get("MediaAudioVolumeMax")) or 11.0
        value = round(volume * maximum, 1)
        await run_command(self.vehicle, lambda: self.vehicle.api.adjust_volume(value))
        self.stream.set_override("MediaAudioVolume", value)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TeslaBridgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(VehicleMedia(v) for v in entry.runtime_data.vehicles)
