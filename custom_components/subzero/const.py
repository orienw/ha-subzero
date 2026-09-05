"""Integration settings and recognized appliance properties."""

DOMAIN = "subzero"
CONTROL_CONFIRM_TIMEOUT = 5
RECONNECT_DELAY = 30
MAX_RECONNECT_DELAY = 900

SETPOINT_KEYS = {"ref_set_temp", "frz_set_temp", "crisp_set_temp"}
FRIDGE_MODE_KEYS = ("sabbath_on", "high_use_on", "short_vacation_on", "long_vacation_on")
ICE_KEYS = ("ice_maker_on", "max_ice_on", "night_ice_on")
FRIDGE_ENUM_OPTIONS = {
    "crisp_temp_mode": {"Automatic": 1, "Manual": 0},
    "humidity_control": {"Normal": 1, "Enhanced": 2},
    "night_mode": {"Disabled": 0, "Enabled": 1},
}
WRITABLE_BOOLEAN_KEYS = {*FRIDGE_MODE_KEYS, *ICE_KEYS, "air_filter_on"}
WRITABLE_INTEGER_KEYS = SETPOINT_KEYS | set(FRIDGE_ENUM_OPTIONS)

OVEN_PREFIXES = ("cav", "cav2")
COOK_MODES = {
    "Off": 0,
    "Bake": 1,
    "Roast": 2,
    "Broil": 3,
    "Bake stone": 4,
    "Convection bake": 5,
    "Convection roast": 6,
    "Convection broil": 7,
    "Convection": 8,
    "Proof": 9,
    "Dehydrate": 10,
    "Self clean": 11,
    "Warm": 12,
    "Eco": 13,
}
# Wolf requires these modes to be started at the appliance.
MANUAL_COOK_MODES = {3, 7, 9, 11}
KITCHEN_TIMERS = {
    "kitchen_timer_duration": "kitchen_timer",
    "kitchen_timer2_duration": "kitchen_timer2",
}
DISHWASHER_SWITCHES = {
    "heated_dry_on": "Heated dry",
    "extended_dry_on": "Extended dry",
    "high_temp_wash_on": "High temperature wash",
    "sani_rinse_on": "Sanitize rinse",
    "top_rack_only_on": "Top rack only",
}
WASH_CYCLES = {
    0: "None",
    1: "Auto",
    2: "Normal",
    3: "Heavy",
    4: "Quick",
    5: "Pots and pans",
    6: "Soak and scrub",
    7: "Light",
    8: "Crystal china",
    9: "Rinse and hold",
    10: "Plastics",
    11: "Energy",
    12: "Extra quiet",
}
WASH_STATUSES = {
    0: "Idle",
    1: "Ready",
    2: "Running",
    3: "Paused",
    4: "Canceled",
    5: "Drying",
    6: "Complete",
}
WRITABLE_BOOLEAN_KEYS |= {
    *(f"{prefix}_{suffix}" for prefix in OVEN_PREFIXES for suffix in ("unit_on", "light_on")),
    *DISHWASHER_SWITCHES,
    "wash_cycle_on",
}
WRITABLE_INTEGER_KEYS |= {
    *(f"{prefix}_{suffix}" for prefix in OVEN_PREFIXES for suffix in ("set_temp", "cook_mode")),
    *KITCHEN_TIMERS,
    "accent_light_level",
    "delay_start_timer_duration",
}


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
SENSOR_KEYS |= {
    "ref_display_temp",
    "frz_display_temp",
    "water_filter_gal_remaining",
    "wash_cycle",
    "wash_status",
    "uptime",
    "ipv4_addr",
    "device_wlan_id",
    *(key.replace("cav_", "cav2_", 1) for key in SENSOR_KEYS if key.startswith("cav_")),
}
BINARY_KEYS |= {
    "cav_mode_change_enabled",
    "cav_cook_timer_active",
    "door_ajar",
    "wash_cycle_on",
    "remote_ready",
    "rinse_aid_low",
    "softener_low",
    "delay_start_timer_active",
    *DISHWASHER_SWITCHES,
}
BINARY_KEYS |= {key.replace("cav_", "cav2_", 1) for key in BINARY_KEYS if key.startswith("cav_")}
TIMESTAMP_KEYS = {
    "max_ice_start_time",
    "max_ice_end_time",
    "high_use_start_time",
    "high_use_end_time",
    "wash_cycle_end_time",
    "delay_start_timer_start_time",
    "delay_start_timer_end_time",
    *(
        f"{prefix}_cook_timer_{point}_time"
        for prefix in OVEN_PREFIXES
        for point in ("start", "end")
    ),
    *(f"{prefix}_{point}_time" for prefix in KITCHEN_TIMERS.values() for point in ("start", "end")),
}
STATE_KEYS = (
    SENSOR_KEYS
    | BINARY_KEYS
    | TIMESTAMP_KEYS
    | WRITABLE_INTEGER_KEYS
    | {
        "appliance_model",
        "appliance_type",
        "version",
        "time",
    }
)
