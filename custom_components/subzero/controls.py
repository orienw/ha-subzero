"""Reported control capabilities, appliance interlocks, and protocol values."""

import math
from datetime import UTC, datetime, timezone, tzinfo

from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from .const import (
    ACCENT_LIGHT_LABELS,
    BROIL,
    BROIL_LEVELS,
    COOK_MODES,
    CRISP_MANUAL_MODE,
    DEPENDENT_ENTITY_KEYS,
    DISHWASHER_CANCELLABLE_STATUSES,
    DISHWASHER_EDITABLE_STATUSES,
    DISHWASHER_MODES,
    DISHWASHER_SABBATH_MODE,
    DISHWASHER_SWITCHES,
    DOOR_AJAR_TIMEOUTS,
    EXCLUDED_PROPERTIES,
    FRIDGE_ENUM_OPTIONS,
    FRIDGE_MODE_KEYS,
    HALO_ON_PERCENT,
    HOOD_BOOLEAN_KEYS,
    HOOD_INTEGER_RANGES,
    HOOD_SERIES,
    HUMIDITY_LABELS,
    ICE_KEYS,
    ICE_MAKER_SERIES,
    ICE_MODES,
    KITCHEN_TIMERS,
    KNOB_OVEN_TYPES,
    LEGACY_ACCENT_LIGHT_OPTIONS,
    LEGACY_START_SERIES,
    MANUAL_COOK_MODES,
    NON_TEMP_COOK_MODES,
    OVEN_PREFIXES,
    OVEN_TEMPERATURE_RANGES,
    RANGE_SERIES,
    SETPOINT_KEYS,
    START_TEMPERATURES,
    WASH_CYCLES,
    WINE_SETPOINT_KEYS,
    WRITABLE_BOOLEAN_KEYS,
    WRITABLE_INTEGER_KEYS,
)


def is_int(value) -> bool:
    """Return whether value is an int, excluding bool."""
    return type(value) is int


def is_finite_number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def uptime_seconds(value: str) -> int | None:
    """Read hours:minutes:seconds uptime.

    Appliances cut uptime to eight characters, so from 100 hours they drop its last
    digits, such as 387:52:1 for 387:52:1x. The missing digits count as zeros.
    """
    hours, separator, rest = value.partition(":")
    if not separator:
        return None
    if len(value) == 8 and len(hours) > 2:
        rest += "00:00"[len(rest) :]
    try:
        minutes, seconds = map(int, rest.split(":"))
        total_hours = int(hours)
    except ValueError:
        return None
    if total_hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        return None
    return total_hours * 3600 + minutes * 60 + seconds


def appliance_type(data: dict) -> tuple[int, int, int, int] | None:
    value = data.get("appliance_type")
    if not isinstance(value, str):
        return None
    parts = value.split(".")
    if len(parts) == 3:
        parts.insert(0, "0")
    if len(parts) != 4 or not all(part.isascii() and part.isdecimal() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def is_fridge(data: dict) -> bool:
    return bool(SETPOINT_KEYS.intersection(data))


def is_ice_maker(data: dict) -> bool:
    parts = appliance_type(data)
    return parts is not None and parts[1] == ICE_MAKER_SERIES


def is_hood(data: dict) -> bool:
    parts = appliance_type(data)
    return parts is not None and parts[1] == HOOD_SERIES


def excluded_properties(data: dict) -> set[str]:
    """Properties the app discards for the reported appliance type."""
    parts = appliance_type(data)
    return EXCLUDED_PROPERTIES.get(parts[1:], set()) if parts is not None else set()


def excluded_entity_keys(data: dict) -> set[str]:
    """Entities the app does not show because of a discarded property."""
    discarded = excluded_properties(data)
    keys = {key for prop in discarded for key in (prop, *DEPENDENT_ENTITY_KEYS.get(prop, ()))}
    if discarded.intersection(FRIDGE_MODE_KEYS) and data.keys().isdisjoint(FRIDGE_MODE_KEYS):
        keys.add("operating_mode")
    return keys


def accent_light_options(data: dict) -> dict[str, int]:
    parts = appliance_type(data)
    if parts is not None and parts[1] in {1, 5, 7}:
        return LEGACY_ACCENT_LIGHT_OPTIONS
    return FRIDGE_ENUM_OPTIONS["accent_light_level"]


def enum_labels(key: str) -> dict[int, str]:
    if key == "accent_light_level":
        return ACCENT_LIGHT_LABELS
    if key == "humidity_control":
        return HUMIDITY_LABELS
    return {value: name for name, value in FRIDGE_ENUM_OPTIONS[key].items()}


def is_wine(data: dict) -> bool:
    return bool(WINE_SETPOINT_KEYS.intersection(data))


def is_oven(data: dict) -> bool:
    return any(key.startswith(("cav_", "cav2_", "kitchen_timer")) for key in data)


def is_dishwasher(data: dict) -> bool:
    return bool({"wash_cycle", "wash_status", "wash_cycle_on"}.intersection(data))


def has_left_and_right_ovens(data: dict) -> bool:
    """Whether the app calls the cavities right and left ovens: a range with two.

    Like the app, a second cavity's setpoint shows that there are two.
    """
    parts = appliance_type(data)
    return parts is not None and parts[1] == RANGE_SERIES and data.get("cav2_set_temp") is not None


def supports_air_filter_reset(data: dict) -> bool:
    return (is_fridge(data) or is_wine(data)) and "air_filter_pct_remaining" in data


def wash_settings_enabled(data: dict) -> bool:
    return is_int(data.get("wash_status")) and data["wash_status"] in DISHWASHER_EDITABLE_STATUSES


def wash_cancel_enabled(data: dict) -> bool:
    # The app offers cancel while a cycle runs, dries, or waits for a delayed start.
    status = data.get("wash_status")
    return data.get("wash_cycle_on") is True or (
        is_int(status) and status in DISHWASHER_CANCELLABLE_STATUSES
    )


def freezer_setpoint_enabled(data: dict) -> bool:
    return "max_ice_on" not in data or data["max_ice_on"] is False


def crisper_manual_enabled(data: dict) -> bool:
    return is_int(data.get("crisp_temp_mode")) and data["crisp_temp_mode"] == CRISP_MANUAL_MODE


def oven_ready_for_settings(data: dict, prefix: str) -> bool:
    return data.get(f"{prefix}_unit_on") is True or data.get(f"{prefix}_remote_ready") is True


def probe_setpoint_enabled(data: dict, prefix: str) -> bool:
    return data.get(f"{prefix}_probe_on") is True and oven_ready_for_settings(data, prefix)


def broil_level(data: dict, prefix: str) -> str | None:
    """The app's broil level while a cavity broils."""
    temperature = data.get(f"{prefix}_set_temp")
    if (
        data.get(f"{prefix}_unit_on") is not True
        or data.get(f"{prefix}_cook_mode") != BROIL
        or not is_finite_number(temperature)
        or temperature <= 0
    ):
        return None
    return next(name for bound, name in BROIL_LEVELS if temperature < bound)


def supports_control(data: dict, key: str) -> bool:
    if key in KITCHEN_TIMERS:
        prefix = KITCHEN_TIMERS[key]
        return f"{prefix}_active" in data and f"{prefix}_end_time" in data
    if key not in data:
        return False
    if key in HOOD_BOOLEAN_KEYS or key in HOOD_INTEGER_RANGES:
        return is_hood(data)
    if is_ice_maker(data) and key in {"ice_maker_on", "door_ajar_timeout"}:
        return True
    if key.startswith(("cav_", "cav2_")):
        return key in WRITABLE_BOOLEAN_KEYS | WRITABLE_INTEGER_KEYS
    if key in {
        *DISHWASHER_SWITCHES,
        "wash_cycle",
        "wash_cycle_on",
        "mode",
        "delay_start_timer_duration",
    }:
        return is_dishwasher(data)
    if key in WINE_SETPOINT_KEYS:
        return is_wine(data)
    return (is_fridge(data) or is_wine(data)) and key in {
        *SETPOINT_KEYS,
        *FRIDGE_ENUM_OPTIONS,
        *FRIDGE_MODE_KEYS,
        *ICE_KEYS,
        "air_filter_on",
        "internal_dispenser_enabled",
        "door_ajar_timeout",
    }


def clock_zone(value) -> tzinfo | None:
    """The time zone of a pushed `time`, which carries the appliance's local offset.

    Status reads report `time` in UTC, so only pushes show the offset. When it
    matches Home Assistant's time zone, that zone also follows daylight saving.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        offset = parsed.utcoffset()
        if offset is None:
            return None
        home = dt_util.get_default_time_zone()
        return home if parsed.astimezone(home).utcoffset() == offset else timezone(offset)
    except ValueError, OverflowError:
        # Times at the edge of the supported years can't be converted between zones.
        return None


def appliance_datetime(value, zone: tzinfo | None = None) -> datetime | None:
    """Read an appliance timestamp, taking one without an offset as local time.

    Local time is the zone of the appliance's last pushed clock, or, like the app,
    which sets the appliance clock to the phone's local time, Home Assistant's.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=zone or dt_util.get_default_time_zone())
        # Appliance times never fall in the first or last supported year, where
        # converting or offsetting them overflows.
        if not 1 < parsed.astimezone(UTC).year < 9999:
            return None
    except ValueError, OverflowError:
        return None
    return parsed


def ice_mode(data: dict) -> str | None:
    keys = {key for key in ICE_KEYS if supports_control(data, key)}
    if "ice_maker_on" not in keys or any(type(data[key]) is not bool for key in keys):
        return None
    active = [name for name, key in ICE_MODES.items() if key in keys and data[key]]
    if active:
        return active[0] if len(active) == 1 else None
    return "On" if data["ice_maker_on"] else "Off"


def ice_mode_properties(data: dict, mode: str) -> dict[str, bool]:
    keys = {key for key in ICE_KEYS if supports_control(data, key)}
    options = {"Off", "On", *(name for name, key in ICE_MODES.items() if key in keys)}
    if "ice_maker_on" not in keys or mode not in options:
        raise ServiceValidationError("The appliance does not support that ice mode.")
    if any(type(data[key]) is not bool for key in keys):
        raise ServiceValidationError("The ice maker mode is unknown.")
    if mode == ice_mode(data):
        return {}
    if mode == "Off":
        return {key: False for key in ("max_ice_on", "night_ice_on", "ice_maker_on") if key in keys}
    if mode == "On":
        return {
            **{key: False for key in ("max_ice_on", "night_ice_on") if key in keys and data[key]},
            "ice_maker_on": True,
        }
    if mode == "Max ice":
        return {
            **({"night_ice_on": False} if data.get("night_ice_on") is True else {}),
            "max_ice_on": True,
        }
    return {
        **({"max_ice_on": False} if "max_ice_on" in keys else {}),
        "night_ice_on": True,
    }


def timer_minutes(data: dict, key: str, zone: tzinfo | None = None) -> float | None:
    prefix = KITCHEN_TIMERS[key]
    if data.get(f"{prefix}_active") is False:
        return 0
    if data.get(f"{prefix}_active") is not True:
        return None
    end = appliance_datetime(data.get(f"{prefix}_end_time"), zone)
    if end is not None:
        # Like the app, a running timer shows the time left on it, in whole minutes
        # that reach 0 only at the end.
        return max(0, math.ceil((end - dt_util.utcnow()).total_seconds() / 60))
    duration = data.get(key)
    return duration if type(duration) is int and duration >= 0 else None


def control_matches(
    data: dict,
    key: str,
    value: bool | int,
    requested_at: datetime,
    zone: tzinfo | None = None,
) -> bool:
    """Whether the appliance reports a setting as requested.

    `zone` is the appliance clock's time zone, for timer times without an offset.
    """
    if key == "accent_light_level":
        return (
            type(data.get(key)) is int
            and value in ACCENT_LIGHT_LABELS
            and ACCENT_LIGHT_LABELS.get(data[key]) == ACCENT_LIGHT_LABELS[value]
        )
    if key == "halo_max_percent" and value:
        # The app treats any nonzero halo level as on.
        return type(data.get(key)) is int and data[key] != 0
    if key not in KITCHEN_TIMERS:
        if key.endswith("_unit_on") and value is False:
            ready = key.replace("unit_on", "remote_ready")
            if ready in data and data[ready] is not False:
                return False
        if (
            key == "wash_cycle_on"
            and value is False
            and data.get("delay_start_timer_active") is True
        ):
            # A delayed start can report the cycle off before cancellation.
            return False
        return data.get(key) == value and isinstance(data.get(key), bool) == isinstance(value, bool)
    prefix = KITCHEN_TIMERS[key]
    if value == 0:
        return data.get(f"{prefix}_active") is False
    end = appliance_datetime(data.get(f"{prefix}_end_time"), zone)
    # The appliance clock has been seen within about 15 seconds of real time.
    return (
        data.get(f"{prefix}_active") is True
        and end is not None
        and abs((end - requested_at).total_seconds() - value * 60) <= 65
    )


def cook_mode_offered(data: dict, key: str, mode: int) -> bool:
    """Whether the app offers a cooking mode for this oven series and cavity."""
    parts = appliance_type(data)
    model = parts[1:] if parts is not None else None
    series = model[0] if model is not None else None
    lower = key.startswith("cav2_")
    if mode == 5:
        return series == 3 and not lower
    if mode == 12:
        return series != 3
    if lower and mode in {6, 8, 10}:
        return series != 3 and model not in {(15, 2, 4), (15, 2, 5)}
    if lower and mode == 4:
        return model not in {(15, 2, 4), (15, 2, 5), (8, 2, 0)}
    return True


def held_for_start(data: dict, key: str) -> bool:
    """Whether a cooking mode or temperature waits for the remote start.

    An idle oven in Remote Ready drops a cooking mode sent on its own, so like the
    app's start dialog the choice is kept and sent with the start. Older E series and
    M series ovens, series 3 and 4, start with the settings made at the oven.
    """
    prefix, _, name = key.partition("_")
    parts = appliance_type(data)
    return (
        prefix in OVEN_PREFIXES
        and name in {"cook_mode", "set_temp"}
        and data.get(f"{prefix}_unit_on") is False
        and data.get(f"{prefix}_remote_ready") is True
        and (parts is None or parts[1] not in LEGACY_START_SERIES)
    )


def validate_remote_start(data: dict, key: str) -> None:
    if key == "wash_cycle_on":
        if data.get("remote_ready") is not True:
            raise ServiceValidationError(
                "Enable Remote Ready on the dishwasher before starting it."
            )
        if "door_ajar" in data and data["door_ajar"] is not False:
            raise ServiceValidationError("Close the dishwasher door before starting it.")
        if "mode" in data:
            if not is_int(data["mode"]) or data["mode"] not in DISHWASHER_MODES.values():
                raise ServiceValidationError("The dishwasher mode is unknown.")
            if data["mode"] == DISHWASHER_SABBATH_MODE:
                raise ServiceValidationError(
                    "Turn off Sabbath mode before starting the dishwasher."
                )
        return
    prefix = key.removesuffix("_unit_on")
    if data.get(f"{prefix}_remote_ready") is not True:
        raise ServiceValidationError("Enable Remote Ready on the oven before starting it.")
    if f"{prefix}_door_ajar" in data and data[f"{prefix}_door_ajar"] is not False:
        raise ServiceValidationError("Close the oven door before starting it.")
    mode = data.get(f"{prefix}_cook_mode")
    if not is_int(mode) or mode not in COOK_MODES.values() or mode == 0:
        raise ServiceValidationError("Select a supported cooking mode before starting the oven.")
    if mode in MANUAL_COOK_MODES or data.get(f"{prefix}_gourmet_mode_on") is True:
        raise ServiceValidationError("Start this cooking mode at the oven's control panel.")
    temperature = data.get(f"{prefix}_set_temp")
    if not is_finite_number(temperature) or temperature <= 0:
        raise ServiceValidationError("Set the oven temperature before starting it.")
    bounds = temperature_range(f"{prefix}_set_temp", data)
    if bounds is not None and not bounds[0] <= temperature <= bounds[1]:
        raise ServiceValidationError("Set a temperature within the cooking mode's range.")


def start_properties(data: dict, key: str, temperature: int | None = None) -> dict:
    """The app's remote-start writes, in its order, from the configured settings."""
    if key == "wash_cycle_on":
        # The app sends the cycle, delay, and options only when they were
        # changed in its start dialog; here they are written when changed.
        return {key: True}
    prefix = key.removesuffix("_unit_on")
    parts = appliance_type(data)
    if parts is not None and parts[1] in LEGACY_START_SERIES:
        properties = {} if temperature is None else {f"{prefix}_set_temp": temperature}
        return {**properties, key: True}
    if temperature is None:
        temperature = data.get(f"{prefix}_set_temp")
    return {
        f"{prefix}_cook_mode": data.get(f"{prefix}_cook_mode"),
        key: True,
        f"{prefix}_set_temp": temperature,
    }


def temperature_range(key: str, data: dict) -> tuple[int, int] | None:
    if key in {f"{prefix}_probe_set_temp" for prefix in OVEN_PREFIXES}:
        return 120, 210
    if key in {f"{prefix}_set_temp" for prefix in OVEN_PREFIXES}:
        mode = data.get(key.replace("set_temp", "cook_mode"))
        if not is_int(mode) or mode not in COOK_MODES.values() or mode in NON_TEMP_COOK_MODES:
            return None
        parts = appliance_type(data)
        if parts is not None and parts[1] in OVEN_TEMPERATURE_RANGES:
            return OVEN_TEMPERATURE_RANGES[parts[1]].get(mode)
        return 85, 550
    if key == "frz_set_temp":
        return -5, 5
    if key == "crisp_set_temp":
        refrigerator = data.get("ref_set_temp")
        if not is_finite_number(refrigerator):
            return None
        maximum = temperature_range("ref_set_temp", data)[1]
        lower, upper = max(34, refrigerator - 2), min(maximum, refrigerator + 2)
        return (math.ceil(lower), math.floor(upper)) if lower <= upper else None
    if key in {"ref_set_temp", "ref2_set_temp"}:
        if (parts := appliance_type(data)) is not None:
            _, series, middle, _ = parts
            if series == 13 and middle in {3, 6}:
                return 34, 55
            return 34, (45 if series in {1, 2, 22} or series == 5 and middle == 2 else 42)
        model = data.get("appliance_model", "")
        legacy = isinstance(model, str) and model.startswith(("BI", "IT", "IC", "ID"))
        return 34, (45 if legacy else 42)
    if key in WINE_SETPOINT_KEYS:
        return 40, 65
    return None


def start_temperature(data: dict, key: str, kept: int | None) -> int | None:
    """The setpoint to keep for a remote start in the cavity's cooking mode.

    Like the app's start dialog, a kept setpoint that suits the mode stays. Without
    one the reported setpoint stands when it suits the mode, and the mode's usual
    temperature is kept when it does not.
    """
    bounds = temperature_range(key, data)
    if bounds is None:
        return None
    if kept is not None and bounds[0] <= kept <= bounds[1]:
        return kept
    reported = data.get(key)
    if is_finite_number(reported) and bounds[0] <= reported <= bounds[1]:
        return None
    usual = START_TEMPERATURES.get(data[key.replace("set_temp", "cook_mode")], 325)
    return min(max(usual, bounds[0]), bounds[1])


def _validate_boolean(data: dict, key: str, value) -> None:
    if type(value) is not bool or type(data[key]) is not bool:
        raise ServiceValidationError("The setting requires an on/off value.")


def _validate_hood_integer(data: dict, key: str, value) -> None:
    lower, upper = HOOD_INTEGER_RANGES[key]
    if not is_int(value) or not lower <= value <= upper:
        raise ServiceValidationError("The setting is outside the hood's range.")
    if not is_int(data[key]):
        raise ServiceValidationError("The current hood setting is unknown.")
    if key == "halo_max_percent" and value not in {0, HALO_ON_PERCENT}:
        raise ServiceValidationError("Halo lighting supports only on or off.")
    if key == "delay_off_duration" and value % 60000:
        raise ServiceValidationError("Enter a whole number of minutes.")


def _validate_fridge_enum(data: dict, key: str, value) -> None:
    options = (
        accent_light_options(data) if key == "accent_light_level" else FRIDGE_ENUM_OPTIONS[key]
    )
    if not is_int(value) or value not in options.values():
        raise ServiceValidationError("Select a supported appliance option.")
    if not is_int(data[key]) or data[key] not in enum_labels(key):
        raise ServiceValidationError("The current appliance setting is unknown.")


def _validate_dishwasher_enum(data: dict, key: str, value) -> None:
    values = WASH_CYCLES if key == "wash_cycle" else DISHWASHER_MODES.values()
    if not is_int(value) or value not in values or key == "wash_cycle" and value == 0:
        raise ServiceValidationError("Select a supported dishwasher option.")
    if not is_int(data[key]) or data[key] not in values:
        raise ServiceValidationError("The current dishwasher setting is unknown.")
    if not wash_settings_enabled(data):
        raise ServiceValidationError("The dishwasher must be idle or waiting to start.")


def _validate_kitchen_timer(data: dict, key: str, value) -> None:
    prefix = KITCHEN_TIMERS[key]
    if not is_int(value) or not 0 <= value <= 719:
        raise ServiceValidationError("Enter a timer duration from 0 to 719 minutes.")
    if type(data.get(f"{prefix}_active")) is not bool:
        raise ServiceValidationError("The timer state is unknown.")


def _validate_delay_start(data: dict, key: str, value) -> None:
    if not is_int(value) or not 0 <= value <= 12:
        raise ServiceValidationError("The setting is outside the appliance's range.")
    if not is_int(data[key]):
        raise ServiceValidationError("The current appliance setting is unknown.")


def _validate_door_ajar_timeout(data: dict, key: str, value) -> None:
    if not is_int(value) or value not in DOOR_AJAR_TIMEOUTS.values():
        raise ServiceValidationError("Select a supported door open delay.")
    if not is_int(data[key]) or data[key] not in DOOR_AJAR_TIMEOUTS.values():
        raise ServiceValidationError("The current appliance setting is unknown.")


def _validate_cook_mode(data: dict, key: str, value) -> None:
    if not is_int(value) or value not in COOK_MODES.values():
        raise ServiceValidationError("Select a supported cooking mode.")
    if not is_int(data[key]) or data[key] not in COOK_MODES.values():
        raise ServiceValidationError("The current cooking mode is unknown.")
    if value in MANUAL_COOK_MODES and data[key] != value:
        raise ServiceValidationError("Start this cooking mode at the oven's control panel.")
    if value != data[key] and not cook_mode_offered(data, key, value):
        raise ServiceValidationError("Select a supported cooking mode.")


def _validate_temperature(data: dict, temperature_unit: str | None, key: str, value) -> None:
    if temperature_unit not in ("F", "C"):
        raise ServiceValidationError("The appliance's temperature unit is unknown.")
    if not is_finite_number(data[key]):
        raise ServiceValidationError("The current appliance temperature is unknown.")
    bounds = temperature_range(key, data)
    if not is_int(value) or bounds is None or not bounds[0] <= value <= bounds[1]:
        raise ServiceValidationError("The temperature is outside the appliance's range.")
    if key == "frz_set_temp" and "max_ice_on" in data:
        if type(data["max_ice_on"]) is not bool:
            raise ServiceValidationError("The ice maker mode is unknown.")
        if data["max_ice_on"]:
            raise ServiceValidationError("Turn off Max ice before changing the freezer setpoint.")
    if key == "crisp_set_temp" and not crisper_manual_enabled(data):
        raise ServiceValidationError("Select Manual crisper temperature before setting it.")
    if key.endswith("_probe_set_temp"):
        if data.get(key.replace("probe_set_temp", "probe_on")) is not True:
            raise ServiceValidationError("Connect the probe before setting its target.")


def _validate_interlocks(data: dict, key: str, value) -> None:
    if key.endswith("_unit_on") or key == "wash_cycle_on":
        if value is True and data[key] is not True:
            validate_remote_start(data, key)
    if key.startswith(("cav_", "cav2_")) and key.endswith(("_set_temp", "_cook_mode")):
        prefix = key.split("_", 1)[0]
        if not oven_ready_for_settings(data, prefix):
            raise ServiceValidationError("The oven must be running or in Remote Ready.")
        if key.endswith("_cook_mode") and data[key] != value:
            flag = f"{prefix}_mode_change_enabled"
            parts = appliance_type(data)
            knobs = parts is not None and parts[1:] in KNOB_OVEN_TYPES
            if knobs and flag in data and data[flag] is not True:
                raise ServiceValidationError("The oven does not currently allow mode changes.")


def validate_control_properties(
    data: dict, temperature_unit: str | None, properties: dict, pending: dict | None = None
) -> None:
    """Check settings against the reported state before they are sent.

    `pending` holds the cooking mode and temperature of a remote start that are still
    to be sent. They travel with the start, so its writes are checked against the
    state they will leave.
    """
    if not properties:
        raise ServiceValidationError("No settings supplied.")
    for key, value in properties.items():
        state = {**data, **{k: v for k, v in pending.items() if k != key}} if pending else data
        if not supports_control(state, key):
            raise ServiceValidationError("The appliance does not report this setting.")
        if key in WRITABLE_BOOLEAN_KEYS:
            _validate_boolean(state, key, value)
        elif key in HOOD_INTEGER_RANGES:
            _validate_hood_integer(state, key, value)
        elif key in FRIDGE_ENUM_OPTIONS:
            _validate_fridge_enum(state, key, value)
        elif key in {"wash_cycle", "mode"}:
            _validate_dishwasher_enum(state, key, value)
        elif key in KITCHEN_TIMERS:
            _validate_kitchen_timer(state, key, value)
        elif key == "delay_start_timer_duration":
            _validate_delay_start(state, key, value)
        elif key == "door_ajar_timeout":
            _validate_door_ajar_timeout(state, key, value)
        elif key.endswith("_cook_mode"):
            _validate_cook_mode(state, key, value)
        else:
            _validate_temperature(state, temperature_unit, key, value)
        _validate_interlocks(state, key, value)
