"""Reported control capabilities, appliance interlocks, and protocol values."""

import math
from datetime import datetime

from homeassistant.exceptions import ServiceValidationError

from .const import (
    ACCENT_LIGHT_LABELS,
    COOK_MODES,
    DISHWASHER_MODES,
    DISHWASHER_SWITCHES,
    DOOR_AJAR_TIMEOUTS,
    FRIDGE_ENUM_OPTIONS,
    FRIDGE_MODE_KEYS,
    HOOD_BOOLEAN_KEYS,
    HOOD_INTEGER_RANGES,
    HUMIDITY_LABELS,
    ICE_KEYS,
    ICE_MODES,
    KITCHEN_TIMERS,
    LEGACY_ACCENT_LIGHT_OPTIONS,
    LEGACY_START_SERIES,
    MANUAL_COOK_MODES,
    OVEN_PREFIXES,
    OVEN_TEMPERATURE_RANGES,
    SETPOINT_KEYS,
    WASH_CYCLES,
    WINE_SETPOINT_KEYS,
    WRITABLE_BOOLEAN_KEYS,
    WRITABLE_INTEGER_KEYS,
)


def is_finite_number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


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
    return parts is not None and parts[1] == 21


def is_hood(data: dict) -> bool:
    parts = appliance_type(data)
    return parts is not None and parts[1] == 23


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


def supports_air_filter_reset(data: dict) -> bool:
    return (is_fridge(data) or is_wine(data)) and "air_filter_pct_remaining" in data


def wash_settings_enabled(data: dict) -> bool:
    return type(data.get("wash_status")) is int and data["wash_status"] in {0, 1}


def supports_control(data: dict, key: str) -> bool:
    if key in KITCHEN_TIMERS:
        prefix = KITCHEN_TIMERS[key]
        return f"{prefix}_active" in data and f"{prefix}_end_time" in data
    if key not in data:
        return False
    if key in HOOD_BOOLEAN_KEYS or key in HOOD_INTEGER_RANGES:
        return is_hood(data)
    if is_ice_maker(data) and key in {"ice_maker_on", "sabbath_on", "door_ajar_timeout"}:
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


def appliance_datetime(value, data: dict) -> datetime | None:
    """Use only explicit offsets, including the appliance clock's offset."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            clock = datetime.fromisoformat(data.get("time", ""))
            parsed = parsed.replace(tzinfo=clock.tzinfo)
        return parsed if parsed.tzinfo is not None else None
    except ValueError, TypeError:
        return None


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


def timer_minutes(data: dict, key: str) -> float | None:
    prefix = KITCHEN_TIMERS[key]
    if data.get(f"{prefix}_active") is False:
        return 0
    if data.get(f"{prefix}_active") is not True:
        return None
    start = appliance_datetime(data.get(f"{prefix}_start_time"), data)
    end = appliance_datetime(data.get(f"{prefix}_end_time"), data)
    if start is not None and end is not None and end >= start:
        return round((end - start).total_seconds() / 60)
    duration = data.get(key)
    return duration if type(duration) is int and duration >= 0 else None


def control_matches(data: dict, key: str, value: bool | int, requested_at: datetime) -> bool:
    if key == "accent_light_level":
        return (
            type(data.get(key)) is int
            and value in ACCENT_LIGHT_LABELS
            and ACCENT_LIGHT_LABELS.get(data[key]) == ACCENT_LIGHT_LABELS[value]
        )
    if key not in KITCHEN_TIMERS:
        if key.endswith("_unit_on") and value is False:
            ready = key.replace("unit_on", "remote_ready")
            if ready in data and data[ready] is not False:
                return False
        return data.get(key) == value and isinstance(data.get(key), bool) == isinstance(value, bool)
    prefix = KITCHEN_TIMERS[key]
    if value == 0:
        return data.get(f"{prefix}_active") is False
    end = appliance_datetime(data.get(f"{prefix}_end_time"), data)
    duration = timer_minutes(data, key)
    return (
        data.get(f"{prefix}_active") is True
        and end is not None
        and (duration is None or duration == value)
        and abs((end - requested_at).total_seconds() - value * 60) <= 65
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
            if type(data["mode"]) is not int or data["mode"] not in DISHWASHER_MODES.values():
                raise ServiceValidationError("The dishwasher mode is unknown.")
            if data["mode"] == 2:
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
    if type(mode) is not int or mode not in COOK_MODES.values() or mode == 0:
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
        properties = {}
        if data.get("wash_cycle") in WASH_CYCLES and data["wash_cycle"] != 0:
            properties["wash_cycle"] = data["wash_cycle"]
        if type(data.get("delay_start_timer_duration")) is int:
            properties["delay_start_timer_duration"] = data["delay_start_timer_duration"]
        return {**properties, key: True}
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
        if type(mode) is not int or mode not in COOK_MODES.values() or mode in {0, 3, 7, 11}:
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


def validate_control_properties(data: dict, temperature_unit: str | None, properties: dict) -> None:
    if not properties:
        raise ServiceValidationError("No settings supplied.")
    for key, value in properties.items():
        if not supports_control(data, key):
            raise ServiceValidationError("The appliance does not report this setting.")
        if key in WRITABLE_BOOLEAN_KEYS:
            if type(value) is not bool or type(data[key]) is not bool:
                raise ServiceValidationError("The setting requires an on/off value.")
        elif key in HOOD_INTEGER_RANGES:
            lower, upper = HOOD_INTEGER_RANGES[key]
            if type(value) is not int or not lower <= value <= upper:
                raise ServiceValidationError("The setting is outside the hood's range.")
            if type(data[key]) is not int:
                raise ServiceValidationError("The current hood setting is unknown.")
            if key == "halo_max_percent" and value not in {0, 30}:
                raise ServiceValidationError("Halo lighting supports only on or off.")
            if key == "delay_off_duration" and value % 60000:
                raise ServiceValidationError("Enter a whole number of minutes.")
        elif key in FRIDGE_ENUM_OPTIONS:
            accent = key == "accent_light_level"
            values = (accent_light_options(data) if accent else FRIDGE_ENUM_OPTIONS[key]).values()
            if type(value) is not int or value not in values:
                raise ServiceValidationError("Select a supported appliance option.")
            if type(data[key]) is not int or data[key] not in enum_labels(key):
                raise ServiceValidationError("The current appliance setting is unknown.")
        elif key in {"wash_cycle", "mode"}:
            values = WASH_CYCLES if key == "wash_cycle" else DISHWASHER_MODES.values()
            if type(value) is not int or value not in values or key == "wash_cycle" and value == 0:
                raise ServiceValidationError("Select a supported dishwasher option.")
            if type(data[key]) is not int or data[key] not in values:
                raise ServiceValidationError("The current dishwasher setting is unknown.")
            if not wash_settings_enabled(data):
                raise ServiceValidationError("The dishwasher must be idle or waiting to start.")
        elif key in KITCHEN_TIMERS:
            prefix = KITCHEN_TIMERS[key]
            if type(value) is not int or not 0 <= value <= 719:
                raise ServiceValidationError("Enter a timer duration from 0 to 719 minutes.")
            if type(data.get(f"{prefix}_active")) is not bool:
                raise ServiceValidationError("The timer state is unknown.")
        elif key == "delay_start_timer_duration":
            if type(value) is not int or not 0 <= value <= 12:
                raise ServiceValidationError("The setting is outside the appliance's range.")
            if type(data[key]) is not int:
                raise ServiceValidationError("The current appliance setting is unknown.")
        elif key == "door_ajar_timeout":
            if type(value) is not int or value not in DOOR_AJAR_TIMEOUTS.values():
                raise ServiceValidationError("Select a supported door open delay.")
            if type(data[key]) is not int or data[key] not in DOOR_AJAR_TIMEOUTS.values():
                raise ServiceValidationError("The current appliance setting is unknown.")
        elif key.endswith("_cook_mode"):
            if type(value) is not int or value not in COOK_MODES.values():
                raise ServiceValidationError("Select a supported cooking mode.")
            if type(data[key]) is not int or data[key] not in COOK_MODES.values():
                raise ServiceValidationError("The current cooking mode is unknown.")
            if value in MANUAL_COOK_MODES and data[key] != value:
                raise ServiceValidationError("Start this cooking mode at the oven's control panel.")
        else:
            if temperature_unit not in ("F", "C"):
                raise ServiceValidationError("The appliance's temperature unit is unknown.")
            if not is_finite_number(data[key]):
                raise ServiceValidationError("The current appliance temperature is unknown.")
            bounds = temperature_range(key, data)
            if type(value) is not int or bounds is None or not bounds[0] <= value <= bounds[1]:
                raise ServiceValidationError("The temperature is outside the appliance's range.")
            if key == "frz_set_temp" and "max_ice_on" in data:
                if type(data["max_ice_on"]) is not bool:
                    raise ServiceValidationError("The ice maker mode is unknown.")
                if data["max_ice_on"]:
                    raise ServiceValidationError(
                        "Turn off Max Ice before changing the freezer setpoint."
                    )
            if key == "crisp_set_temp":
                if type(data.get("crisp_temp_mode")) is not int or data["crisp_temp_mode"] != 0:
                    raise ServiceValidationError(
                        "Select Manual crisper temperature before setting it."
                    )
            if key.endswith("_probe_set_temp"):
                if data.get(key.replace("probe_set_temp", "probe_on")) is not True:
                    raise ServiceValidationError("Connect the probe before setting its target.")
        if key.endswith("_unit_on") or key == "wash_cycle_on":
            if value is True and data[key] is not True:
                validate_remote_start(data, key)
        if key.startswith(("cav_", "cav2_")) and key.endswith(("_set_temp", "_cook_mode")):
            prefix = key.split("_", 1)[0]
            if (
                data.get(f"{prefix}_unit_on") is not True
                and data.get(f"{prefix}_remote_ready") is not True
            ):
                raise ServiceValidationError("The oven must be running or in Remote Ready.")
            if key.endswith("_cook_mode") and data[key] != value:
                flag = f"{prefix}_mode_change_enabled"
                if flag in data and data[flag] is not True:
                    raise ServiceValidationError("The oven does not currently allow mode changes.")
