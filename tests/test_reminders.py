"""Appliance alerts followed by a verified delayed state condition."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.automation import DATA_COMPONENT
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.subzero.api import ApiError, ChannelOpened, RateLimited, StateUpdate
from custom_components.subzero.fcm import FcmAlerts
from tests.common import async_capture_events, async_fire_time_changed
from tests.test_fcm import message

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.mark.parametrize(
    "outcome", ["closed", "open", "error", "rate_limited", "incomplete", "reload", "reconnect"]
)
async def test_delayed_door_reminder_requires_a_successful_current_observation(
    hass, loaded, freezer, outcome
):
    entry, client, updates, disconnected, _ = loaded
    hass.config_entries.async_update_entry(entry, pref_disable_polling=True)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    device = dr.async_get(hass).async_get_device_by_identifier(
        ("subzero", "test-fridge"), entry.entry_id
    )
    notifications = AsyncMock()
    hass.services.async_register("notify", "door_test", notifications)
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "alias": "Door reminder",
                    "triggers": [
                        {
                            "trigger": "state",
                            "entity_id": "event.kitchen_appliance_event",
                            "not_from": "unavailable",
                            "not_to": ["unknown", "unavailable"],
                        }
                    ],
                    "conditions": [
                        {
                            "condition": "state",
                            "entity_id": "event.kitchen_appliance_event",
                            "attribute": "event_type",
                            "state": "refrigerator_door_ajar",
                        }
                    ],
                    "actions": [
                        {"action": "notify.door_test", "data": {"message": "initial"}},
                        {"delay": {"minutes": 10}},
                        {"action": "subzero.read_status", "data": {"device_id": device.id}},
                        {
                            "condition": "state",
                            "entity_id": "binary_sensor.kitchen_refrigerator_door",
                            "state": "on",
                        },
                        {"action": "notify.door_test", "data": {"message": "reminder"}},
                    ],
                }
            ]
        },
    )
    client.state.return_value["ref_door_ajar"] = True
    coordinator.apply_update(StateUpdate({**client.state.return_value, "notifs": []}, full=True))
    confirmed = coordinator.confirmed_at
    client.state.reset_mock()
    freezer.tick(1)
    alerts = FcmAlerts(hass, entry, client, entry.runtime_data.coordinators)
    alerts._on_alert(
        message(device_id="test-fridge", code=101, timestamp=dt_util.utcnow()), "message-id", None
    )
    script = hass.data[DATA_COMPONENT].get_entity("automation.door_reminder").action_script
    async with asyncio.timeout(5):
        while script.last_action != "delay 0:10:00":
            await asyncio.sleep(0)
    assert [call.args[0].data["message"] for call in notifications.call_args_list] == ["initial"]
    client.state.assert_not_awaited()

    if outcome == "reload":
        await hass.config_entries.async_reload(entry.entry_id)
        await asyncio.sleep(0)
    elif outcome == "reconnect":
        connections = len(client.watched)
        disconnected.clear()
        client.push_connected = False
        await updates.put(ApiError("Notification stream disconnected"))
        await disconnected.wait()
        assert disconnected.is_set()

    still_open = outcome in {"open", "reload"}
    client.state.return_value["ref_door_ajar"] = still_open
    if outcome == "error":
        client.state.side_effect = ApiError("Status unavailable")
    elif outcome == "rate_limited":
        client.state.side_effect = RateLimited(600)
    elif outcome == "incomplete":
        del client.state.return_value["ref_door_ajar"]

    deadline = dt_util.utcnow() + timedelta(minutes=10)
    async_fire_time_changed(hass, deadline)
    freezer.move_to(deadline)
    await hass.async_block_till_done()
    assert [call.args[0].data["message"] for call in notifications.call_args_list] == (
        ["initial", "reminder"] if still_open else ["initial"]
    )
    assert client.state.await_count >= 1
    if outcome == "rate_limited":
        assert coordinator.confirmed_at == confirmed
        assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"
    elif outcome in {"closed", "reconnect"}:
        assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    if outcome == "reconnect":
        assert len(client.watched) > connections
        client.push_connected = True
        await updates.put(ChannelOpened())
        await hass.async_block_till_done()
        assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"


@pytest.mark.parametrize("failure", ["none", "failed_read", "rate_limited", "reconnect"])
async def test_channel_alert_then_missed_door_close(hass, freezer, loaded, monkeypatch, failure):
    """The reported workflow: a door alert, then the door closes and the channel never says so.

    A reminder that reads status first never fires. A plain "open for 10 minutes"
    trigger is repaired by follow-up and scheduled reads, except while the cloud
    rate-limits status reads, when the sensor keeps its last value.
    """
    entry, client, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    monkeypatch.setattr("custom_components.subzero.coordinator.random.uniform", lambda *_: 0)
    device = dr.async_get(hass).async_get_device_by_identifier(
        ("subzero", "test-fridge"), entry.entry_id
    )
    notifications = async_capture_events(hass, "test_door_notification")
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "alias": "Plain reminder",
                    "triggers": [
                        {
                            "trigger": "state",
                            "entity_id": "binary_sensor.kitchen_refrigerator_door",
                            "to": "on",
                            "for": {"minutes": 10},
                        }
                    ],
                    "actions": [
                        {"event": "test_door_notification", "event_data": {"stage": "plain"}}
                    ],
                },
                {
                    "alias": "Confirmed reminder",
                    "triggers": [
                        {"trigger": "state", "entity_id": "event.kitchen_appliance_event"}
                    ],
                    "conditions": [
                        {
                            "condition": "template",
                            "value_template": "{{ trigger.to_state is not none and "
                            "trigger.to_state.attributes.event_type == 'refrigerator_door_ajar' }}",
                        }
                    ],
                    "actions": [
                        {"event": "test_door_notification", "event_data": {"stage": "alert"}},
                        {"delay": {"minutes": 10}},
                        {"action": "subzero.read_status", "data": {"device_id": device.id}},
                        {
                            "condition": "state",
                            "entity_id": "binary_sensor.kitchen_refrigerator_door",
                            "state": "on",
                        },
                        {"event": "test_door_notification", "event_data": {"stage": "confirmed"}},
                    ],
                },
            ]
        },
    )
    elapsed = 0

    async def settle():
        # The confirmed reminder sleeps in its delay, so block_till_done would never return.
        for _ in range(5):
            await asyncio.sleep(0)
        if (task := coordinator._read_task) is not None and not task.done():
            await asyncio.wait_for(asyncio.shield(task), 1)
        for _ in range(5):
            await asyncio.sleep(0)

    async def advance(until):
        nonlocal elapsed
        while elapsed < until:
            step = min(10, until - elapsed)
            elapsed += step
            freezer.tick(step)
            async_fire_time_changed(hass, dt_util.utcnow())
            await settle()

    def door():
        return hass.states.get("binary_sensor.kitchen_refrigerator_door").state

    def stages():
        return [event.data["stage"] for event in notifications]

    # The door opens and the channel reports it.
    client.state.return_value["ref_door_ajar"] = True
    await updates.put(StateUpdate({"ref_door_ajar": True}, full=False))
    await hass.async_block_till_done()
    assert door() == "on"
    # Five minutes later the appliance raises its door alert, still through the channel.
    await advance(300)
    assert door() == "on"
    alert = {"notif_seq": 1, "notif_type": 101, "timestamp": dt_util.utcnow().isoformat()}
    await updates.put(StateUpdate({"ref_door_ajar": True, "notifs": [alert]}, full=False))
    await settle()
    assert stages() == ["alert"]
    # The door closes at 5:30, but the channel has gone quiet and never reports it.
    await advance(330)
    client.state.return_value["ref_door_ajar"] = False
    if failure == "failed_read":
        client.state.side_effect = ApiError("Status request failed")
    elif failure == "rate_limited":
        client.state.side_effect = RateLimited(300)
    elif failure == "reconnect":
        await updates.put(ApiError("Sub-Zero's notification connection stopped responding."))
        await settle()
        assert door() == "on"
    await advance(450)
    if failure == "failed_read":
        client.state.side_effect = None
    elif failure == "reconnect":
        # The new connection reopens the channel and the appliance answers with a snapshot.
        await updates.put(StateUpdate(dict(client.state.return_value), full=True))
        await settle()
    await advance(590)
    if failure == "rate_limited":
        assert door() == "on"
        assert coordinator.confirmed_at < dt_util.utcnow() - timedelta(seconds=250)
    else:
        assert door() == "off"
    await advance(630)
    client.state.side_effect = None
    await advance(1000)
    assert door() == "off"
    assert stages() == (["alert", "plain"] if failure == "rate_limited" else ["alert"])
    assert hass.states.get("automation.confirmed_reminder").attributes["current"] == 0
