"""Dedicated ice-maker discovery, scheduling, and read-only cleaning state."""

import pytest
import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.subzero.api import ApiError, RateLimited
from custom_components.subzero.auth import InvalidAuth
from custom_components.subzero.const import DOMAIN
from custom_components.subzero.controls import is_ice_maker

ICE_STATE = {
    "appliance_model": "DEC1850CI",
    "appliance_type": "1.21.2.5",
    "ice_maker_on": True,
    "sabbath_on": False,
    "unit_on": True,
    "door_ajar_timeout": 5,
    "ice_door_ajar": False,
    "water_filter_inserted": True,
    "water_filter_pct_remaining": 80,
    "delay_active": False,
    "delay_duration": 0,
    "delay_start_offset": 0,
    "delay_recurring": False,
    "delay_start_time": None,
    "delay_end_time": None,
    "failsafe_on": False,
    "ice_maker_clean_stage": 50,
    "clean_soon_on": False,
    "clean_now_on": False,
    "next_clean_time": "2027-01-01T12:00:00-08:00",
    "next_clean_cycles": 100,
}
pytestmark = pytest.mark.parametrize("cloud_appliance", [ICE_STATE], indirect=True)


async def test_ice_maker_discovery(hass, cloud_appliance):
    assert hass.states.get("select.kitchen_ice_maker").attributes["options"] == ["Off", "On"]
    assert hass.states.get("select.kitchen_mode").attributes["options"] == ["Normal", "Sabbath"]
    assert hass.states.get("sensor.kitchen_ice_maker_cleaning_stage").state == "Not cleaning"
    assert hass.states.get("binary_sensor.kitchen_water_filter_inserted").state == "on"
    assert hass.states.get("binary_sensor.kitchen_ice_maker_door").state == "off"
    assert hass.states.get("sensor.kitchen_ice_maker_status").state == "On"
    assert not hass.states.async_entity_ids("climate")
    cloud_appliance.client.set_property.assert_not_called()
    cloud_appliance.client.set_ice_delay.assert_not_called()
    cloud_appliance.client.exit_ice_delay.assert_not_called()


async def test_ice_controls_and_cleaning_updates(hass, cloud_appliance):
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.kitchen_ice_maker", "option": "Off"},
        blocking=True,
    )
    cloud_appliance.client.set_property.assert_awaited_once_with("appliance", "ice_maker_on", False)
    await cloud_appliance.update(
        {"ice_door_ajar": True, "ice_maker_clean_stage": 65, "failsafe_on": True}
    )
    assert hass.states.get("binary_sensor.kitchen_ice_maker_door").state == "on"
    assert hass.states.get("sensor.kitchen_ice_maker_cleaning_stage").state == "Add sanitizer"
    assert hass.states.get("sensor.kitchen_ice_maker_status").state == "Disabled"
    for value in [True, "65", 999]:
        await cloud_appliance.update({"ice_maker_clean_stage": value})
        assert hass.states.get("sensor.kitchen_ice_maker_cleaning_stage").state == "unknown"
    with pytest.raises(ServiceValidationError):
        await cloud_appliance.coordinator.async_set_properties({"ice_maker_clean_on": True})


async def test_schedule_ice_delay_converts_units_and_refreshes(hass, cloud_appliance):
    await hass.services.async_call(
        DOMAIN,
        "schedule_ice_delay",
        {"device_id": cloud_appliance.device_id, "duration": 12, "start_in": 1439, "repeat": True},
        blocking=True,
    )
    cloud_appliance.client.set_ice_delay.assert_awaited_once_with("appliance", 43200, 86340, True)
    assert hass.states.get("sensor.kitchen_ice_delay_duration").state == "43200"
    assert hass.states.get("binary_sensor.kitchen_ice_delay_repeats_daily").state == "on"
    cloud_appliance.client.set_property.assert_not_called()


async def test_cancel_current_delay_preserves_repeat_schedule(hass, cloud_appliance):
    await cloud_appliance.update(
        {"delay_active": True, "delay_duration": 3600, "delay_recurring": True}
    )
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.kitchen_end_current_ice_delay"}, blocking=True
    )
    cloud_appliance.client.exit_ice_delay.assert_awaited_once_with("appliance")
    cloud_appliance.client.set_ice_delay.assert_not_called()
    assert hass.states.get("binary_sensor.kitchen_ice_delay_active").state == "off"
    assert hass.states.get("binary_sensor.kitchen_ice_delay_repeats_daily").state == "on"
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.kitchen_cancel_ice_delay_schedule"}, blocking=True
    )
    cloud_appliance.client.set_ice_delay.assert_awaited_once_with("appliance", 0, 0, False)


async def test_delay_rejection_does_not_change_reported_state(hass, cloud_appliance):
    cloud_appliance.client.set_ice_delay.side_effect = ApiError("Rejected")
    with pytest.raises(HomeAssistantError, match="Rejected"):
        await cloud_appliance.coordinator.async_set_ice_delay(3600)
    assert hass.states.get("sensor.kitchen_ice_delay_duration").state == "0"
    with pytest.raises(ServiceValidationError, match="no active"):
        await cloud_appliance.coordinator.async_set_ice_delay(end_current=True)
    cloud_appliance.client.exit_ice_delay.assert_not_called()


@pytest.mark.parametrize("error", [None, ApiError("Sub-Zero returned HTTP 503.")])
async def test_ending_delay_refreshes_after_acknowledgment_or_command_error(
    hass, cloud_appliance, error
):
    await cloud_appliance.update({"delay_active": True, "delay_duration": 3600})
    reads = cloud_appliance.client.state.await_count

    async def exit_delay(device_id):
        cloud_appliance.state["delay_active"] = False
        if error is not None:
            raise error
        return {"status": 0}

    cloud_appliance.client.exit_ice_delay.side_effect = exit_delay
    if error is None:
        await cloud_appliance.coordinator.async_set_ice_delay(end_current=True)
    else:
        with pytest.raises(HomeAssistantError, match="HTTP 503"):
            await cloud_appliance.coordinator.async_set_ice_delay(end_current=True)
    assert cloud_appliance.client.state.await_count == reads + 1
    assert hass.states.get("binary_sensor.kitchen_ice_delay_active").state == "off"


@pytest.mark.parametrize("error", [InvalidAuth("Expired"), RateLimited(60)])
async def test_ending_delay_stops_on_authentication_or_rate_limit(cloud_appliance, error):
    await cloud_appliance.update({"delay_active": True, "delay_duration": 3600})
    reads = cloud_appliance.client.state.await_count
    cloud_appliance.client.exit_ice_delay.side_effect = error
    with pytest.raises(HomeAssistantError):
        await cloud_appliance.coordinator.async_set_ice_delay(end_current=True)
    assert cloud_appliance.client.state.await_count == reads


async def test_ice_capabilities_are_checked_again_before_writing(hass, cloud_appliance):
    await cloud_appliance.update({"appliance_type": "23.255.255"})
    with pytest.raises(ServiceValidationError, match="does not report"):
        await cloud_appliance.coordinator.async_set_ice_delay(3600)
    assert hass.states.get("select.kitchen_ice_maker").state == "unavailable"
    cloud_appliance.client.set_ice_delay.assert_not_called()
    assert is_ice_maker({"appliance_type": "21.2.5"})
    assert not is_ice_maker({"ice_maker_on": True})


@pytest.mark.parametrize(
    "duration,offset,repeat",
    [(True, 0, False), (3601, 0, False), (3600, 86400, False), (3600, 0, 1)],
)
async def test_invalid_delays_never_write(cloud_appliance, duration, offset, repeat):
    with pytest.raises(ServiceValidationError):
        await cloud_appliance.coordinator.async_set_ice_delay(duration, offset, repeat)
    cloud_appliance.client.set_ice_delay.assert_not_called()


@pytest.mark.parametrize(("duration", "start_in"), [(3.0, "5"), ("3", 5.0)])
async def test_schedule_service_accepts_whole_numbers_from_templates(
    hass, cloud_appliance, duration, start_in
):
    await hass.services.async_call(
        DOMAIN,
        "schedule_ice_delay",
        {"device_id": cloud_appliance.device_id, "duration": duration, "start_in": start_in},
        blocking=True,
    )
    cloud_appliance.client.set_ice_delay.assert_awaited_once_with("appliance", 10800, 300, False)


@pytest.mark.parametrize("duration", [True, 1.5, "soon", None])
async def test_schedule_service_rejects_other_durations(hass, cloud_appliance, duration):
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "schedule_ice_delay",
            {"device_id": cloud_appliance.device_id, "duration": duration},
            blocking=True,
        )
    cloud_appliance.client.set_ice_delay.assert_not_called()


async def test_schedule_service_rejects_an_unknown_device(hass, cloud_appliance):
    with pytest.raises(ServiceValidationError, match="Select a connected"):
        await hass.services.async_call(
            DOMAIN,
            "schedule_ice_delay",
            {"device_id": "not-this-device", "duration": 2},
            blocking=True,
        )
    cloud_appliance.client.set_ice_delay.assert_not_called()
