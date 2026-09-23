"""Ice-mode sequences, bounded retries, and state confirmation."""

import asyncio
from unittest.mock import call

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.script import Script
from homeassistant.setup import async_setup_component

from custom_components.subzero.api import ApiError, RateLimited, StateUpdate
from custom_components.subzero.auth import InvalidAuth

pytestmark = pytest.mark.parametrize(
    "cloud_appliance",
    [
        {
            "appliance_model": "CL4850UFDID",
            "ref_set_temp": 38,
            "ice_maker_on": True,
            "max_ice_on": False,
            "night_ice_on": True,
        }
    ],
    indirect=True,
)


async def test_unconfirmed_step_retries_three_times_and_stops(cloud_appliance):
    client = cloud_appliance.client
    client.set_property.side_effect = None
    reads = client.state.await_count

    with pytest.raises(HomeAssistantError, match="did not confirm") as failure:
        await cloud_appliance.coordinator.async_set_ice_mode("Off")

    assert client.set_property.await_args_list == [
        call("appliance", "max_ice_on", False),
        *[call("appliance", "night_ice_on", False)] * 3,
    ]
    assert client.state.await_count == reads + 3
    assert cloud_appliance.coordinator.data["ice_maker_on"] is True
    assert failure.value.__cause__ is None


async def test_successful_retry_continues_with_the_next_step(cloud_appliance):
    client = cloud_appliance.client
    original = client.set_property.side_effect

    async def write(device_id, key, value):
        if client.set_property.await_count > 1:
            await original(device_id, key, value)

    client.set_property.side_effect = write
    await cloud_appliance.coordinator.async_set_ice_mode("Max ice")

    assert client.set_property.await_args_list == [
        call("appliance", "night_ice_on", False),
        call("appliance", "night_ice_on", False),
        call("appliance", "max_ice_on", True),
    ]


async def test_error_response_can_be_confirmed_by_reading_state(cloud_appliance):
    client = cloud_appliance.client
    reads = client.state.await_count

    async def write(device_id, key, value):
        cloud_appliance.state[key] = value
        raise ApiError("Sub-Zero returned HTTP 503.")

    client.set_property.side_effect = write
    await cloud_appliance.coordinator.async_set_ice_mode("Max ice")

    assert client.set_property.await_args_list == [
        call("appliance", "night_ice_on", False),
        call("appliance", "max_ice_on", True),
    ]
    assert client.state.await_count == reads + 2


async def test_error_without_state_change_exhausts_retries(cloud_appliance):
    client = cloud_appliance.client
    reads = client.state.await_count
    client.set_property.side_effect = ApiError("Sub-Zero returned HTTP 503.")

    with pytest.raises(HomeAssistantError, match="HTTP 503") as failure:
        await cloud_appliance.coordinator.async_set_ice_mode("Max ice")

    assert client.set_property.await_args_list == [call("appliance", "night_ice_on", False)] * 3
    assert client.state.await_count == reads + 3
    assert failure.value.__cause__ is client.set_property.side_effect


async def test_failed_refresh_preserves_the_command_error(cloud_appliance):
    client = cloud_appliance.client
    client.set_property.side_effect = ApiError("Sub-Zero returned HTTP 503.")
    client.state.side_effect = ApiError("Status read failed.")
    with pytest.raises(HomeAssistantError, match="HTTP 503") as failure:
        await cloud_appliance.coordinator.async_set_ice_mode("Max ice")
    assert failure.value.__cause__ is client.set_property.side_effect
    client.set_property.assert_awaited_once_with("appliance", "night_ice_on", False)


@pytest.mark.parametrize("error", [InvalidAuth("Expired"), RateLimited(60)])
@pytest.mark.parametrize("control", ["mode", "property"])
async def test_authentication_and_rate_limits_stop_without_retry(
    hass, cloud_appliance, error, control
):
    client = cloud_appliance.client
    reads = client.state.await_count
    client.set_property.side_effect = error

    with pytest.raises(HomeAssistantError):
        if control == "mode":
            await cloud_appliance.coordinator.async_set_ice_mode("Max ice")
        else:
            await cloud_appliance.coordinator.async_set_properties({"night_ice_on": False})

    client.set_property.assert_awaited_once_with("appliance", "night_ice_on", False)
    assert client.state.await_count == reads
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == (
        ["reauth"] if isinstance(error, InvalidAuth) else []
    )


async def test_confirmation_deadline_includes_request_time(cloud_appliance):
    client = cloud_appliance.client
    coordinator = cloud_appliance.coordinator
    listeners = len(coordinator._listeners)
    cancelled = 0

    async def write(device_id, key, value):
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled += 1
            raise

    client.set_property.side_effect = write
    with pytest.raises(HomeAssistantError, match="did not confirm"):
        async with asyncio.timeout(1):
            await coordinator.async_set_ice_mode("Max ice")

    assert cancelled == 3
    assert client.set_property.await_args_list == [call("appliance", "night_ice_on", False)] * 3
    assert len(coordinator._listeners) == listeners


async def test_request_cut_off_by_the_deadline_is_confirmed_by_reading_state(cloud_appliance):
    client = cloud_appliance.client
    reads = client.state.await_count

    async def write(device_id, key, value):
        cloud_appliance.state[key] = value
        await asyncio.Event().wait()

    client.set_property.side_effect = write
    await cloud_appliance.coordinator.async_set_properties({"night_ice_on": False})

    client.set_property.assert_awaited_once_with("appliance", "night_ice_on", False)
    assert client.state.await_count == reads + 1


async def test_push_during_a_slow_request_confirms_without_resending(cloud_appliance):
    client = cloud_appliance.client
    coordinator = cloud_appliance.coordinator
    reads = client.state.await_count

    async def write(device_id, key, value):
        cloud_appliance.state[key] = value
        coordinator.apply_update(StateUpdate({key: value}, full=False))
        await asyncio.Event().wait()

    client.set_property.side_effect = write
    await coordinator.async_set_properties({"night_ice_on": False})

    client.set_property.assert_awaited_once_with("appliance", "night_ice_on", False)
    assert client.state.await_count == reads


async def test_status_read_may_outlast_the_confirmation_deadline(cloud_appliance):
    client = cloud_appliance.client
    coordinator = cloud_appliance.coordinator

    async def write(device_id, key, value):
        cloud_appliance.state[key] = value

    async def read(device_id):
        await asyncio.sleep(0.1)
        return dict(cloud_appliance.state)

    client.set_property.side_effect = write
    client.state.side_effect = read
    await coordinator.async_set_properties({"night_ice_on": False})

    client.set_property.assert_awaited_once_with("appliance", "night_ice_on", False)
    assert coordinator.last_update_success


async def test_failed_status_read_reports_an_unconfirmed_setting(cloud_appliance):
    client = cloud_appliance.client
    client.set_property.side_effect = None
    client.state.side_effect = ApiError("Status read failed.")

    with pytest.raises(HomeAssistantError, match="did not confirm"):
        await cloud_appliance.coordinator.async_set_properties({"night_ice_on": False})

    client.set_property.assert_awaited_once_with("appliance", "night_ice_on", False)


async def test_cancellation_removes_listener_and_stops_writing(cloud_appliance):
    client = cloud_appliance.client
    coordinator = cloud_appliance.coordinator
    listeners = len(coordinator._listeners)
    entered = asyncio.Event()

    async def write(device_id, key, value):
        entered.set()
        await asyncio.Event().wait()

    client.set_property.side_effect = write
    pending = asyncio.create_task(coordinator.async_set_ice_mode("Max ice"))
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    client.set_property.assert_awaited_once_with("appliance", "night_ice_on", False)
    assert len(coordinator._listeners) == listeners
    assert not coordinator._command_lock.locked()


@pytest.mark.parametrize("control", ["mode", "property"])
@pytest.mark.parametrize("error", [None, ApiError("Status read failed.")])
async def test_cancelled_command_leaves_status_read_running(cloud_appliance, control, error):
    client = cloud_appliance.client
    coordinator = cloud_appliance.coordinator
    listeners = len(coordinator._listeners)
    entered = asyncio.get_running_loop().create_future()
    finish = asyncio.Event()

    async def write(device_id, key, value):
        cloud_appliance.state[key] = value

    async def read(device_id):
        entered.set_result(asyncio.current_task())
        await finish.wait()
        if error is not None:
            raise error
        return dict(cloud_appliance.state)

    client.set_property.side_effect = write
    client.state.side_effect = read
    pending = asyncio.create_task(
        coordinator.async_set_ice_mode("Max ice")
        if control == "mode"
        else coordinator.async_set_properties({"night_ice_on": False, "max_ice_on": True})
    )
    refresh = await asyncio.wait_for(entered, 1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert coordinator.last_update_success
    assert not refresh.done()
    assert len(coordinator._listeners) == listeners
    assert not coordinator._command_lock.locked()
    finish.set()
    await refresh

    client.set_property.assert_awaited_once_with("appliance", "night_ice_on", False)
    assert coordinator.last_update_success is (error is None)
    if error is None:
        assert coordinator.data["night_ice_on"] is False
        assert coordinator.data["max_ice_on"] is False
    else:
        assert str(coordinator.last_exception) == "Status read failed."


async def test_script_stop_preserves_status_read_until_unload(hass, cloud_appliance):
    client = cloud_appliance.client
    entered = asyncio.get_running_loop().create_future()

    async def read(device_id):
        entered.set_result(asyncio.current_task())
        await asyncio.Event().wait()

    client.set_property.side_effect = None
    client.state.side_effect = read
    script = Script(
        hass,
        cv.SCRIPT_SCHEMA(
            [
                {
                    "action": "select.select_option",
                    "data": {"entity_id": "select.kitchen_ice_maker", "option": "Max ice"},
                }
            ]
        ),
        "Change ice mode",
        "automation",
    )
    pending = asyncio.create_task(script.async_run(context=Context()))
    refresh = await asyncio.wait_for(entered, 1)
    await asyncio.wait_for(script.async_stop(), 1)
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert cloud_appliance.coordinator.last_update_success
    assert not refresh.done()

    assert await hass.config_entries.async_unload(cloud_appliance.entry.entry_id)
    assert refresh.cancelled()
    client.set_property.assert_awaited_once_with("appliance", "night_ice_on", False)


async def test_script_stop_during_entity_update_finishes_the_status_read(hass, cloud_appliance):
    assert await async_setup_component(hass, "homeassistant", {})
    entered = asyncio.get_running_loop().create_future()
    finish = asyncio.Event()

    async def read(device_id):
        entered.set_result(asyncio.current_task())
        await finish.wait()
        return {**cloud_appliance.state, "night_ice_on": False}

    cloud_appliance.client.state.side_effect = read
    script = Script(
        hass,
        cv.SCRIPT_SCHEMA(
            [
                {
                    "action": "homeassistant.update_entity",
                    "target": {"entity_id": "select.kitchen_ice_maker"},
                }
            ]
        ),
        "Refresh ice maker",
        "automation",
    )
    pending = asyncio.create_task(script.async_run(context=Context()))
    refresh = await asyncio.wait_for(entered, 1)
    await asyncio.wait_for(script.async_stop(), 1)
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert cloud_appliance.coordinator.last_update_success

    finish.set()
    await refresh
    await hass.async_block_till_done()
    assert cloud_appliance.coordinator.last_update_success
    assert hass.states.get("select.kitchen_ice_maker").state == "On"


@pytest.mark.parametrize("changed_key", ["night_ice_on", "max_ice_on"])
async def test_changed_capabilities_stop_retries_or_later_steps(cloud_appliance, changed_key):
    client = cloud_appliance.client
    coordinator = cloud_appliance.coordinator

    async def write(device_id, key, value):
        properties = {key: value, changed_key: None}
        cloud_appliance.state.update(properties)
        coordinator.apply_update(StateUpdate(properties, full=False))

    client.set_property.side_effect = write
    with pytest.raises(ServiceValidationError, match="on/off value"):
        await coordinator.async_set_ice_mode("Max ice")

    client.set_property.assert_awaited_once_with("appliance", "night_ice_on", False)


async def test_queued_mode_change_uses_state_after_previous_command(cloud_appliance):
    client = cloud_appliance.client
    coordinator = cloud_appliance.coordinator
    await cloud_appliance.update({"night_ice_on": False})

    async with coordinator._command_lock:
        pending = asyncio.create_task(coordinator.async_set_ice_mode("On"))
        await asyncio.sleep(0)
        cloud_appliance.state["night_ice_on"] = True
        coordinator.apply_update(StateUpdate({"night_ice_on": True}, full=False))
    await pending

    assert client.set_property.await_args_list == [
        call("appliance", "night_ice_on", False),
        call("appliance", "ice_maker_on", True),
    ]


async def test_max_ice_from_normal_leaves_power_alone(cloud_appliance):
    await cloud_appliance.update({"night_ice_on": False})
    await cloud_appliance.coordinator.async_set_ice_mode("Max ice")
    cloud_appliance.client.set_property.assert_awaited_once_with("appliance", "max_ice_on", True)
