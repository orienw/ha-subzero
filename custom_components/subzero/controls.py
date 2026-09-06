"""Reported control capabilities, appliance interlocks, and protocol values."""

import math
from datetime import datetime

from homeassistant.exceptions import ServiceValidationError

from .const import (
    COOK_MODES,
    DISHWASHER_SWITCHES,
    FRIDGE_ENUM_OPTIONS,
    FRIDGE_MODE_KEYS,
    ICE_KEYS,
    KITCHEN_TIMERS,
    MANUAL_COOK_MODES,
    OVEN_PREFIXES,
    SETPOINT_KEYS,
    WRITABLE_BOOLEAN_KEYS,
    WRITABLE_INTEGER_KEYS,
)


def is_finite_number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def is_fridge(data: dict) -> bool:
    return bool(SETPOINT_KEYS.intersection(data))


def is_oven(data: dict) -> bool:
    return any(key.startswith(("cav_", "cav2_", "kitchen_timer")) for key in data)


def is_dishwasher(data: dict) -> bool:
    return bool({"wash_cycle", "wash_status", "wash_cycle_on"}.intersection(data))


def supports_control(data: dict, key: str) -> bool:
    if key in KITCHEN_TIMERS:
        prefix = KITCHEN_TIMERS[key]
        return f"{prefix}_active" in data and f"{prefix}_end_time" in data
    if key not in data:
        return False
    if key.startswith(("cav_", "cav2_")):
        return key in WRITABLE_BOOLEAN_KEYS | WRITABLE_INTEGER_KEYS
    if key in {*DISHWASHER_SWITCHES, "wash_cycle_on", "delay_start_timer_duration"}:
        return is_dishwasher(data)
    return is_fridge(data) and key in {
        *SETPOINT_KEYS,
        *FRIDGE_ENUM_OPTIONS,
        *FRIDGE_MODE_KEYS,
        *ICE_KEYS,
        "air_filter_on",
        "accent_light_level",
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


def temperature_range(key: str, data: dict) -> tuple[int, int] | None:
    if key in {f"{prefix}_set_temp" for prefix in OVEN_PREFIXES}:
        return 85, 550
    if key == "frz_set_temp":
        return -5, 5
    if key == "crisp_set_temp":
        refrigerator = data.get("ref_set_temp")
        if not is_finite_number(refrigerator):
            return None
        lower, upper = max(34, refrigerator - 2), min(42, refrigerator + 2)
        return (math.ceil(lower), math.floor(upper)) if lower <= upper else None
    if key == "ref_set_temp":
        model = data.get("appliance_model", "")
        legacy = isinstance(model, str) and model.startswith(("BI", "IT", "IC", "ID"))
        return 34, 45 if legacy else 42
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
        elif key in FRIDGE_ENUM_OPTIONS:
            values = FRIDGE_ENUM_OPTIONS[key].values()
            if type(value) is not int or value not in values:
                raise ServiceValidationError("Select a supported appliance option.")
            if type(data[key]) is not int or data[key] not in values:
                raise ServiceValidationError("The current appliance setting is unknown.")
        elif key in KITCHEN_TIMERS:
            prefix = KITCHEN_TIMERS[key]
            if type(value) is not int or not 0 <= value <= 660:
                raise ServiceValidationError("Enter a timer duration from 0 to 660 minutes.")
            if type(data.get(f"{prefix}_active")) is not bool:
                raise ServiceValidationError("The timer state is unknown.")
        elif key == "accent_light_level" or key == "delay_start_timer_duration":
            maximum = 100 if key == "accent_light_level" else 12
            if type(value) is not int or not 0 <= value <= maximum:
                raise ServiceValidationError("The setting is outside the appliance's range.")
            if type(data[key]) is not int:
                raise ServiceValidationError("The current appliance setting is unknown.")
        elif key.endswith("_cook_mode"):
            if type(value) is not int or value not in COOK_MODES.values():
                raise ServiceValidationError("Select a supported cooking mode.")
            if type(data[key]) is not int or data[key] not in COOK_MODES.values():
                raise ServiceValidationError("The current cooking mode is unknown.")
            if value in MANUAL_COOK_MODES and data[key] != value:
                raise ServiceValidationError("Start this cooking mode at the oven's control panel.")
        else:
            if temperature_unit != "F":
                raise ServiceValidationError("Temperature controls require Fahrenheit in the app.")
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
