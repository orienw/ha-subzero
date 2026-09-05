"""Integration settings and recognized appliance properties."""

DOMAIN = "subzero"


def selected_devices(entry) -> dict[str, dict]:
    """Return the appliance selection, including entries awaiting migration."""
    if "device_id" in entry.data:
        return {
            entry.data["device_id"]: {
                "name": entry.title,
                "temperature_unit": entry.data.get("temperature_unit"),
            }
        }
    return entry.options.get("devices", entry.data["devices"])


SENSOR_KEYS = {
    "ref_set_temp",
    "frz_set_temp",
    "crisp_set_temp",
    "air_filter_pct_remaining",
    "water_filter_pct_remaining",
    "ap_rssi",
    "cav_temp",
    "cav_set_temp",
    "cav_probe_temp",
    "cav_probe_set_temp",
}
BINARY_KEYS = {
    "ref_door_ajar",
    "frz_door_ajar",
    "service_required",
    "unit_on",
    "ice_maker_on",
    "max_ice_on",
    "night_ice_on",
    "air_filter_on",
    "sabbath_on",
    "high_use_on",
    "short_vacation_on",
    "long_vacation_on",
    "cav_door_ajar",
    "cav_unit_on",
    "cav_at_set_temp",
    "cav_light_on",
    "cav_remote_ready",
    "cav_probe_on",
    "cav_probe_at_set_temp",
    "cav_gourmet_mode_on",
    "cav_cook_timer_complete",
    "kitchen_timer_active",
    "kitchen_timer_complete",
    "kitchen_timer2_active",
    "kitchen_timer2_complete",
}
STATE_KEYS = SENSOR_KEYS | BINARY_KEYS | {"appliance_model", "version"}
