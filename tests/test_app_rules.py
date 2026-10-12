"""Readings and names that follow the Sub-Zero app's own rules."""

import pytest
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

CAVITY = {
    "temp": 75,
    "set_temp": 350,
    "unit_on": False,
    "cook_mode": 1,
    "light_on": False,
    "door_ajar": False,
    "remote_ready": True,
    "probe_on": False,
    "probe_temp": 0,
    "probe_set_temp": 0,
    "mode_change_enabled": True,
}
SINGLE_RANGE = {
    "appliance_model": "DF48850SP",
    "appliance_type": "17.8.2.0",
    **{f"cav_{key}": value for key, value in CAVITY.items()},
}
RANGE = {**SINGLE_RANGE, **{f"cav2_{key}": value for key, value in CAVITY.items()}}
DUAL_ZONE_WINE = {
    "appliance_model": "DEU2450WDZ",
    "appliance_type": "17.14.2.0",
    "wine_set_temp": 40,
    "wine2_set_temp": 55,
    "wine_door_ajar": False,
    "wine_temp_alert_on": False,
}
SINGLE_ZONE_WINE = {
    "appliance_model": "DEU2450W",
    "appliance_type": "17.14.1.0",
    "wine_set_temp": 55,
}


@pytest.fixture(autouse=True)
def fahrenheit(hass):
    hass.config.units = US_CUSTOMARY_SYSTEM


def state(hass, entity_id):
    return hass.states.get(entity_id).state


def friendly_name(hass, entity_id):
    return hass.states.get(entity_id).attributes["friendly_name"]


@pytest.mark.parametrize("cloud_appliance", [RANGE], indirect=True)
async def test_a_range_names_its_cavities_right_and_left(hass, cloud_appliance):
    for entity_id, name in (
        ("climate.kitchen_right_oven", "Right oven"),
        ("climate.kitchen_left_oven", "Left oven"),
        ("sensor.kitchen_right_oven_temperature", "Right oven temperature"),
        ("sensor.kitchen_left_oven_temperature", "Left oven temperature"),
        ("select.kitchen_right_oven_cooking_mode", "Right oven cooking mode"),
        ("switch.kitchen_left_oven_light", "Left oven light"),
        ("button.kitchen_start_right_oven", "Start right oven"),
        ("binary_sensor.kitchen_right_oven_door", "Right oven door"),
        ("sensor.kitchen_left_oven_broil_level", "Left oven broil level"),
    ):
        assert friendly_name(hass, entity_id) == f"Kitchen {name}"


@pytest.mark.parametrize("cloud_appliance", [SINGLE_RANGE], indirect=True)
async def test_a_second_cavity_reported_later_renames_the_first(hass, cloud_appliance):
    assert friendly_name(hass, "climate.kitchen_oven") == "Kitchen Oven"
    assert friendly_name(hass, "sensor.kitchen_cooking_mode") == "Kitchen Cooking mode"
    # Like the app, the second cavity's setpoint shows that there are two.
    await cloud_appliance.update({"cav2_door_ajar": False})
    assert friendly_name(hass, "climate.kitchen_oven") == "Kitchen Oven"
    await cloud_appliance.update({f"cav2_{key}": value for key, value in CAVITY.items()})
    # Renaming keeps the first cavity's entity IDs.
    for entity_id, name in (
        ("climate.kitchen_oven", "Right oven"),
        ("sensor.kitchen_cooking_mode", "Right oven cooking mode"),
        ("button.kitchen_start_oven", "Start right oven"),
        ("climate.kitchen_left_oven", "Left oven"),
        ("sensor.kitchen_left_oven_cooking_mode", "Left oven cooking mode"),
    ):
        assert friendly_name(hass, entity_id) == f"Kitchen {name}"


@pytest.mark.parametrize("cloud_appliance", [RANGE], indirect=True)
async def test_a_probe_temperature_of_one_is_no_reading(hass, cloud_appliance):
    entity_id = "sensor.kitchen_right_oven_probe_temperature"
    await cloud_appliance.update({"cav_probe_on": True, "cav_probe_temp": 1})
    assert state(hass, entity_id) == "unknown"
    await cloud_appliance.update({"cav_probe_temp": 120})
    assert state(hass, entity_id) == "120"


@pytest.mark.parametrize("cloud_appliance", [RANGE], indirect=True)
async def test_broil_level_follows_the_broil_setpoint(hass, cloud_appliance):
    entity_id = "sensor.kitchen_right_oven_broil_level"
    assert state(hass, entity_id) == "unknown"
    await cloud_appliance.update({"cav_unit_on": True, "cav_cook_mode": 3})
    for temperature, level in ((350, "Low"), (400, "Medium"), (499, "Medium"), (500, "High")):
        await cloud_appliance.update({"cav_set_temp": temperature})
        assert state(hass, entity_id) == level
    await cloud_appliance.update({"cav_cook_mode": 1})
    assert state(hass, entity_id) == "unknown"
    assert hass.states.get(entity_id).attributes["options"] == ["Low", "Medium", "High"]


@pytest.mark.parametrize("cloud_appliance", [DUAL_ZONE_WINE], indirect=True)
async def test_two_wine_zones_are_upper_and_lower(hass, cloud_appliance):
    for entity_id, name in (
        ("climate.kitchen_upper_wine", "Upper wine"),
        ("climate.kitchen_lower_wine", "Lower wine"),
        ("sensor.kitchen_upper_wine_setpoint", "Upper wine setpoint"),
        ("number.kitchen_lower_wine_setpoint", "Lower wine setpoint"),
        # One door serves both zones.
        ("binary_sensor.kitchen_wine_storage_door", "Wine storage door"),
        ("binary_sensor.kitchen_wine_temperature_alert", "Wine temperature alert"),
    ):
        assert friendly_name(hass, entity_id) == f"Kitchen {name}"


@pytest.mark.parametrize("cloud_appliance", [SINGLE_ZONE_WINE], indirect=True)
async def test_a_single_wine_zone_keeps_its_name(hass, cloud_appliance):
    assert friendly_name(hass, "climate.kitchen_wine") == "Kitchen Wine"


@pytest.mark.parametrize("cloud_appliance", [SINGLE_ZONE_WINE], indirect=True)
async def test_a_second_wine_zone_reported_later_renames_the_first(hass, cloud_appliance):
    await cloud_appliance.update({"wine2_set_temp": 45})
    assert friendly_name(hass, "climate.kitchen_wine") == "Kitchen Upper wine"
    assert friendly_name(hass, "number.kitchen_wine_setpoint") == "Kitchen Upper wine setpoint"
    assert friendly_name(hass, "climate.kitchen_lower_wine") == "Kitchen Lower wine"
    # A snapshot from a different model replaces the state, dropping the second zone.
    await cloud_appliance.update({**SINGLE_ZONE_WINE, "appliance_model": "DEU2450R"}, full=True)
    assert friendly_name(hass, "climate.kitchen_wine") == "Kitchen Wine"
