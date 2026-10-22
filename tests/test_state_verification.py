"""Completed observations, freshness and independent state recovery."""

import asyncio
from datetime import timedelta
from unittest.mock import Mock

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.subzero.api import ApiError, StateUpdate
from tests.test_integration import loaded as loaded

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def test_partial_push_does_not_confirm_unchanged_properties(hass, loaded, freezer):
    entry, _, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    confirmed = coordinator.confirmed_at
    assert confirmed is not None
    freezer.tick(1)
    await updates.put(StateUpdate({"ref_set_temp": 39}, full=False))
    await hass.async_block_till_done()
    assert coordinator.confirmed_at == confirmed


@pytest.mark.parametrize("complete", [False, True])
async def test_failed_read_only_confirms_a_complete_concurrent_snapshot(
    hass, loaded, monkeypatch, freezer, complete
):
    entry, client, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    monkeypatch.setattr("custom_components.subzero.coordinator.SNAPSHOT_TIMEOUT", 0)
    await updates.put(StateUpdate({"ref_door_ajar": True}, full=False))
    await hass.async_block_till_done()
    confirmed = coordinator.confirmed_at
    freezer.tick(1)

    async def failed_read(_device_id):
        properties = (
            dict(client.state.return_value)
            if complete
            else {"appliance_model": "ANOTHER-MODEL", "ref_set_temp": 39}
        )
        coordinator.apply_update(StateUpdate(properties, full=True))
        raise ApiError("Status request failed")

    client.state.side_effect = failed_read
    renew = Mock(return_value=True)
    await coordinator.async_verify_state(renew)
    if complete:
        assert coordinator.confirmed_at == dt_util.utcnow()
        assert coordinator.verification_stats["last_success"] is not None
        assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    else:
        assert coordinator.confirmed_at == confirmed
        assert coordinator.verification_stats["last_success"] is None
        assert "ref_door_ajar" in coordinator.missing_state_keys["snapshot"]
    renew.assert_not_called()


async def test_incomplete_status_reply_does_not_confirm_old_values(loaded, freezer):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    confirmed = coordinator.confirmed_at
    client.state.return_value = {"appliance_model": "ANOTHER-MODEL", "ref_set_temp": 39}
    freezer.tick(1)
    await coordinator.async_refresh()
    assert coordinator.confirmed_at == confirmed
    assert "ref_door_ajar" in coordinator.missing_state_keys["read"]


async def test_update_entity_waits_for_each_read_inside_the_old_cooldown(hass, loaded):
    _, client, _, _, _ = loaded
    assert await async_setup_component(hass, "homeassistant", {})
    client.state.reset_mock()
    for opened in (True, False):
        client.state.return_value["ref_door_ajar"] = opened
        await hass.services.async_call(
            "homeassistant",
            "update_entity",
            {"entity_id": "binary_sensor.kitchen_refrigerator_door"},
            blocking=True,
        )
        assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == (
            "on" if opened else "off"
        )
    assert client.state.await_count == 2


async def test_calls_during_a_read_share_a_newer_followup(hass, loaded):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    reading, release = asyncio.Event(), asyncio.Event()

    async def read(_device_id):
        snapshot = dict(client.state.return_value)
        if client.state.await_count == 1:
            reading.set()
            await release.wait()
        return snapshot

    client.state.reset_mock()
    client.state.side_effect = read
    first = asyncio.create_task(coordinator.async_read_status())
    await reading.wait()
    client.state.return_value["ref_door_ajar"] = True
    later = [asyncio.create_task(coordinator.async_read_status()) for _ in range(5)]
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, *later)
    assert client.state.await_count == 2
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"


async def test_cancelled_caller_does_not_cancel_the_shared_read(hass, loaded):
    entry, client, _, _, _ = loaded
    reading, release = asyncio.Event(), asyncio.Event()

    async def read(_device_id):
        reading.set()
        await release.wait()
        return {**client.state.return_value, "ref_door_ajar": True}

    client.state.side_effect = read
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    caller = asyncio.create_task(coordinator.async_read_status())
    await reading.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert not coordinator._read_task.cancelled()
    release.set()
    await coordinator._read_task
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"


async def test_read_status_action_confirms_and_returns_after_state_is_published(hass, loaded):
    entry, client, _, _, _ = loaded
    device = dr.async_get(hass).async_get_device_by_identifier(
        ("subzero", "test-fridge"), entry.entry_id
    )
    client.state.return_value["ref_door_ajar"] = True
    response = await hass.services.async_call(
        "subzero", "read_status", {"device_id": device.id}, blocking=True, return_response=True
    )
    assert (
        response["confirmed_at"]
        == entry.runtime_data.coordinators["test-fridge"].confirmed_at.isoformat()
    )
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"


async def test_read_status_action_rejects_an_unloaded_device(hass):
    from custom_components.subzero.services import async_setup_services

    async_setup_services(hass)
    with pytest.raises(ServiceValidationError, match="loaded Sub-Zero"):
        await hass.services.async_call(
            "subzero", "read_status", {"device_id": "missing"}, blocking=True
        )


@pytest.mark.parametrize("failure", ["error", "incomplete"])
async def test_explicit_confirmation_fails_instead_of_trusting_cached_state(loaded, failure):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    confirmed = coordinator.confirmed_at
    if failure == "error":
        client.state.side_effect = ApiError("Could not read status")
    else:
        client.state.return_value = {"appliance_model": "ANOTHER-MODEL"}
    with pytest.raises(HomeAssistantError):
        await coordinator.async_read_status()
    assert coordinator.confirmed_at == confirmed


async def test_confirmation_sensor_keeps_the_last_observation_when_offline(hass, loaded):
    entry, _, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    timestamp = hass.states.get("sensor.kitchen_state_confirmed").state
    assert timestamp == coordinator.confirmed_at.replace(microsecond=0).isoformat()
    coordinator.async_set_update_error(ApiError("Offline"))
    assert hass.states.get("sensor.kitchen_state_confirmed").state == timestamp


async def test_scheduled_reads_continue_when_push_is_disconnected(hass, loaded, freezer):
    entry, client, updates, _, _ = loaded
    client.push_connected = False
    await updates.put(ApiError("Push unavailable"))
    await hass.async_block_till_done()
    client.state.reset_mock()
    client.state.return_value["ref_door_ajar"] = True
    freezer.tick(301)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    await entry.runtime_data._verifications["test-fridge"]
    client.open_channel.assert_not_awaited()
    client.state.assert_awaited_once()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"


async def test_rate_limited_checks_leave_confirmation_age_unchanged(
    hass, loaded, freezer, monkeypatch
):
    from custom_components.subzero.api import RateLimited

    entry, client, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    monkeypatch.setattr("custom_components.subzero.coordinator.SNAPSHOT_TIMEOUT", 0)
    confirmed = coordinator.confirmed_at
    client.state.side_effect = RateLimited(600)
    started = dt_util.utcnow()
    for seconds in (301, 602, 903):
        freezer.move_to(started + timedelta(seconds=seconds))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        await entry.runtime_data._verifications["test-fridge"]
        await updates.put(StateUpdate({"ref_set_temp": 39}, full=False))
        await hass.async_block_till_done()
    assert coordinator.confirmed_at == confirmed
    assert (
        hass.states.get("sensor.kitchen_state_confirmed").state
        == confirmed.replace(microsecond=0).isoformat()
    )
    with pytest.raises(HomeAssistantError):
        await coordinator.async_read_status()
