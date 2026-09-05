"""Integration settings and the properties verified on the CL4850UFDID."""

DOMAIN = "subzero"

SENSOR_KEYS = {
    "ref_set_temp",
    "frz_set_temp",
    "crisp_set_temp",
    "air_filter_pct_remaining",
    "water_filter_pct_remaining",
    "ap_rssi",
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
}
STATE_KEYS = SENSOR_KEYS | BINARY_KEYS | {"appliance_model", "version"}
