"""Exercise fridge controls through real Home Assistant services."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.subzero.api import ApiError, Appliance, StateUpdate, token_state
from custom_components.subzero.auth import InvalidAuth
from custom_components.subzero.const import DOMAIN
from custom_components.subzero.controls import temperature_range

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture
async def controls(hass, tokens, request):
    hass.config.units = US_CUSTOMARY_SYSTEM
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Account",
        data={
            "tokens": token_state(tokens),
            "devices": {
                "test-fridge": {
                    "name": "Kitchen",
                    "temperature_unit": getattr(request, "param", "F"),
                },
                "test-oven": {"name": "Oven", "temperature_unit": "F"},
                "test-cove": {"name": "Dishwasher", "temperature_unit": "F"},
            },
        },
    )
    entry.add_to_hass(hass)
    states = {
        "test-fridge": {
            "appliance_model": "CL4850UFDID",
            "ref_set_temp": 38,
            "frz_set_temp": 0,
            "crisp_set_temp": 36,
            "crisp_temp_mode": 1,
            "humidity_control": 1,
            "night_mode": 1,
            "ice_maker_on": True,
            "max_ice_on": False,
            "night_ice_on": True,
            "air_filter_on": True,
            "sabbath_on": False,
            "high_use_on": False,
            "short_vacation_on": False,
            "long_vacation_on": False,
            "unit_on": True,
        },
        "test-oven": {"appliance_model": "SO3050PMSP", "cav_light_on": False, "sabbath_on": False},
        "test-cove": {"appliance_model": "DW2450", "service_required": False, "sabbath_on": False},
    }
    updates = asyncio.Queue()
    behavior = {"push": True, "accept": True}

    async def watch(device_ids):
        while True:
            yield await updates.get()

    async def write(device_id, key, value):
        assert device_id == "test-fridge"
        if not behavior["accept"]:
            return
        properties = {key: value}
        states[device_id].update(properties)
        if behavior["push"]:
            await updates.put((device_id, StateUpdate(dict(properties), full=False)))

    with (
        patch("custom_components.subzero.SubZeroClient") as factory,
        patch("custom_components.subzero.coordinator.CONTROL_CONFIRM_TIMEOUT", 0.02),
    ):
        client = factory.return_value
        client.tokens = token_state(tokens)
        client.appliances = AsyncMock(
            return_value=[
                Appliance(device_id, device["name"], device["temperature_unit"])
                for device_id, device in entry.data["devices"].items()
            ]
        )
        client.state = AsyncMock(side_effect=lambda device_id: dict(states[device_id]))
        client.set_property = AsyncMock(side_effect=write)
        client.open_channel = AsyncMock()
        client.watch = watch
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield SimpleNamespace(
            entry=entry, client=client, states=states, updates=updates, behavior=behavior
        )
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_controls_are_grouped_and_require_reported_properties(hass, controls):
    entities = er.async_entries_for_config_entry(er.async_get(hass), controls.entry.entry_id)
    control_entities = {e.entity_id for e in entities if e.domain in {"number", "select", "switch"}}
    assert control_entities == {
        "number.kitchen_refrigerator_setpoint",
        "number.kitchen_freezer_setpoint",
        "number.kitchen_crisper_setpoint",
        "select.kitchen_ice_maker",
        "select.kitchen_mode",
        "select.kitchen_crisper_temperature_mode",
        "select.kitchen_humidity_control",
        "select.kitchen_night_mode",
        "switch.kitchen_air_purification",
        "switch.oven_oven_light",
    }
    assert hass.states.get("select.kitchen_ice_maker").state == "Night ice"
    assert hass.states.get("select.kitchen_mode").state == "Normal"
    assert hass.states.get("select.kitchen_crisper_temperature_mode").state == "Automatic"
    assert hass.states.get("select.kitchen_humidity_control").state == "Normal"
    assert hass.states.get("select.kitchen_night_mode").state == "Enabled"
    assert hass.states.get("number.kitchen_crisper_setpoint").state == "unavailable"
    assert hass.states.get("sensor.kitchen_crisper_setpoint").state == "36"
    assert hass.states.get("binary_sensor.kitchen_night_ice").state == "on"
    assert hass.states.get("switch.kitchen_air_purification").state == "on"
    controls.client.set_property.assert_not_called()


async def test_air_purification_switch_confirms_push_without_duplicate_status(hass, controls):
    reads = controls.client.state.await_count
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.kitchen_air_purification"}, blocking=True
    )
    controls.client.set_property.assert_awaited_once_with("test-fridge", "air_filter_on", False)
    assert hass.states.get("switch.kitchen_air_purification").state == "off"
    assert hass.states.get("binary_sensor.kitchen_air_purification") is None
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.kitchen_air_purification"}, blocking=True
    )
    assert hass.states.get("switch.kitchen_air_purification").state == "on"
    assert controls.client.state.await_count == reads


@pytest.mark.parametrize(
    ("option", "writes"),
    [
        ("Off", [("night_ice_on", False), ("ice_maker_on", False)]),
        ("On", [("night_ice_on", False)]),
        ("Max ice", [("night_ice_on", False), ("max_ice_on", True)]),
        ("Night ice", []),
    ],
)
async def test_ice_maker_select_sets_one_coherent_mode(hass, controls, option, writes):
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.kitchen_ice_maker", "option": option},
        blocking=True,
    )
    assert controls.client.set_property.await_args_list == [
        call("test-fridge", key, value) for key, value in writes
    ]
    assert hass.states.get("select.kitchen_ice_maker").state == option
    freezer = hass.states.get("number.kitchen_freezer_setpoint")
    assert freezer.state == ("unavailable" if option == "Max ice" else "0")


async def test_night_ice_stays_selected_while_ice_maker_power_is_off(hass, controls):
    for powered in (False, True, False):
        controls.states["test-fridge"]["ice_maker_on"] = powered
        await controls.updates.put(
            ("test-fridge", StateUpdate({"ice_maker_on": powered}, full=False))
        )
        await hass.async_block_till_done()
        assert hass.states.get("select.kitchen_ice_maker").state == "Night ice"
        assert hass.states.get("binary_sensor.kitchen_ice_maker_enabled").state == (
            "on" if powered else "off"
        )
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.kitchen_ice_maker", "option": "Night ice"},
        blocking=True,
    )
    controls.client.set_property.assert_not_awaited()


async def test_selecting_night_ice_from_off_does_not_force_ice_maker_power(hass, controls):
    properties = {"ice_maker_on": False, "night_ice_on": False}
    controls.states["test-fridge"].update(properties)
    await controls.updates.put(("test-fridge", StateUpdate(properties, full=False)))
    await hass.async_block_till_done()
    assert hass.states.get("select.kitchen_ice_maker").state == "Off"
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.kitchen_ice_maker", "option": "Night ice"},
        blocking=True,
    )
    controls.client.set_property.assert_awaited_once_with("test-fridge", "night_ice_on", True)
    assert hass.states.get("select.kitchen_ice_maker").state == "Night ice"
    assert hass.states.get("binary_sensor.kitchen_ice_maker_enabled").state == "off"


@pytest.mark.parametrize(
    ("option", "writes"),
    [
        ("Off", [("night_ice_on", False)]),
        ("On", [("night_ice_on", False), ("ice_maker_on", True)]),
        ("Max ice", [("night_ice_on", False), ("ice_maker_on", True), ("max_ice_on", True)]),
    ],
)
async def test_leaving_night_ice_while_power_is_off(hass, controls, option, writes):
    controls.states["test-fridge"]["ice_maker_on"] = False
    await controls.updates.put(("test-fridge", StateUpdate({"ice_maker_on": False}, full=False)))
    await hass.async_block_till_done()
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.kitchen_ice_maker", "option": option},
        blocking=True,
    )
    assert controls.client.set_property.await_args_list == [
        call("test-fridge", key, value) for key, value in writes
    ]
    assert hass.states.get("select.kitchen_ice_maker").state == option


@pytest.mark.parametrize(
    ("option", "property_key"),
    [
        ("Normal", None),
        ("Sabbath", "sabbath_on"),
        ("High use", "high_use_on"),
        ("Short vacation", "short_vacation_on"),
        ("Long vacation", "long_vacation_on"),
    ],
)
async def test_operating_mode_select_clears_other_modes(hass, controls, option, property_key):
    await controls.updates.put(("test-fridge", StateUpdate({"high_use_on": True}, full=False)))
    await hass.async_block_till_done()
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.kitchen_mode", "option": option},
        blocking=True,
    )
    writes = []
    if property_key != "high_use_on":
        writes.append(call("test-fridge", "high_use_on", False))
        if property_key is not None:
            writes.append(call("test-fridge", property_key, True))
    assert controls.client.set_property.await_args_list == writes
    assert hass.states.get("select.kitchen_mode").state == option


@pytest.mark.parametrize(
    ("entity", "key", "value"),
    [
        ("refrigerator", "ref_set_temp", 40),
        ("freezer", "frz_set_temp", -3),
    ],
)
async def test_temperature_setpoints_write_integers_and_update_sensors(
    hass, controls, entity, key, value
):
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": f"number.kitchen_{entity}_setpoint", "value": value},
        blocking=True,
    )
    controls.client.set_property.assert_awaited_once_with("test-fridge", key, value)
    assert type(controls.client.set_property.call_args.args[2]) is int
    assert hass.states.get(f"number.kitchen_{entity}_setpoint").state == str(value)
    assert hass.states.get(f"sensor.kitchen_{entity}_setpoint").state == str(value)


async def test_crisper_manual_mode_and_limits_follow_refrigerator(hass, controls):
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.kitchen_crisper_temperature_mode", "option": "Manual"},
        blocking=True,
    )
    controls.client.set_property.assert_awaited_once_with("test-fridge", "crisp_temp_mode", 0)
    assert hass.states.get("number.kitchen_crisper_setpoint").state == "36"
    await controls.updates.put(("test-fridge", StateUpdate({"ref_set_temp": 39}, full=False)))
    await hass.async_block_till_done()
    crisper = hass.states.get("number.kitchen_crisper_setpoint")
    assert crisper.attributes["min"] == 37
    assert crisper.attributes["max"] == 41
    await hass.services.async_call(
        "number", "set_value", {"entity_id": crisper.entity_id, "value": 37}, blocking=True
    )
    controls.client.set_property.assert_awaited_with("test-fridge", "crisp_set_temp", 37)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "number", "set_value", {"entity_id": crisper.entity_id, "value": 36}, blocking=True
        )
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.kitchen_crisper_temperature_mode", "option": "Automatic"},
        blocking=True,
    )
    assert hass.states.get(crisper.entity_id).state == "unavailable"


@pytest.mark.parametrize(
    ("key", "option", "value", "original"),
    [
        ("humidity_control", "Enhanced", 2, "Normal"),
        ("night_mode", "Disabled", 0, "Enabled"),
    ],
)
async def test_humidity_and_night_mode_use_integer_values(
    hass, controls, key, option, value, original
):
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": f"select.kitchen_{key}", "option": option},
        blocking=True,
    )
    controls.client.set_property.assert_awaited_once_with("test-fridge", key, value)
    assert type(controls.client.set_property.call_args.args[2]) is int
    assert hass.states.get(f"select.kitchen_{key}").state == option
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": f"select.kitchen_{key}", "option": original},
        blocking=True,
    )
    controls.client.set_property.assert_awaited_with("test-fridge", key, 1)
    assert hass.states.get(f"select.kitchen_{key}").state == original
    assert hass.states.get("select.kitchen_ice_maker").state == "Night ice"


@pytest.mark.parametrize(
    ("key", "value"),
    [("humidity_control", 3), ("night_mode", 2), ("night_mode", True)],
)
@pytest.mark.parametrize("source", ["push", "refresh"])
async def test_unknown_mode_values_are_not_treated_as_enabled(hass, controls, key, value, source):
    coordinator = controls.entry.runtime_data.coordinators["test-fridge"]
    if source == "push":
        await controls.updates.put(("test-fridge", StateUpdate({key: value}, full=False)))
    else:
        controls.states["test-fridge"][key] = value
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(f"select.kitchen_{key}").state == "unavailable"
    with pytest.raises(ServiceValidationError, match="unknown"):
        await coordinator.async_set_properties({key: 1})
    controls.client.set_property.assert_not_called()


async def test_mode_change_stops_on_partial_failure_without_replaying_settings(hass, controls):
    original = controls.client.set_property.side_effect

    async def write(device_id, key, value):
        if key == "max_ice_on":
            raise ApiError("Sub-Zero returned HTTP 503.")
        await original(device_id, key, value)

    controls.client.set_property.side_effect = write
    with pytest.raises(HomeAssistantError, match="HTTP 503"):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": "select.kitchen_ice_maker", "option": "Max ice"},
            blocking=True,
        )
    assert controls.client.set_property.await_args_list == [
        call("test-fridge", "night_ice_on", False),
        call("test-fridge", "max_ice_on", True),
    ]
    assert hass.states.get("select.kitchen_ice_maker").state == "On"
    assert hass.states.get("binary_sensor.kitchen_max_ice").state == "off"


async def test_mode_change_confirms_each_step_before_sending_the_next(hass, controls):
    entered = asyncio.Event()
    original = controls.client.set_property.side_effect

    async def write(device_id, key, value):
        if key == "night_ice_on":
            entered.set()
            return
        await original(device_id, key, value)

    controls.client.set_property.side_effect = write
    pending = asyncio.create_task(
        hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": "select.kitchen_ice_maker", "option": "Max ice"},
            blocking=True,
        )
    )
    await entered.wait()
    controls.client.set_property.assert_awaited_once_with("test-fridge", "night_ice_on", False)
    assert hass.states.get("select.kitchen_ice_maker").state == "Night ice"
    await controls.updates.put(("test-fridge", StateUpdate({"night_ice_on": False}, full=False)))
    await pending
    assert hass.states.get("select.kitchen_ice_maker").state == "Max ice"


@pytest.mark.parametrize(
    ("entity", "value"),
    [
        ("refrigerator", 33),
        ("refrigerator", 43),
        ("freezer", -6),
        ("freezer", 6),
        ("refrigerator", float("nan")),
        ("refrigerator", float("inf")),
    ],
)
async def test_invalid_temperatures_do_not_reach_the_api(hass, controls, entity, value):
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": f"number.kitchen_{entity}_setpoint", "value": value},
            blocking=True,
        )
    controls.client.set_property.assert_not_called()


async def test_celsius_display_converts_to_a_whole_fahrenheit_setpoint(hass, controls):
    hass.config.units = METRIC_SYSTEM
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.kitchen_refrigerator_setpoint", "value": 4},
        blocking=True,
    )
    controls.client.set_property.assert_awaited_once_with("test-fridge", "ref_set_temp", 39)


@pytest.mark.parametrize("controls", ["C", None], indirect=True)
async def test_unknown_native_temperature_units_keep_other_controls(hass, controls):
    assert hass.states.get("number.kitchen_refrigerator_setpoint") is None
    assert hass.states.get("number.kitchen_freezer_setpoint") is None
    assert hass.states.get("number.kitchen_crisper_setpoint") is None
    assert hass.states.get("select.kitchen_ice_maker").state == "Night ice"
    assert hass.states.get("switch.kitchen_air_purification").state == "on"


async def test_delayed_push_does_not_show_an_optimistic_setting(hass, controls):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def pending_write(*_):
        entered.set()
        await release.wait()

    controls.client.set_property.side_effect = pending_write
    call = asyncio.create_task(
        hass.services.async_call(
            "switch", "turn_off", {"entity_id": "switch.kitchen_air_purification"}, blocking=True
        )
    )
    await entered.wait()
    assert hass.states.get("switch.kitchen_air_purification").state == "on"
    await controls.updates.put(("test-fridge", StateUpdate({"air_filter_on": False}, full=False)))
    release.set()
    await call
    assert hass.states.get("switch.kitchen_air_purification").state == "off"


async def test_missing_push_uses_one_status_read_to_confirm(hass, controls):
    controls.behavior["push"] = False
    reads = controls.client.state.await_count
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.kitchen_air_purification"}, blocking=True
    )
    assert hass.states.get("switch.kitchen_air_purification").state == "off"
    assert controls.client.state.await_count == reads + 1
    assert controls.client.set_property.await_count == 1


async def test_silent_rejection_reports_failure_and_actual_state(hass, controls):
    controls.behavior["accept"] = False
    with pytest.raises(HomeAssistantError, match="did not confirm"):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": "switch.kitchen_air_purification"}, blocking=True
        )
    assert hass.states.get("switch.kitchen_air_purification").state == "on"
    assert controls.client.set_property.await_count == 1


async def test_failed_write_is_not_retried_and_does_not_change_state(hass, controls):
    controls.client.set_property.side_effect = ApiError("Could not connect to Sub-Zero.")
    with pytest.raises(HomeAssistantError, match="Could not connect"):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": "switch.kitchen_air_purification"}, blocking=True
        )
    assert hass.states.get("switch.kitchen_air_purification").state == "on"
    assert controls.client.set_property.await_count == 1


async def test_expired_write_authentication_starts_reauthentication(hass, controls):
    controls.client.set_property.side_effect = InvalidAuth("Sign in again")
    with pytest.raises(HomeAssistantError, match="Sign in"):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": "switch.kitchen_air_purification"}, blocking=True
        )
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == "reauth"


async def test_queued_temperature_write_rechecks_max_ice_after_previous_command(hass, controls):
    entered = asyncio.Event()
    release = asyncio.Event()
    original = controls.client.set_property.side_effect

    async def pending_write(*args):
        entered.set()
        await release.wait()
        await original(*args)

    controls.client.set_property.side_effect = pending_write
    first = asyncio.create_task(
        hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": "select.kitchen_ice_maker", "option": "Max ice"},
            blocking=True,
        )
    )
    await entered.wait()
    coordinator = controls.entry.runtime_data.coordinators["test-fridge"]
    second = asyncio.create_task(coordinator.async_set_properties({"frz_set_temp": 1}))
    await asyncio.sleep(0)
    assert controls.client.set_property.await_count == 1
    release.set()
    await first
    with pytest.raises(ServiceValidationError, match="Max Ice"):
        await second
    assert controls.client.set_property.await_args_list == [
        call("test-fridge", "night_ice_on", False),
        call("test-fridge", "max_ice_on", True),
    ]


async def test_options_follow_reported_capabilities(hass, controls):
    snapshot = {
        "appliance_model": "FUTURE-MODEL",
        "frz_set_temp": 0,
        "ice_maker_on": True,
        "sabbath_on": False,
    }
    await controls.updates.put(("test-fridge", StateUpdate(snapshot, full=True)))
    await hass.async_block_till_done()
    assert hass.states.get("select.kitchen_ice_maker").attributes["options"] == ["Off", "On"]
    assert hass.states.get("select.kitchen_mode").attributes["options"] == ["Normal", "Sabbath"]
    assert hass.states.get("switch.kitchen_air_purification").state == "unavailable"
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": "select.kitchen_ice_maker", "option": "Max ice"},
            blocking=True,
        )
    controls.client.set_property.assert_not_called()


@pytest.mark.parametrize(
    ("model", "maximum"),
    [("CL4850UFDID", 42), ("DEC3050R", 42), ("BI-36U", 45), ("IT-36CI", 45), ("FUTURE-MODEL", 42)],
)
def test_refrigerator_ranges_follow_known_product_families(model, maximum):
    assert temperature_range("ref_set_temp", {"appliance_model": model}) == (34, maximum)
