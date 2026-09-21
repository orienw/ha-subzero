"""Hood controls through Home Assistant fan, light, and setting services."""

from unittest.mock import call

import pytest
from homeassistant.components.fan import FanEntityFeature
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr

HOOD_STATE = {
    "appliance_model": "TEST-HOOD",
    "appliance_type": "23.255.255",
    "fan_on": False,
    "fan_speed": 1,
    "light_on": False,
    "light_percent": 25,
    "color_level": 50,
    "halo_max_percent": 0,
    "auto_sensivity": -1,
    "delay_off_duration": 600000,
    "delay_enabled": False,
    "key_tone_on": True,
    "user_lock_on": False,
    "filter_count": 36000,
    "filter_max_count": 360000,
}
pytestmark = pytest.mark.parametrize("cloud_appliance", [HOOD_STATE], indirect=True)


async def test_hood_discovery_and_units(hass, cloud_appliance):
    assert hass.states.get("fan.kitchen_fan").state == "off"
    assert hass.states.get("fan.kitchen_fan").attributes["percentage_step"] == 25
    assert hass.states.get("light.kitchen_task_light").attributes["supported_color_modes"] == [
        "color_temp"
    ]
    assert hass.states.get("select.kitchen_automatic_fan_sensitivity").state == "Off"
    assert hass.states.get("number.kitchen_delayed_shutoff_duration").state == "10.0"
    assert hass.states.get("sensor.kitchen_hood_filter_usage").state == "36000"
    assert (
        hass.states.get("sensor.kitchen_hood_filter_usage").attributes["unit_of_measurement"] == "s"
    )
    assert dr.async_get(hass).async_get(cloud_appliance.device_id).manufacturer == "Wolf"
    cloud_appliance.client.set_property.assert_not_called()


async def test_fan_services_use_four_speeds_and_separate_power(hass, cloud_appliance):
    await hass.services.async_call(
        "fan", "turn_on", {"entity_id": "fan.kitchen_fan", "percentage": 75}, blocking=True
    )
    assert cloud_appliance.client.set_property.await_args_list == [
        call("appliance", "fan_speed", 3),
        call("appliance", "fan_on", True),
    ]
    assert hass.states.get("fan.kitchen_fan").attributes["percentage"] == 75
    cloud_appliance.client.set_property.reset_mock()
    await hass.services.async_call(
        "fan", "set_percentage", {"entity_id": "fan.kitchen_fan", "percentage": 100}, blocking=True
    )
    cloud_appliance.client.set_property.assert_awaited_once_with("appliance", "fan_speed", 4)
    await hass.services.async_call(
        "fan", "turn_off", {"entity_id": "fan.kitchen_fan"}, blocking=True
    )
    assert hass.states.get("fan.kitchen_fan").state == "off"


async def test_zero_fan_percentage_turns_off_power(hass, cloud_appliance):
    await cloud_appliance.update({"fan_on": True, "fan_speed": 3})
    await hass.services.async_call(
        "fan", "set_percentage", {"entity_id": "fan.kitchen_fan", "percentage": 0}, blocking=True
    )
    cloud_appliance.client.set_property.assert_awaited_once_with("appliance", "fan_on", False)
    assert hass.states.get("fan.kitchen_fan").state == "off"


async def test_task_light_uses_percent_and_color_level(hass, cloud_appliance):
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": "light.kitchen_task_light", "brightness": 1, "color_temp_kelvin": 5000},
        blocking=True,
    )
    assert cloud_appliance.client.set_property.await_args_list == [
        call("appliance", "light_percent", 5),
        call("appliance", "color_level", 100),
        call("appliance", "light_on", True),
    ]
    light = hass.states.get("light.kitchen_task_light")
    assert light.attributes["brightness"] == 13
    assert light.attributes["color_temp_kelvin"] == 5000
    await hass.services.async_call(
        "light", "turn_off", {"entity_id": "light.kitchen_task_light"}, blocking=True
    )
    assert hass.states.get("light.kitchen_task_light").state == "off"


async def test_halo_is_a_switch_with_the_app_on_value(hass, cloud_appliance):
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.kitchen_halo_light"}, blocking=True
    )
    cloud_appliance.client.set_property.assert_awaited_once_with(
        "appliance", "halo_max_percent", 30
    )
    await cloud_appliance.update({"halo_max_percent": 60})
    assert hass.states.get("switch.kitchen_halo_light").state == "on"
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.kitchen_halo_light"}, blocking=True
    )
    assert cloud_appliance.client.set_property.await_args == call(
        "appliance", "halo_max_percent", 0
    )


async def test_hood_settings_convert_delay_and_off_sensitivity(hass, cloud_appliance):
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.kitchen_delayed_shutoff_duration", "value": 719},
        blocking=True,
    )
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.kitchen_automatic_fan_sensitivity", "option": "Low"},
        blocking=True,
    )
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.kitchen_automatic_fan_sensitivity", "option": "Off"},
        blocking=True,
    )
    for name in ["delayed_shutoff", "control_lock"]:
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": f"switch.kitchen_{name}"}, blocking=True
        )
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.kitchen_button_tones"}, blocking=True
    )
    assert cloud_appliance.client.set_property.await_args_list == [
        call("appliance", "delay_off_duration", 43140000),
        call("appliance", "auto_sensivity", 0),
        call("appliance", "auto_sensivity", -1),
        call("appliance", "delay_enabled", True),
        call("appliance", "user_lock_on", True),
        call("appliance", "key_tone_on", False),
    ]


async def test_missing_hood_features_are_not_advertised(hass, cloud_appliance):
    await cloud_appliance.update(
        {
            "appliance_model": "BASIC-HOOD",
            "appliance_type": "23.255.255",
            "fan_on": False,
            "light_on": False,
        },
        full=True,
    )
    assert (
        hass.states.get("fan.kitchen_fan").attributes["supported_features"]
        == FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
    )
    assert hass.states.get("light.kitchen_task_light").attributes["supported_color_modes"] == [
        "onoff"
    ]
    assert hass.states.get("switch.kitchen_halo_light").state == "unavailable"
    with pytest.raises(ServiceValidationError):
        await cloud_appliance.coordinator.async_set_properties({"fan_speed": 2})
    cloud_appliance.client.set_property.assert_not_called()


@pytest.mark.parametrize(
    "key,value",
    [
        ("fan_speed", 5),
        ("light_percent", 4),
        ("color_level", 5000),
        ("halo_max_percent", 15),
        ("auto_sensivity", 3),
        ("delay_off_duration", 1),
        ("fan_speed", True),
        ("fan_on", 1),
    ],
)
async def test_hood_rejects_invalid_wire_values(cloud_appliance, key, value):
    with pytest.raises(ServiceValidationError):
        await cloud_appliance.coordinator.async_set_properties({key: value})
    cloud_appliance.client.set_property.assert_not_called()


async def test_hood_controls_require_recognized_family(hass, cloud_appliance):
    await cloud_appliance.update({"appliance_type": "1.15.2.5"})
    assert hass.states.get("fan.kitchen_fan").state == "unavailable"
    assert hass.states.get("light.kitchen_task_light").state == "unavailable"
    with pytest.raises(ServiceValidationError):
        await cloud_appliance.coordinator.async_set_properties({"fan_on": True})
    cloud_appliance.client.set_property.assert_not_called()
