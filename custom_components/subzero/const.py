"""Integration settings and recognized appliance properties."""

DOMAIN = "subzero"
CONTROL_CONFIRM_TIMEOUT = 5
ICE_CONFIRM_TIMEOUT = 8
RECONNECT_DELAY = 30
MAX_RECONNECT_DELAY = 900
MAX_EVENT_HISTORY = 256
NOTIFICATION_TYPES = {
    0: "unknown",
    101: "refrigerator_door_ajar",
    102: "freezer_door_ajar",
    103: "wine_door_ajar",
    104: "wine_setpoint_changed",
    105: "refrigerator_service_required",
    106: "refrigerator_setpoint_changed",
    107: "freezer_setpoint_changed",
    108: "water_filter_expired",
    109: "air_filter_expired",
    112: "wine_temperature_alert",
    113: "ice_maker_door_ajar",
    114: "ice_cleaning_required",
    115: "ice_cleaning_due_soon",
    116: "ice_cleaning_add_descaler",
    117: "ice_cleaning_add_sanitizer",
    118: "ice_cleaning_cancelled",
    119: "ice_cleaning_complete",
    201: "oven_preheated",
    202: "lower_oven_preheated",
    203: "oven_probe_connected",
    204: "lower_oven_probe_connected",
    205: "oven_probe_target_reached",
    206: "lower_oven_probe_target_reached",
    207: "kitchen_timer_complete",
    208: "kitchen_timer_2_complete",
    209: "kitchen_timer_under_one_minute",
    210: "kitchen_timer_2_under_one_minute",
    211: "oven_cooking_timer_complete",
    212: "lower_oven_cooking_timer_complete",
    213: "oven_cooking_timer_under_one_minute",
    214: "lower_oven_cooking_timer_under_one_minute",
    215: "oven_probe_within_ten_degrees",
    216: "lower_oven_probe_within_ten_degrees",
    217: "oven_service_required",
    218: "oven_door_ajar",
    219: "lower_oven_door_ajar",
    220: "oven_self_clean_complete",
    221: "lower_oven_self_clean_complete",
    301: "dishwasher_started",
    302: "dishwasher_complete",
    303: "softener_salt_low",
    304: "rinse_aid_low",
    305: "dishwasher_service_required",
    306: "dishwasher_paused",
    307: "dishwasher_cancelled",
    400: "fault_notification",
    401: "feedback_notification",
}

SETPOINT_KEYS = {"ref_set_temp", "ref2_set_temp", "frz_set_temp", "crisp_set_temp"}
WINE_SETPOINT_KEYS = {"wine_set_temp", "wine2_set_temp"}
FRIDGE_MODE_KEYS = ("sabbath_on", "high_use_on", "short_vacation_on", "long_vacation_on")
ICE_KEYS = ("ice_maker_on", "max_ice_on", "night_ice_on")
ICE_MODES = {"Max ice": "max_ice_on", "Night ice": "night_ice_on"}
ICE_DELAY_KEYS = {"delay_start_offset", "delay_duration", "delay_recurring"}
HOOD_SWITCHES = {
    "delay_enabled": "Delayed shutoff",
    "key_tone_on": "Button tones",
    "user_lock_on": "Control lock",
}
HOOD_BOOLEAN_KEYS = {"fan_on", "light_on", *HOOD_SWITCHES}
HOOD_INTEGER_RANGES = {
    "fan_speed": (0, 4),
    "light_percent": (5, 100),
    "color_level": (0, 100),
    "halo_max_percent": (0, 30),
    "auto_sensivity": (-1, 2),
    "delay_off_duration": (0, 719 * 60000),
}
HOOD_SENSITIVITY = {"Off": -1, "Low": 0, "Medium": 1, "High": 2}
ICE_CLEAN_STAGES = {
    0: "Off",
    50: "Not cleaning",
    51: "Empty bin",
    52: "Manually clean",
    53: "Add descaler",
    60: "Descale fill",
    61: "Descale clean",
    62: "Descale flush",
    63: "Descale rinse",
    64: "Sanitize fill",
    65: "Add sanitizer",
    66: "Sanitize clean",
    67: "Sanitize flush",
    68: "Sanitize rinse",
    73: "Cleaning reset flush",
    74: "Cleaning reset rinse",
    80: "Cleaning complete",
}
FRIDGE_ENUM_OPTIONS = {
    "crisp_temp_mode": {"Automatic": 1, "Manual": 0},
    "humidity_control": {"Normal": 1, "Enhanced": 2},
    "night_mode": {"Disabled": 0, "Enabled": 1},
    "accent_light_level": {"Off": 0, "On": 100, "Low": 110, "Medium": 120, "High": 130},
}
# The app shows every humidity state but only lets Normal and Enhanced be selected.
HUMIDITY_LABELS = {0: "Disabled", 1: "Normal", 2: "Enhanced", 3: "Low"}
DOOR_AJAR_TIMEOUTS = {"Off": 0, "1 minute": 1, "2 minutes": 2, "5 minutes": 5, "10 minutes": 10}
LEGACY_ACCENT_LIGHT_OPTIONS = {"Off": 0, "On": 100, "Low": 30, "Medium": 50, "High": 70}
ACCENT_LIGHT_LABELS = {
    value: name
    for options in (FRIDGE_ENUM_OPTIONS["accent_light_level"], LEGACY_ACCENT_LIGHT_OPTIONS)
    for name, value in options.items()
}

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
}
GOURMET_RECIPES = {
    0: "None",
    1: "Beef steak, 1 to 3 in",
    2: "Beef steak, under 1 in",
    3: "Beef steak, over 3 in, rare",
    4: "Beef steak, over 3 in, medium rare",
    5: "Beef steak, over 3 in, medium",
    6: "Beef steak, over 3 in, medium well",
    7: "Beef steak, over 3 in, well done",
    8: "Prime rib, rare",
    9: "Prime rib, medium rare",
    10: "Prime rib, medium",
    11: "Prime rib, medium well",
    12: "Prime rib, well done",
    13: "Beef tenderloin, rare",
    14: "Beef tenderloin, medium rare",
    15: "Beef tenderloin, medium",
    16: "Beef tenderloin, medium well",
    17: "Beef tenderloin, well done",
    18: "Beef roast, rare",
    19: "Beef roast, medium rare",
    20: "Beef roast, medium",
    21: "Beef roast, medium well",
    22: "Beef roast, well done",
    23: "Meatloaf",
    24: "Slow cooked beef",
    25: "Pork tenderloin",
    26: "Whole ham",
    27: "Pork ribs",
    28: "Pork roast",
    29: "Pork steak",
    30: "Pork chop",
    31: "Whole poultry, under 12 lb, unbrined",
    32: "Whole poultry, under 12 lb, brined",
    33: "Whole poultry, 12 to 20 lb, unbrined",
    34: "Whole poultry, 12 to 20 lb, brined",
    35: "Whole poultry, over 20 lb, unbrined",
    36: "Whole poultry, over 20 lb, brined",
    37: "Poultry breast",
    38: "Poultry pieces, mixed meat",
    39: "Poultry pieces, white meat",
    40: "Poultry pieces, dark meat",
    41: "Leg of lamb",
    42: "Rack of lamb",
    43: "Lamb roast",
    44: "Roasted vegetables",
    45: "Baked potato",
    46: "Sweet potato",
    47: "Fish fillet",
    48: "Fish steak",
    49: "Breaded fish",
    50: "Sheet cake, 1 rack",
    51: "Sheet cake, 2 racks",
    52: "Sheet cake, 3 racks",
    53: "Cupcakes, 1 rack",
    54: "Cupcakes, 2 racks",
    55: "Cupcakes, 3 racks",
    56: "Fluted cake",
    57: "Angel food cake",
    58: "Pound cake",
    59: "Cookies, 1 rack",
    60: "Cookies, 2 racks",
    61: "Cookies, 3 racks",
    62: "Double crust pie",
    63: "Single crust pie",
    64: "Quick bread",
    65: "Biscuits",
    66: "Yeast loaf",
    67: "Yeast rolls, 1 rack",
    68: "Yeast rolls, 2 racks",
    69: "Yeast rolls, 3 racks",
    70: "Fresh pizza",
    71: "Par-baked pizza",
    72: "Calzone",
    73: "Casserole, 1 rack",
    74: "Casserole, 2 racks",
    75: "Casserole, 3 racks",
    76: "Quiche",
    77: "Lasagna, 1 rack",
    78: "Lasagna, 2 racks",
    79: "Lasagna, 3 racks",
}
# Wolf requires these modes to be started at the appliance.
MANUAL_COOK_MODES = {3, 7, 9, 11}
# E series and M series start with a power write alone; the app sends the
# cooking mode, power, and setpoint to every other series.
LEGACY_START_SERIES = {3, 4}
OVEN_TEMPERATURE_RANGES = {
    3: {
        **dict.fromkeys((1, 2, 4, 5, 6), (170, 550)),
        8: (120, 550),
        9: (85, 110),
        10: (110, 160),
    },
    4: {
        **dict.fromkeys((1, 2, 4, 5, 6), (200, 550)),
        9: (85, 110),
        10: (110, 170),
        12: (140, 200),
    },
    **{
        series: {
            **dict.fromkeys((1, 2, 4, 6, 8), (200, 550)),
            9: (85, 110),
            10: (110, 170),
            12: (140, 200),
        }
        for series in (8, 15)
    },
}
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
DISHWASHER_MODES = {"Normal": 0, "Child lock": 1, "Sabbath": 2}
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
    1: "Start pending",
    2: "Running",
    3: "Restart pending",
    4: "Cancel pending",
    5: "Drying",
    6: "Complete",
    7: "Delayed",
    8: "Error",
}

WRITABLE_BOOLEAN_KEYS = {
    *HOOD_BOOLEAN_KEYS,
    *FRIDGE_MODE_KEYS,
    *ICE_KEYS,
    "air_filter_on",
    "internal_dispenser_enabled",
    *(f"{prefix}_{suffix}" for prefix in OVEN_PREFIXES for suffix in ("unit_on", "light_on")),
    *DISHWASHER_SWITCHES,
    "wash_cycle_on",
}
WRITABLE_INTEGER_KEYS = {
    *HOOD_INTEGER_RANGES,
    *SETPOINT_KEYS,
    *WINE_SETPOINT_KEYS,
    *FRIDGE_ENUM_OPTIONS,
    *(
        f"{prefix}_{suffix}"
        for prefix in OVEN_PREFIXES
        for suffix in ("set_temp", "cook_mode", "probe_set_temp")
    ),
    *KITCHEN_TIMERS,
    "wash_cycle",
    "mode",
    "delay_start_timer_duration",
    "door_ajar_timeout",
}
NETWORK_KEYS = {"ipv4_addr", "device_wlan_id"}
FAULT_SEVERITIES = {
    0: "undefined",
    1: "low",
    2: "medium",
    3: "high",
    4: "critical",
    5: "urgent",
}
FAULT_METADATA_APPLIES_TO_BY_SERIES = {
    0: "unknown",
    1: "bi",
    2: "ngi",
    3: "eSeries",
    4: "mSeries",
    5: "wine",
    6: "cove",
    7: "pro",
    8: "range",
    9: "specialty",
    11: "bi5",
    12: "bi5Wine",
    13: "deu",
    14: "deuWine",
    15: "nge",
    16: "hybridM",
    17: "ds3",
    18: "ds3Wine",
    20: "cove2",
    21: "dice",
    22: "ngix",
    23: "pvii",
}
SENSOR_KEYS = {
    "filter_count",
    "filter_max_count",
    "ice_maker_clean_stage",
    "next_clean_cycles",
    "delay_duration",
    "delay_start_offset",
    *SETPOINT_KEYS,
    *WINE_SETPOINT_KEYS,
    "ref_display_temp",
    "ref2_display_temp",
    "crisp_display_temp",
    "frz_display_temp",
    "wine_display_temp",
    "wine2_display_temp",
    "air_filter_pct_remaining",
    "water_filter_pct_remaining",
    "water_filter_gal_remaining",
    *(
        f"{prefix}_{suffix}"
        for prefix in OVEN_PREFIXES
        for suffix in ("temp", "set_temp", "probe_temp", "probe_set_temp", "gourmet_recipe")
    ),
    "wash_cycle",
    "wash_status",
    "ap_rssi",
    "uptime",
    *NETWORK_KEYS,
}
BINARY_KEYS = {
    *HOOD_BOOLEAN_KEYS,
    "ice_door_ajar",
    "water_filter_inserted",
    "delay_active",
    "delay_recurring",
    "failsafe_on",
    "winterize_on",
    "ice_maker_clean_on",
    "clean_soon_on",
    "clean_now_on",
    "ref_door_ajar",
    "ref2_door_ajar",
    "frz_door_ajar",
    "wine_door_ajar",
    "wine2_door_ajar",
    "wine_temp_alert_on",
    "service_required",
    "unit_on",
    *ICE_KEYS,
    *FRIDGE_MODE_KEYS,
    "air_filter_on",
    "internal_dispenser_enabled",
    *(
        f"{prefix}_{suffix}"
        for prefix in OVEN_PREFIXES
        for suffix in (
            "door_ajar",
            "unit_on",
            "at_set_temp",
            "light_on",
            "remote_ready",
            "probe_on",
            "probe_at_set_temp",
            "gourmet_mode_on",
            "mode_change_enabled",
            "cook_timer_active",
            "cook_timer_complete",
        )
    ),
    *(
        f"{prefix}_{state}"
        for prefix in KITCHEN_TIMERS.values()
        for state in ("active", "complete")
    ),
    "door_ajar",
    "wash_cycle_on",
    "remote_ready",
    "rinse_aid_low",
    "softener_low",
    "delay_start_timer_active",
    *DISHWASHER_SWITCHES,
}
TIMESTAMP_KEYS = {
    "delay_start_time",
    "delay_end_time",
    "next_clean_time",
    "max_ice_start_time",
    "max_ice_end_time",
    "high_use_start_time",
    "high_use_end_time",
    *(
        f"{prefix}_cook_timer_{point}_time"
        for prefix in OVEN_PREFIXES
        for point in ("start", "end")
    ),
    *(f"{prefix}_{point}_time" for prefix in KITCHEN_TIMERS.values() for point in ("start", "end")),
    "wash_cycle_end_time",
    "delay_start_timer_start_time",
    "delay_start_timer_end_time",
}
STATE_KEYS = (
    SENSOR_KEYS
    | BINARY_KEYS
    | TIMESTAMP_KEYS
    | WRITABLE_INTEGER_KEYS
    | {"appliance_model", "appliance_type", "version", "time", "notifs"}
)
