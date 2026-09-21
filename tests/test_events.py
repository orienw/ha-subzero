"""Appliance notification decoding and automation replay protection."""

import json
from datetime import timedelta

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.subzero.api import ApiError, parse_notification
from custom_components.subzero.const import MAX_EVENT_HISTORY

pytestmark = pytest.mark.parametrize(
    "cloud_appliance",
    [{"appliance_model": "TEST-OVEN", "appliance_type": "1.15.2.5", "notifs": []}],
    indirect=True,
)


def record(sequence=1, code=201, timestamp=None):
    return {
        "notif_seq": sequence,
        "notif_type": code,
        "timestamp": (timestamp or dt_util.utcnow()).isoformat(),
    }


def event_changes(events):
    return [
        event.data["new_state"]
        for event in events
        if event.data["entity_id"] == "event.kitchen_appliance_event"
        and event.data["new_state"] is not None
        and event.data["new_state"].state not in {"unknown", "unavailable"}
        and event.data["old_state"] is not None
        and event.data["old_state"].state != event.data["new_state"].state
    ]


async def test_live_event_becomes_an_event_entity(hass, cloud_appliance):
    assert hass.states.get("event.kitchen_appliance_event").state == "unknown"
    payload = record()
    envelope = {"DeviceId": "appliance", "Payload": {"api.async_channel": {"pload": payload}}}
    parsed = parse_notification(
        {"type": 1, "target": "ConnectedApplianceMessage", "arguments": [json.dumps(envelope)]},
        "test-owner",
        ["appliance"],
    )
    cloud_appliance.coordinator.apply_update(parsed[1])
    await hass.async_block_till_done()
    event = hass.states.get("event.kitchen_appliance_event")
    assert event.attributes["event_type"] == "oven_preheated"
    assert event.attributes["code"] == 201
    assert event.attributes["sequence"] == 1
    assert event.attributes["appliance_timestamp"] == payload["timestamp"]


async def test_history_duplicates_and_sequence_reset(hass, cloud_appliance):
    events = async_capture_events(hass, "state_changed")
    first = record(100, 201)
    second = record(1, 202, dt_util.utcnow() + timedelta(seconds=1))
    await cloud_appliance.update({"notifs": [first]})
    await cloud_appliance.update({"notifs": [first, second]}, full=False)
    await cloud_appliance.update({**cloud_appliance.state, "notifs": [first, second]}, full=True)
    assert [item.attributes["event_type"] for item in event_changes(events)] == [
        "oven_preheated",
        "lower_oven_preheated",
    ]


async def test_startup_and_stale_history_never_fire(hass, cloud_appliance):
    events = async_capture_events(hass, "state_changed")
    old = record(timestamp=dt_util.utcnow() - timedelta(days=1))
    await cloud_appliance.update({"notifs": [old]})
    assert hass.states.get("event.kitchen_appliance_event").state == "unknown"
    assert not event_changes(events)
    await cloud_appliance.update({"notifs": [record(2)]})
    last = hass.states.get("event.kitchen_appliance_event").state
    await cloud_appliance.update({"notifs": [old]})
    assert hass.states.get("event.kitchen_appliance_event").state == last


async def test_reconnect_history_delivers_new_events_once(hass, cloud_appliance):
    events = async_capture_events(hass, "state_changed")
    cloud_appliance.coordinator.async_set_update_error(ApiError("Disconnected"))
    first, second = record(1, 207), record(2, 302)
    await cloud_appliance.update({"notifs": [first]})
    await cloud_appliance.update({**cloud_appliance.state, "notifs": [first, second]}, full=True)
    assert [item.attributes["code"] for item in event_changes(events)] == [207, 302]


async def test_status_reads_deliver_only_new_history(hass, cloud_appliance):
    events = async_capture_events(hass, "state_changed")
    cloud_appliance.state["notifs"] = [record(1, 108), record(2, 109)]
    await cloud_appliance.coordinator.async_refresh()
    await cloud_appliance.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert [item.attributes["code"] for item in event_changes(events)] == [108, 109]


async def test_unknown_codes_remain_numeric_and_metadata_is_filtered(hass, cloud_appliance):
    payload = {**record(1, 111), "private_account": "do-not-expose"}
    await cloud_appliance.update({"notifs": [payload]})
    event = hass.states.get("event.kitchen_appliance_event")
    assert event.attributes["event_type"] == "unknown"
    assert event.attributes["code"] == 111
    assert "private_account" not in cloud_appliance.coordinator.data["notifs"][0]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        [None],
        [{"notif_seq": True, "notif_type": 201, "timestamp": "2026-09-21T00:00:00Z"}],
        [{"notif_seq": 1, "notif_type": True, "timestamp": "2026-09-21T00:00:00Z"}],
        [{"notif_seq": 1, "notif_type": 201, "timestamp": "not-a-date"}],
    ],
)
async def test_malformed_event_history_is_ignored(hass, cloud_appliance, payload):
    await cloud_appliance.update({"notifs": payload})
    assert hass.states.get("event.kitchen_appliance_event").state == "unknown"


async def test_bounded_history_does_not_replay_evicted_events(hass, cloud_appliance):
    events = async_capture_events(hass, "state_changed")
    now = dt_util.utcnow()
    history = [
        record(i, 201, now + timedelta(milliseconds=i)) for i in range(MAX_EVENT_HISTORY + 2)
    ]
    await cloud_appliance.update({"notifs": history})
    count = len(event_changes(events))
    assert count == MAX_EVENT_HISTORY + 2
    assert len(cloud_appliance.coordinator._event_ids) == MAX_EVENT_HISTORY
    await cloud_appliance.update({**cloud_appliance.state, "notifs": history}, full=True)
    assert len(event_changes(events)) == count


async def test_reload_baselines_previously_received_events(hass, cloud_appliance):
    await cloud_appliance.update({"notifs": [record()]})
    await hass.config_entries.async_reload(cloud_appliance.entry.entry_id)
    await hass.async_block_till_done()
    events = async_capture_events(hass, "state_changed")
    coordinator = cloud_appliance.entry.runtime_data.coordinators["appliance"]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not event_changes(events)


async def test_out_of_order_events_and_equal_timestamps_remain_distinct(hass, cloud_appliance):
    events = async_capture_events(hass, "state_changed")
    now = dt_util.utcnow()
    first = record(20, 201, now + timedelta(seconds=2))
    older = record(19, 205, now + timedelta(seconds=1))
    same_time = record(21, 202, now + timedelta(seconds=2))
    await cloud_appliance.update({"notifs": [first]})
    await cloud_appliance.update({"notifs": [same_time, older, first]})
    assert [item.attributes["code"] for item in event_changes(events)] == [201, 205, 202]


async def test_event_timestamp_uses_only_reported_offset(hass, cloud_appliance):
    now = dt_util.utcnow() + timedelta(seconds=1)
    payload = record(timestamp=now.replace(tzinfo=None))
    await cloud_appliance.update({"notifs": [payload]})
    assert hass.states.get("event.kitchen_appliance_event").state == "unknown"
    await cloud_appliance.update({"time": now.isoformat(), "notifs": [payload]})
    assert (
        hass.states.get("event.kitchen_appliance_event").attributes["appliance_timestamp"]
        == now.isoformat()
    )
