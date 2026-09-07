"""Cloud feature coverage through Home Assistant services and appliance updates."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.subzero.api import ApiError, Appliance, StateUpdate, token_state
from custom_components.subzero.const import DOMAIN
from custom_components.subzero.diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)
from custom_components.subzero.number import DESCRIPTIONS as NUMBER_DESCRIPTIONS
from custom_components.subzero.number import SubZeroNumber
from custom_components.subzero.sensor import DESCRIPTIONS as SENSOR_DESCRIPTIONS
from custom_components.subzero.sensor import SubZeroSensor

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture
async def appliances(hass, tokens, request):
    hass.config.units = US_CUSTOMARY_SYSTEM
    unit = getattr(request, "param", "F")
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Account",
        data={
            "tokens": token_state(tokens),
            "devices": {
                "fridge": {"name": "Fridge", "temperature_unit": unit},
                "oven": {"name": "Oven", "temperature_unit": unit},
                "dishwasher": {"name": "Dishwasher", "temperature_unit": unit},
            },
        },
    )
    entry.add_to_hass(hass)
    states = {
        "fridge": {
            "appliance_model": "BI-36U",
            "ref_set_temp": 38,
            "frz_set_temp": 0,
            "ref_display_temp": 37,
            "water_filter_gal_remaining": -10,
            "accent_light_level": 0,
            "max_ice_on": False,
            "max_ice_start_time": None,
            "max_ice_end_time": None,
            "high_use_start_time": None,
            "high_use_end_time": None,
            "uptime": "435:53:58",
            "ipv4_addr": "192.0.2.1",
            "device_wlan_id": "001122334455",
            "ap_ssid": "private-network",
            "appliance_serial": "private-serial",
            "remote_svc_reg_token": "private-registration",
        },
        "oven": {
            "appliance_model": "DO30PM",
            "appliance_type": "1.4.2.3",
            **{
                f"{prefix}_{key}": value
                for prefix in ("cav", "cav2")
                for key, value in {
                    "temp": 75,
                    "set_temp": 350,
                    "unit_on": False,
                    "cook_mode": 1,
                    "light_on": False,
                    "door_ajar": False,
                    "remote_ready": False,
                    "mode_change_enabled": True,
                    "probe_on": False,
                    "probe_temp": 0,
                    "probe_set_temp": 0,
                    "cook_timer_active": False,
                    "cook_timer_complete": False,
                    "cook_timer_start_time": None,
                    "cook_timer_end_time": None,
                }.items()
            },
            **{
                f"{prefix}_{key}": value
                for prefix in ("kitchen_timer", "kitchen_timer2")
                for key, value in {
                    "active": False,
                    "complete": False,
                    "start_time": None,
                    "end_time": None,
                }.items()
            },
        },
        "dishwasher": {
            "appliance_model": "DW2450WS",
            "appliance_type": "17.6.1.1",
            "wash_cycle": 2,
            "wash_status": 0,
            "wash_cycle_on": False,
            "wash_cycle_end_time": None,
            "door_ajar": False,
            "remote_ready": False,
            "rinse_aid_low": False,
            "softener_low": True,
            "service_required": False,
            "heated_dry_on": False,
            "extended_dry_on": False,
            "high_temp_wash_on": False,
            "sani_rinse_on": False,
            "top_rack_only_on": False,
            "delay_start_timer_duration": 0,
            "delay_start_timer_active": False,
            "delay_start_timer_start_time": None,
            "delay_start_timer_end_time": None,
        },
    }
    updates = asyncio.Queue()
    behavior = {"accept": True, "push": True}

    async def update(device_id, properties, *, full=False):
        if full:
            states[device_id] = dict(properties)
        else:
            states[device_id].update(properties)
        await updates.put((device_id, StateUpdate(properties, full=full)))
        await hass.async_block_till_done()

    async def watch(device_ids):
        while True:
            yield await updates.get()

    async def write(device_id, key, value):
        if not behavior["accept"]:
            return
        if key in {"kitchen_timer_duration", "kitchen_timer2_duration"}:
            prefix = key.removesuffix("_duration")
            start = dt_util.utcnow()
            properties = {
                f"{prefix}_active": value > 0,
                f"{prefix}_start_time": start.isoformat() if value else None,
                f"{prefix}_end_time": (start + timedelta(minutes=value)).isoformat()
                if value
                else None,
            }
        else:
            properties = {key: value}
            if key.endswith("_unit_on"):
                properties[key.replace("unit_on", "remote_ready")] = False
            if key == "wash_cycle_on" and value:
                properties.update(remote_ready=False, wash_status=2)
        states[device_id].update(properties)
        if behavior["push"]:
            await updates.put((device_id, StateUpdate(properties, full=False)))

    with (
        patch("custom_components.subzero.SubZeroClient") as factory,
        patch("custom_components.subzero.coordinator.CONTROL_CONFIRM_TIMEOUT", 0.02),
    ):
        client = factory.return_value
        client.tokens = token_state(tokens)
        client.notification_stats = {
            "received": 0,
            "ignored": 0,
            "invalid": 0,
            "last_received": None,
        }
        client.appliances = AsyncMock(
            return_value=[
                Appliance(device_id, device["name"], unit)
                for device_id, device in entry.data["devices"].items()
            ]
        )
        client.push_connected = True
        client.state = AsyncMock(side_effect=lambda device_id: dict(states[device_id]))
        client.set_property = AsyncMock(side_effect=write)
        client.watch = watch
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield SimpleNamespace(
            entry=entry,
            client=client,
            states=states,
            update=update,
            behavior=behavior,
            updates=updates,
        )
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_cloud_feature_discovery_does_not_send_controls(hass, appliances):
    assert hass.states.get("sensor.fridge_water_filter_capacity_remaining").state == "-10"
    assert hass.states.get("sensor.fridge_refrigerator_display_temperature").state == "37"
    assert hass.states.get("climate.fridge_refrigerator").attributes["current_temperature"] == 37
    assert hass.states.get("climate.fridge_freezer").attributes["current_temperature"] is None
    assert hass.states.get("climate.oven_lower_oven").state == "off"
    assert hass.states.get("sensor.oven_lower_oven_temperature").state == "75"
    assert hass.states.get("sensor.dishwasher_wash_cycle").state == "Normal"
    assert hass.states.get("sensor.dishwasher_wash_status").state == "Idle"
    assert hass.states.get("binary_sensor.dishwasher_softener_salt_low").state == "on"
    assert hass.states.get("sensor.dishwasher_wash_cycle_end").state == "unknown"
    assert hass.states.get("button.dishwasher_start_wash_cycle").state == "unavailable"
    assert hass.states.get("number.oven_kitchen_timer_duration").state == "0"
    registry = er.async_get(hass)
    for entity_id in (
        "number.fridge_accent_light",
        "sensor.fridge_ip_address",
        "sensor.fridge_mac_address",
        "sensor.fridge_uptime",
    ):
        assert registry.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "dishwasher"), appliances.entry.entry_id
    )
    assert device.manufacturer == "Cove"
    appliances.client.set_property.assert_not_called()


@pytest.mark.parametrize(
    ("entity_id", "device", "key"),
    [
        ("switch.oven_oven_light", "oven", "cav_light_on"),
        ("switch.oven_lower_oven_light", "oven", "cav2_light_on"),
        ("switch.dishwasher_heated_dry", "dishwasher", "heated_dry_on"),
        ("switch.dishwasher_extended_dry", "dishwasher", "extended_dry_on"),
        ("switch.dishwasher_high_temperature_wash", "dishwasher", "high_temp_wash_on"),
        ("switch.dishwasher_sanitize_rinse", "dishwasher", "sani_rinse_on"),
        ("switch.dishwasher_top_rack_only", "dishwasher", "top_rack_only_on"),
    ],
)
async def test_switches_send_only_the_selected_appliance_property(
    hass, appliances, entity_id, device, key
):
    for service, value, expected in (("turn_on", True, "on"), ("turn_off", False, "off")):
        await hass.services.async_call("switch", service, {"entity_id": entity_id}, blocking=True)
        appliances.client.set_property.assert_awaited_with(device, key, value)
        assert hass.states.get(entity_id).state == expected
    assert appliances.client.set_property.await_count == 2


@pytest.mark.parametrize(
    ("device", "key", "entity_id", "ready"),
    [
        ("oven", "cav_unit_on", "button.oven_start_oven", "cav_remote_ready"),
        ("oven", "cav2_unit_on", "button.oven_start_lower_oven", "cav2_remote_ready"),
        ("dishwasher", "wash_cycle_on", "button.dishwasher_start_wash_cycle", "remote_ready"),
    ],
)
async def test_remote_start_consumes_remote_ready(hass, appliances, device, key, entity_id, ready):
    coordinator = appliances.entry.runtime_data.coordinators[device]
    with pytest.raises(ServiceValidationError, match="Remote Ready"):
        await coordinator.async_set_properties({key: True})
    appliances.client.set_property.assert_not_called()
    await appliances.update(device, {ready: True})
    assert hass.states.get(entity_id).state != "unavailable"
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
    appliances.client.set_property.assert_awaited_once_with(device, key, True)
    assert coordinator.data[key] is True
    assert coordinator.data[ready] is False
    assert hass.states.get(entity_id).state == "unavailable"


async def test_lower_oven_controls_leave_the_upper_oven_alone(hass, appliances):
    await appliances.update("oven", {"cav2_remote_ready": True})
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {
            "entity_id": "climate.oven_lower_oven",
            "temperature": 375,
        },
        blocking=True,
    )
    assert appliances.states["oven"]["cav2_unit_on"] is False
    await hass.services.async_call(
        "select",
        "select_option",
        {
            "entity_id": "select.oven_lower_oven_cooking_mode",
            "option": "Roast",
        },
        blocking=True,
    )
    await hass.services.async_call(
        "climate", "turn_on", {"entity_id": "climate.oven_lower_oven"}, blocking=True
    )
    await hass.services.async_call(
        "select",
        "select_option",
        {
            "entity_id": "select.oven_lower_oven_cooking_mode",
            "option": "Off",
        },
        blocking=True,
    )
    assert appliances.client.set_property.await_args_list == [
        call("oven", "cav2_set_temp", 375),
        call("oven", "cav2_cook_mode", 2),
        call("oven", "cav2_unit_on", True),
        call("oven", "cav2_unit_on", False),
    ]
    assert hass.states.get("climate.oven_lower_oven").state == "off"
    assert appliances.states["oven"]["cav_set_temp"] == 350
    assert appliances.states["oven"]["cav_cook_mode"] == 1


@pytest.mark.parametrize(
    ("properties", "key", "value", "message"),
    [
        ({"cav2_remote_ready": True}, "cav_unit_on", True, "Remote Ready"),
        ({"cav_remote_ready": True, "cav_door_ajar": True}, "cav_unit_on", True, "door"),
        ({"cav_remote_ready": 1}, "cav_unit_on", True, "Remote Ready"),
        ({}, "cav_set_temp", 375, "running"),
        ({"cav_remote_ready": True}, "cav_set_temp", 551, "range"),
        ({"cav_remote_ready": True}, "cav_set_temp", 84, "range"),
        (
            {"cav_remote_ready": True, "cav_mode_change_enabled": False},
            "cav_cook_mode",
            2,
            "mode changes",
        ),
        ({"cav_remote_ready": True}, "cav_cook_mode", 11, "control panel"),
        ({"cav_remote_ready": True, "cav_cook_mode": 3}, "cav_unit_on", True, "control panel"),
        (
            {"cav_remote_ready": True, "cav_cook_mode": 999},
            "cav_unit_on",
            True,
            "supported cooking mode",
        ),
    ],
)
async def test_oven_interlocks_apply_below_the_entity_layer(
    appliances, properties, key, value, message
):
    await appliances.update("oven", properties)
    with pytest.raises(ServiceValidationError, match=message):
        await appliances.entry.runtime_data.coordinators["oven"].async_set_properties({key: value})
    appliances.client.set_property.assert_not_called()


async def test_queued_start_rechecks_remote_ready(appliances):
    await appliances.update("oven", {"cav_remote_ready": True})
    coordinator = appliances.entry.runtime_data.coordinators["oven"]
    async with coordinator._command_lock:
        task = asyncio.create_task(coordinator.async_set_properties({"cav_unit_on": True}))
        await appliances.update("oven", {"cav_remote_ready": False})
    with pytest.raises(ServiceValidationError, match="Remote Ready"):
        await task
    appliances.client.set_property.assert_not_called()


async def test_turning_off_an_armed_idle_oven_cancels_remote_ready(hass, appliances):
    await appliances.update("oven", {"cav_remote_ready": True})
    assert hass.states.get("climate.oven_oven").state == "off"
    await hass.services.async_call(
        "climate", "turn_off", {"entity_id": "climate.oven_oven"}, blocking=True
    )
    appliances.client.set_property.assert_awaited_once_with("oven", "cav_unit_on", False)
    assert appliances.states["oven"]["cav_remote_ready"] is False


async def test_off_ack_without_canceling_remote_ready_is_not_success(hass, appliances):
    await appliances.update("oven", {"cav_remote_ready": True})
    appliances.behavior["accept"] = False
    with pytest.raises(HomeAssistantError, match="did not confirm"):
        await hass.services.async_call(
            "climate", "turn_off", {"entity_id": "climate.oven_oven"}, blocking=True
        )
    assert appliances.states["oven"]["cav_remote_ready"] is True
    assert appliances.client.set_property.await_count == 1


@pytest.mark.parametrize(
    ("key", "entity_id"),
    [
        ("kitchen_timer_duration", "number.oven_kitchen_timer_duration"),
        ("kitchen_timer2_duration", "number.oven_kitchen_timer_2_duration"),
    ],
)
@pytest.mark.parametrize("push", [True, False])
async def test_timer_duration_is_confirmed_from_timer_state(hass, appliances, key, entity_id, push):
    appliances.behavior["push"] = push
    reads = appliances.client.state.await_count
    for minutes in (15, 15, 0):
        await hass.services.async_call(
            "number", "set_value", {"entity_id": entity_id, "value": minutes}, blocking=True
        )
        assert hass.states.get(entity_id).state == str(minutes)
    assert key not in appliances.states["oven"]
    assert appliances.client.set_property.await_args_list == [
        call("oven", key, 15),
        call("oven", key, 15),
        call("oven", key, 0),
    ]
    assert appliances.client.state.await_count == reads + (0 if push else 3)


@pytest.mark.parametrize("previous_minutes", [16, 45])
async def test_timer_ack_without_correct_end_time_is_not_success(
    hass, appliances, previous_minutes
):
    appliances.behavior["accept"] = False
    await appliances.update(
        "oven",
        {
            "kitchen_timer_active": True,
            "kitchen_timer_start_time": dt_util.utcnow().isoformat(),
            "kitchen_timer_end_time": (
                dt_util.utcnow() + timedelta(minutes=previous_minutes)
            ).isoformat(),
        },
    )
    with pytest.raises(HomeAssistantError, match="did not confirm"):
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": "number.oven_kitchen_timer_duration", "value": 15},
            blocking=True,
        )
    assert hass.states.get("number.oven_kitchen_timer_duration").state == str(previous_minutes)
    assert appliances.client.set_property.await_count == 1


@pytest.mark.parametrize(
    ("device", "key", "value"),
    [
        ("oven", "kitchen_timer_duration", -1),
        ("oven", "kitchen_timer2_duration", 661),
        ("oven", "kitchen_timer_duration", True),
        ("fridge", "accent_light_level", 101),
        ("fridge", "accent_light_level", -1),
        ("dishwasher", "delay_start_timer_duration", 13),
        ("dishwasher", "delay_start_timer_duration", 0.5),
    ],
)
async def test_invalid_new_control_values_never_reach_cloud(appliances, device, key, value):
    with pytest.raises(ServiceValidationError):
        await appliances.entry.runtime_data.coordinators[device].async_set_properties({key: value})
    appliances.client.set_property.assert_not_called()


async def test_dishwasher_delay_start_then_completion_updates(hass, appliances):
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.dishwasher_delay_start", "option": "12 hours"},
        blocking=True,
    )
    appliances.client.set_property.assert_awaited_once_with(
        "dishwasher", "delay_start_timer_duration", 12
    )
    assert hass.states.get("select.dishwasher_delay_start").state == "12 hours"
    await appliances.update(
        "dishwasher",
        {
            "wash_status": 6,
            "wash_cycle_on": False,
            "wash_cycle_end_time": "2026-09-05T17:00:00Z",
            "rinse_aid_low": True,
        },
    )
    assert hass.states.get("sensor.dishwasher_wash_status").state == "Complete"
    assert hass.states.get("sensor.dishwasher_wash_cycle_end").state == "2026-09-05T17:00:00+00:00"
    assert hass.states.get("binary_sensor.dishwasher_rinse_aid_low").state == "on"


async def test_unknown_enum_values_do_not_become_known_modes(hass, appliances):
    await appliances.update(
        "dishwasher", {"wash_cycle": True, "wash_status": 900, "delay_start_timer_duration": 13}
    )
    await appliances.update("oven", {"cav_cook_mode": 100})
    assert hass.states.get("sensor.dishwasher_wash_cycle").state == "unknown"
    assert hass.states.get("sensor.dishwasher_wash_status").state == "unknown"
    assert hass.states.get("select.dishwasher_delay_start").state == "unavailable"
    assert hass.states.get("select.oven_cooking_mode").state == "unavailable"


async def test_naive_timestamps_need_the_appliance_clock_offset(hass, appliances):
    await appliances.update("fridge", {"max_ice_start_time": "2026-09-05T10:00:00"})
    assert hass.states.get("sensor.fridge_max_ice_start").state == "unknown"
    await appliances.update("fridge", {"time": "2026-09-05T11:00:00-07:00"})
    assert hass.states.get("sensor.fridge_max_ice_start").state == "2026-09-05T17:00:00+00:00"
    await appliances.update("fridge", {"max_ice_start_time": None})
    assert hass.states.get("sensor.fridge_max_ice_start").state == "unknown"


async def test_full_snapshot_marks_missing_controls_unavailable(hass, appliances):
    await appliances.update(
        "oven", {"appliance_model": "SINGLE-OVEN", "cav_light_on": False}, full=True
    )
    for entity_id in (
        "climate.oven_lower_oven",
        "select.oven_lower_oven_cooking_mode",
        "switch.oven_lower_oven_light",
        "button.oven_start_lower_oven",
        "number.oven_kitchen_timer_duration",
    ):
        assert hass.states.get(entity_id).state == "unavailable"
    assert hass.states.get("switch.oven_oven_light").state == "off"
    assert hass.states.get("sensor.dishwasher_wash_status").state == "Idle"


@pytest.mark.parametrize("appliances", ["C", None], indirect=True)
async def test_non_fahrenheit_units_omit_temperature_entities(hass, appliances):
    assert hass.states.get("climate.oven_oven") is None
    assert hass.states.get("climate.fridge_refrigerator") is None
    assert hass.states.get("select.oven_cooking_mode").state == "Bake"
    assert hass.states.get("number.oven_kitchen_timer_duration").state == "0"
    assert hass.states.get("switch.dishwasher_heated_dry").state == "off"


async def test_climate_writes_convert_celsius_to_fahrenheit(hass, appliances):
    hass.config.units = METRIC_SYSTEM
    await appliances.update("oven", {"cav_unit_on": True})
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": "climate.oven_oven", "temperature": 200},
        blocking=True,
    )
    appliances.client.set_property.assert_awaited_once_with("oven", "cav_set_temp", 392)


async def test_diagnostics_omit_private_values(hass, appliances):
    result = await async_get_config_entry_diagnostics(hass, appliances.entry)
    assert result["notifications"]["received"] == 0
    appliances.entry.runtime_data.client.notification_stats["received"] = 3
    assert result["notifications"]["received"] == 0
    encoded = json.dumps(result)
    assert result["push_connected"] is True
    assert "DO30PM" in encoded and "DW2450WS" in encoded
    for private in (
        "192.0.2.1",
        "001122334455",
        "private-network",
        "private-serial",
        "private-registration",
        "test-owner",
        "test-refresh",
        "Dishwasher",
    ):
        assert private not in encoded
    appliances.client.state.assert_has_awaits([call("fridge"), call("oven"), call("dishwasher")])
    assert appliances.client.state.await_count == 3


async def test_diagnostics_list_unrecognized_keys_without_values(hass, appliances):
    coordinator = appliances.entry.runtime_data.coordinators["fridge"]
    appliances.states["fridge"]["new_read_feature"] = {"private_nested_key": "private-read-value"}
    await coordinator.async_refresh()
    await appliances.update("fridge", {"new_push_feature": "private-push-value"})
    await appliances.update(
        "fridge",
        {"appliance_model": "TEST-MODEL", "new_snapshot_feature": "private-snapshot-value"},
        full=True,
    )
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "fridge"), appliances.entry.entry_id
    )
    result = await async_get_device_diagnostics(hass, appliances.entry, device)
    assert result["unrecognized_state_keys"] == [
        "ap_ssid",
        "appliance_serial",
        "new_push_feature",
        "new_read_feature",
        "new_snapshot_feature",
        "remote_svc_reg_token",
    ]
    assert result["state"] == {"appliance_model": "TEST-MODEL"}
    encoded = json.dumps(await async_get_config_entry_diagnostics(hass, appliances.entry))
    for private in (
        "private_nested_key",
        "private-read-value",
        "private-push-value",
        "private-snapshot-value",
    ):
        assert private not in encoded


async def test_stream_error_only_affects_its_appliance(hass, appliances):
    await appliances.updates.put(("oven", ApiError("Disconnected")))
    await hass.async_block_till_done()
    assert hass.states.get("climate.oven_oven").state == "unavailable"
    assert hass.states.get("switch.oven_lower_oven_light").state == "unavailable"
    assert hass.states.get("switch.dishwasher_heated_dry").state == "off"


async def test_switch_states_update_without_duplicate_binary_sensors(hass, appliances):
    switches = {
        "oven_oven_light": ("oven", "cav_light_on"),
        "oven_lower_oven_light": ("oven", "cav2_light_on"),
        "dishwasher_heated_dry": ("dishwasher", "heated_dry_on"),
        "dishwasher_extended_dry": ("dishwasher", "extended_dry_on"),
        "dishwasher_high_temperature_wash": ("dishwasher", "high_temp_wash_on"),
        "dishwasher_sanitize_rinse": ("dishwasher", "sani_rinse_on"),
        "dishwasher_top_rack_only": ("dishwasher", "top_rack_only_on"),
    }
    for name, (device_id, key) in switches.items():
        assert hass.states.get(f"switch.{name}").state == "off"
        await appliances.update(device_id, {key: True})
        assert hass.states.get(f"switch.{name}").state == "on"
        assert hass.states.get(f"binary_sensor.{name}") is None


async def test_minor_upgrade_removes_only_retired_entities(hass, appliances):
    entry = appliances.entry
    await appliances.update("fridge", {"air_filter_on": True})
    registry = er.async_get(hass)
    switches = [
        entity
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
        if entity.domain == "switch"
    ]
    assert len(switches) == 8
    mode = registry.async_get("binary_sensor.fridge_max_ice")
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    switches[0] = registry.async_update_entity(
        switches[0].entity_id, new_entity_id="switch.custom_control"
    )
    switches[1] = registry.async_update_entity(
        switches[1].entity_id, disabled_by=er.RegistryEntryDisabler.USER
    )
    retired = [
        registry.async_get_or_create(
            "binary_sensor",
            DOMAIN,
            switch.unique_id,
            config_entry=entry,
            device_id=switch.device_id,
            suggested_object_id=f"old_{index}",
        )
        for index, switch in enumerate(switches)
    ]
    retired.append(
        registry.async_get_or_create(
            "sensor",
            DOMAIN,
            "fridge_connection_mode",
            config_entry=entry,
            suggested_object_id="custom_connection_mode",
        )
    )
    hass.config_entries.async_update_entry(entry, minor_version=1)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.minor_version == 2
    for entity in retired:
        assert registry.async_get(entity.entity_id) is None
        assert hass.states.get(entity.entity_id) is None
    for switch in switches:
        current = registry.async_get(switch.entity_id)
        assert current.id == switch.id
        assert current.disabled_by is switch.disabled_by
    assert registry.async_get(mode.entity_id).id == mode.id
    assert hass.states.get(mode.entity_id).state == "off"


async def test_optional_entities_report_native_values(appliances):
    coordinator = appliances.entry.runtime_data.coordinators["fridge"]
    light = SubZeroNumber(
        coordinator, next(d for d in NUMBER_DESCRIPTIONS if d.key == "accent_light_level")
    )
    await light.async_set_native_value(65)
    appliances.client.set_property.assert_awaited_once_with("fridge", "accent_light_level", 65)
    assert light.native_value == 65
    uptime = SubZeroSensor(coordinator, next(d for d in SENSOR_DESCRIPTIONS if d.key == "uptime"))
    assert uptime.native_value == 435 * 3600 + 53 * 60 + 58
    reporting = SubZeroSensor(
        coordinator, next(d for d in SENSOR_DESCRIPTIONS if d.key == "live_reporting_mode")
    )
    assert reporting.native_value == "Cloud push"
    appliances.client.push_connected = False
    assert reporting.native_value == "Disconnected"


async def test_diagnostics_count_push_updates(hass, appliances):
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "fridge"), appliances.entry.entry_id
    )
    before = await async_get_device_diagnostics(hass, appliances.entry, device)
    assert before["push"] == {"snapshots": 0, "updates": 0, "last_received": None}
    await appliances.update("fridge", {"ref_door_ajar": True})
    await appliances.update("fridge", {"appliance_model": "TEST-MODEL"}, full=True)
    result = await async_get_device_diagnostics(hass, appliances.entry, device)
    assert result["push"]["snapshots"] == 1
    assert result["push"]["updates"] == 1
    assert dt_util.parse_datetime(result["push"]["last_received"]) <= dt_util.utcnow()
    assert before["push"] == {"snapshots": 0, "updates": 0, "last_received": None}


async def test_device_diagnostics_select_only_the_requested_appliance(hass, appliances):
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "oven"), appliances.entry.entry_id
    )
    result = await async_get_device_diagnostics(hass, appliances.entry, device)
    assert result["state"]["appliance_model"] == "DO30PM"
    assert "DW2450WS" not in json.dumps(result)
    assert appliances.client.state.await_count == 3


@pytest.mark.parametrize(
    "properties",
    [
        {"cav_set_temp": 0},
        {"cav_set_temp": None},
        {"cav_set_temp": True},
        {"cav_gourmet_mode_on": True},
        {"cav_door_ajar": 1},
    ],
)
async def test_remote_start_rejects_incomplete_or_manual_cooking_setup(appliances, properties):
    await appliances.update("oven", {"cav_remote_ready": True, **properties})
    with pytest.raises(ServiceValidationError):
        await appliances.entry.runtime_data.coordinators["oven"].async_set_properties(
            {"cav_unit_on": True}
        )
    appliances.client.set_property.assert_not_called()


@pytest.mark.parametrize("door", [True, 1, None])
async def test_dishwasher_start_requires_a_closed_reported_door(appliances, door):
    await appliances.update("dishwasher", {"remote_ready": True, "door_ajar": door})
    with pytest.raises(ServiceValidationError, match="door"):
        await appliances.entry.runtime_data.coordinators["dishwasher"].async_set_properties(
            {"wash_cycle_on": True}
        )
    appliances.client.set_property.assert_not_called()


async def test_timer_complete_is_written_before_timer_active(hass, appliances):
    """Entities write state in registration order, which same-update automations observe."""
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "trigger": {
                        "platform": "state",
                        "entity_id": "binary_sensor.oven_cooking_timer_active",
                        "to": "off",
                    },
                    "condition": {
                        "condition": "state",
                        "entity_id": "binary_sensor.oven_cooking_timer_complete",
                        "state": "on",
                    },
                    "action": {"event": "timer_done"},
                }
            ]
        },
    )
    events = async_capture_events(hass, "timer_done")
    await appliances.update("oven", {"cav_cook_timer_active": True})
    await appliances.update(
        "oven", {"cav_cook_timer_active": False, "cav_cook_timer_complete": True}
    )
    await hass.async_block_till_done()
    assert len(events) == 1


async def test_null_binary_value_is_unknown(hass, appliances):
    await appliances.update("fridge", {"max_ice_on": None})
    assert hass.states.get("binary_sensor.fridge_max_ice").state == "unknown"


async def test_null_setpoint_disables_controls_but_not_the_sensor(hass, appliances):
    await appliances.update("fridge", {"ref_set_temp": None})
    assert hass.states.get("climate.fridge_refrigerator").state == "unavailable"
    assert hass.states.get("number.fridge_refrigerator_setpoint").state == "unavailable"
    assert hass.states.get("sensor.fridge_refrigerator_setpoint").state == "unknown"
