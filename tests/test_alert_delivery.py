"""Alert follow-up reads and delayed reminders after missed channel updates."""

import asyncio
from datetime import timedelta

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.subzero.api import StateUpdate
from tests.test_integration import loaded as loaded
from tests.test_state_verification import _advance_followup

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def door_alert(when, sequence=0):
    return {"notif_seq": sequence, "notif_type": 101, "timestamp": when.isoformat()}


async def test_an_alert_during_the_last_followup_read_starts_a_new_window(hass, freezer, loaded):
    entry, client, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    client.state.reset_mock()
    client.state.return_value["ref_door_ajar"] = True
    await updates.put(StateUpdate({"ref_door_ajar": True}, full=False))
    await hass.async_block_till_done()
    coordinator.async_receive_event(dt_util.utcnow(), 0, 101)
    await _advance_followup(hass, freezer, coordinator, 3)
    await _advance_followup(hass, freezer, coordinator, 61)
    assert client.state.await_count == 2
    reading, release = asyncio.Event(), asyncio.Event()

    async def read(_device_id):
        reading.set()
        await release.wait()
        return dict(client.state.return_value)

    client.state.side_effect = read
    freezer.tick(121)
    async_fire_time_changed(hass, dt_util.utcnow())
    await asyncio.wait_for(reading.wait(), 1)
    await updates.put(
        StateUpdate(
            {"ref_door_ajar": True, "notifs": [door_alert(dt_util.utcnow(), 1)]}, full=False
        )
    )
    await hass.async_block_till_done()
    release.set()
    await coordinator._read_task
    await hass.async_block_till_done()
    assert client.state.await_count == 3
    assert coordinator._followup_task is not None
    client.state.side_effect = None
    client.state.return_value["ref_door_ajar"] = False
    await _advance_followup(hass, freezer, coordinator, 61)
    assert client.state.await_count == 4
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    assert coordinator._followup_task is None


@pytest.mark.parametrize("startup_source", ["read", "snapshot"])
@pytest.mark.parametrize("history_source", ["read", "snapshot"])
async def test_historyless_startup_keeps_later_alerts(
    hass, freezer, loaded, startup_source, history_source
):
    entry, client, updates, _, _ = loaded
    assert "notifs" not in client.state.return_value
    if startup_source == "snapshot":
        await hass.config_entries.async_unload(entry.entry_id)
        client.state.reset_mock()
        await updates.put(StateUpdate(dict(client.state.return_value), full=True))
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        client.state.assert_not_awaited()
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    observed = []
    coordinator.async_add_event_listener(observed.append)
    started = dt_util.utcnow()
    freezer.tick(60)
    client.state.return_value["notifs"] = [
        door_alert(started - timedelta(seconds=1), 1),
        door_alert(dt_util.utcnow(), 2),
    ]
    for _ in range(2):
        if history_source == "snapshot":
            await updates.put(StateUpdate(dict(client.state.return_value), full=True))
        else:
            await coordinator.async_read_status()
        await hass.async_block_till_done()
    assert [(event["sequence"], event["source"]) for event in observed] == [(2, "history")]


@pytest.mark.parametrize("failure", ["none", "failed_read", "rate_limited", "reconnect"])
async def test_channel_alert_then_missed_door_close(hass, freezer, loaded, monkeypatch, failure):
    """The reported workflow: a door alert, then the door closes and the channel never says so.

    A reminder that reads status first never fires. A plain "open for 10 minutes"
    trigger is repaired by follow-up and scheduled reads, except while the cloud
    rate-limits status reads, when the sensor keeps its last value.
    """
    from homeassistant.helpers import device_registry as dr
    from homeassistant.setup import async_setup_component
    from pytest_homeassistant_custom_component.common import async_capture_events

    from custom_components.subzero.api import ApiError, RateLimited

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
    await updates.put(
        StateUpdate(
            {"ref_door_ajar": True, "notifs": [door_alert(dt_util.utcnow(), 1)]}, full=False
        )
    )
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
        assert door() == "unavailable"
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
