"""Take over entities from the Tesla Fleet and Tesla Powerwall integrations.

Moving a registry entry to this integration keeps its entity ID, name, icon,
area, labels and (because the entity ID is unchanged) its recorded history and
long-term statistics, so dashboards, automations and the Energy dashboard keep
working. Only entities whose meaning matches are moved.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry, ConfigEntryDisabler
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import UNDEFINED

from .const import DOMAIN, LOGGER, POWERWALL_DOMAIN, TESLA_FLEET_DOMAIN

# new key -> Tesla Fleet key; unique IDs are f"{vin}-{key}".
TESLA_FLEET_VEHICLE: dict[Platform, dict[str, str]] = {
    Platform.SENSOR: {
        "battery_level": "charge_state_battery_level",
        "usable_battery_level": "charge_state_usable_battery_level",
        "charging_state": "charge_state_charging_state",
        "charger_power": "charge_state_charger_power",
        "charge_energy_added": "charge_state_charge_energy_added",
        "charger_voltage": "charge_state_charger_voltage",
        "charger_current": "charge_state_charger_actual_current",
        "charge_cable": "charge_state_conn_charge_cable",
        "fast_charger_type": "charge_state_fast_charger_type",
        "full_charge_at": "charge_state_minutes_to_full_charge",
        "battery_range": "charge_state_battery_range",
        "est_battery_range": "charge_state_est_battery_range",
        "ideal_battery_range": "charge_state_ideal_battery_range",
        "battery_power": "drive_state_power",
        "speed": "drive_state_speed",
        "shift_state": "drive_state_shift_state",
        "odometer": "vehicle_state_odometer",
        "inside_temp": "climate_state_inside_temp",
        "outside_temp": "climate_state_outside_temp",
        "driver_temp_setting": "climate_state_driver_temp_setting",
        "passenger_temp_setting": "climate_state_passenger_temp_setting",
        "tpms_pressure_fl": "vehicle_state_tpms_pressure_fl",
        "tpms_pressure_fr": "vehicle_state_tpms_pressure_fr",
        "tpms_pressure_rl": "vehicle_state_tpms_pressure_rl",
        "tpms_pressure_rr": "vehicle_state_tpms_pressure_rr",
        "route_destination": "drive_state_active_route_destination",
        "route_arrival": "drive_state_active_route_minutes_to_arrival",
        "route_distance": "drive_state_active_route_miles_to_arrival",
        "route_energy_at_arrival": "drive_state_active_route_energy_at_arrival",
        "route_traffic_delay": "drive_state_active_route_traffic_minutes_delay",
    },
    Platform.BINARY_SENSOR: {
        "online": "state",
        "charger_connected": "charge_state_conn_charge_cable",
        "battery_heater": "charge_state_battery_heater_on",
        "preconditioning": "charge_state_preconditioning_enabled",
        "scheduled_charging_pending": "charge_state_scheduled_charging_pending",
        "door_driver_front": "vehicle_state_df",
        "door_driver_rear": "vehicle_state_dr",
        "door_passenger_front": "vehicle_state_pf",
        "door_passenger_rear": "vehicle_state_pr",
        "window_driver_front": "vehicle_state_fd_window",
        "window_passenger_front": "vehicle_state_fp_window",
        "window_driver_rear": "vehicle_state_rd_window",
        "window_passenger_rear": "vehicle_state_rp_window",
        "tpms_soft_warning_fl": "vehicle_state_tpms_soft_warning_fl",
        "tpms_soft_warning_fr": "vehicle_state_tpms_soft_warning_fr",
        "tpms_soft_warning_rl": "vehicle_state_tpms_soft_warning_rl",
        "tpms_soft_warning_rr": "vehicle_state_tpms_soft_warning_rr",
    },
    Platform.LOCK: {
        "lock": "vehicle_state_locked",
        "charge_cable_lock": "charge_state_charge_port_latch",
    },
    Platform.SWITCH: {
        "charge": "charge_state_charging_state",
        "sentry_mode": "vehicle_state_sentry_mode",
        "defrost": "climate_state_defrost_mode",
        "auto_seat_climate_left": "climate_state_auto_seat_climate_left",
        "auto_seat_climate_right": "climate_state_auto_seat_climate_right",
        "auto_steering_wheel_heat": "climate_state_auto_steering_wheel_heat",
    },
    Platform.NUMBER: {
        "charge_limit": "charge_state_charge_limit_soc",
        "charge_current": "charge_state_charge_current_request",
    },
    Platform.SELECT: {
        "seat_heater_left": "climate_state_seat_heater_left",
        "seat_heater_right": "climate_state_seat_heater_right",
        "seat_heater_rear_left": "climate_state_seat_heater_rear_left",
        "seat_heater_rear_center": "climate_state_seat_heater_rear_center",
        "seat_heater_rear_right": "climate_state_seat_heater_rear_right",
        "steering_wheel_heat_level": "climate_state_steering_wheel_heat_level",
    },
    Platform.COVER: {
        "windows": "windows",
        "charge_port_door": "charge_state_charge_port_door_open",
        "frunk": "vehicle_state_ft",
        "trunk": "vehicle_state_rt",
    },
    Platform.CLIMATE: {"climate": "driver_temp"},
    Platform.DEVICE_TRACKER: {"location": "location", "route": "route"},
    Platform.MEDIA_PLAYER: {"media": "media"},
    Platform.UPDATE: {"software_update": "vehicle_state_software_update_status"},
    Platform.BUTTON: {
        "wake": "wake",
        "flash_lights": "flash_lights",
        "honk": "honk",
        "homelink": "homelink",
        "boombox": "boombox",
    },
}

# new key -> Tesla Fleet energy key; unique IDs are f"{site_id}-{key}".
TESLA_FLEET_ENERGY: dict[Platform, dict[str, str]] = {
    Platform.SENSOR: {
        "percentage_charged": "percentage_charged",
        "energy_left": "energy_left",
        "solar_power": "solar_power",
        "battery_power": "battery_power",
        "load_power": "load_power",
        "grid_power": "grid_power",
    },
    Platform.BINARY_SENSOR: {"grid_services_active": "grid_services_active"},
    Platform.NUMBER: {
        "backup_reserve": "backup_reserve_percent",
        "off_grid_ev_reserve": "off_grid_vehicle_charging_reserve_percent",
    },
    Platform.SELECT: {
        "operation_mode": "default_real_mode",
        "export_rule": "components_customer_preferred_export_rule",
    },
    Platform.SWITCH: {
        "storm_watch": "user_settings_storm_mode_enabled",
        "allow_grid_charging": "components_disallow_charge_from_grid_with_solar_installed",
    },
}

# new key -> Tesla Powerwall suffix; unique IDs are f"{din}_{suffix}". These win
# over Tesla Fleet for the same entity (longer, local, 30-second history).
POWERWALL_SITE: dict[Platform, dict[str, str]] = {
    Platform.SENSOR: {
        "charge_raw": "charge",
        "battery_power": "battery_instant_power",
        "solar_power": "solar_instant_power",
        "load_power": "load_instant_power",
        "grid_power": "site_instant_power",
        "grid_imported": "site_import",
        "grid_exported": "site_export",
        "solar_generated": "solar_export",
        "battery_discharged": "battery_export",
        "battery_charged": "battery_import",
        "home_consumed": "load_import",
        "grid_voltage": "site_instant_voltage",
    },
    Platform.BINARY_SENSOR: {
        "grid_connected": "grid_status",
        "grid_services_active": "grid_services_active",
        "battery_charging": "powerwall_charging",
    },
}
# Per-battery: f"{din}_{serial}_{suffix}" -> f"{site_id}-{serial}_{key}".
POWERWALL_BATTERY: dict[str, str] = {
    "capacity": "battery_capacity",
    "energy_remaining": "battery_remaining",
    "power": "instant_power",
    "charged": "battery_import",
    "discharged": "battery_export",
}


@dataclass
class AdoptResult:
    """What was taken over."""

    moved: list[str] = field(default_factory=list)
    disabled_entries: list[str] = field(default_factory=list)


def old_integrations(hass: HomeAssistant) -> list[ConfigEntry]:
    return [
        *hass.config_entries.async_entries(TESLA_FLEET_DOMAIN),
        *hass.config_entries.async_entries(POWERWALL_DOMAIN),
    ]


async def async_adopt_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    vins: list[str],
    sites: list[tuple[int, list[str]]],
) -> AdoptResult:
    """Disable the old integrations and move matching entities to this entry.

    ``sites`` is a list of ``(site_id, battery_serials)``.
    """
    result = AdoptResult()
    registry = er.async_get(hass)

    old_entries = old_integrations(hass)
    for old in old_entries:
        if old.disabled_by is None:
            # Unloads it; registry entries must not be loaded to be moved.
            await hass.config_entries.async_set_disabled_by(
                old.entry_id, ConfigEntryDisabler.USER
            )
            result.disabled_entries.append(old.title)

    def move(platform: Platform, old_domain: str, old_uid: str, new_uid: str) -> None:
        old_entity_id = registry.async_get_entity_id(platform, old_domain, old_uid)
        if old_entity_id is None:
            return
        if registry.async_get_entity_id(platform, DOMAIN, new_uid) is not None:
            return  # already ours (an earlier mapping won)
        # Passing an unchanged unique ID makes the registry report a conflict
        # with the entity itself, so only pass it when it differs.
        registry.async_update_entity_platform(
            old_entity_id,
            DOMAIN,
            new_config_entry_id=entry.entry_id,
            new_unique_id=new_uid if new_uid != old_uid else UNDEFINED,
            new_device_id=None,
        )
        result.moved.append(old_entity_id)

    # The local Powerwall integration first: it has the better energy history.
    gateway_dins = [
        old.unique_id
        for old in old_entries
        if old.domain == POWERWALL_DOMAIN and old.unique_id
    ]
    for site_id, serials in sites:
        for din in gateway_dins:
            for platform, keys in POWERWALL_SITE.items():
                for new_key, suffix in keys.items():
                    move(
                        platform,
                        POWERWALL_DOMAIN,
                        f"{din}_{suffix}",
                        f"{site_id}-{new_key}",
                    )
            for serial in serials:
                for new_key, suffix in POWERWALL_BATTERY.items():
                    move(
                        Platform.SENSOR,
                        POWERWALL_DOMAIN,
                        f"{din}_{serial}_{suffix}",
                        f"{site_id}-{serial}_{new_key}",
                    )
        for platform, keys in TESLA_FLEET_ENERGY.items():
            for new_key, old_key in keys.items():
                move(
                    platform,
                    TESLA_FLEET_DOMAIN,
                    f"{site_id}-{old_key}",
                    f"{site_id}-{new_key}",
                )

    for vin in vins:
        for platform, keys in TESLA_FLEET_VEHICLE.items():
            for new_key, old_key in keys.items():
                move(
                    platform, TESLA_FLEET_DOMAIN, f"{vin}-{old_key}", f"{vin}-{new_key}"
                )

    LOGGER.info(
        "Took over %d entities from %s",
        len(result.moved),
        ", ".join(result.disabled_entries) or "-",
    )
    return result
