"""Fridge capabilities and temperature limits."""

import math

from homeassistant.exceptions import ServiceValidationError

from .const import FRIDGE_ENUM_OPTIONS, SETPOINT_KEYS, WRITABLE_BOOLEAN_KEYS, WRITABLE_INTEGER_KEYS


def is_fridge(data: dict) -> bool:
    return bool(SETPOINT_KEYS.intersection(data))


def temperature_range(key: str, data: dict) -> tuple[int, int] | None:
    if key == "frz_set_temp":
        return -5, 5
    if key == "crisp_set_temp":
        refrigerator = data.get("ref_set_temp")
        if type(refrigerator) not in (int, float) or not math.isfinite(refrigerator):
            return None
        lower, upper = max(34, refrigerator - 2), min(42, refrigerator + 2)
        return (math.ceil(lower), math.floor(upper)) if lower <= upper else None
    if key == "ref_set_temp":
        model = data.get("appliance_model", "")
        legacy = isinstance(model, str) and model.startswith(("BI", "IT", "IC", "ID"))
        return 34, 45 if legacy else 42
    return None


def validate_control_properties(data: dict, temperature_unit: str | None, properties: dict) -> None:
    if not is_fridge(data):
        raise ServiceValidationError("Controls require a refrigerator or freezer.")
    if not properties:
        raise ServiceValidationError("No settings supplied.")
    for key, value in properties.items():
        if key not in WRITABLE_BOOLEAN_KEYS | WRITABLE_INTEGER_KEYS or key not in data:
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
        else:
            if temperature_unit != "F":
                raise ServiceValidationError("Temperature controls require Fahrenheit in the app.")
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
