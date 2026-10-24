"""Appliance notification decoding and automation replay protection."""

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from homeassistant.helpers import entity_registry as er
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


async def test_every_event_type_has_a_display_name(hass, cloud_appliance):
    entity_id = "event.kitchen_appliance_event"
    assert er.async_get(hass).async_get(entity_id).translation_key == "appliance_event"
    event_types = hass.states.get(entity_id).attributes["event_types"]
    component = Path(__file__).parents[1] / "custom_components" / "subzero"
    for path in ("strings.json", "translations/en.json"):
        strings = json.loads((component / path).read_text())
        names = strings["entity"]["event"]["appliance_event"]["state_attributes"]["event_type"]
        assert sorted(names["state"]) == sorted(event_types)


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


async def test_slow_appliance_clock_must_catch_up_to_startup_cutoff(hass, cloud_appliance, freezer):
    started = dt_util.utcnow()
    for sequence, elapsed, expected in [(1, 0, None), (2, 6, "oven_preheated")]:
        freezer.move_to(started + timedelta(minutes=elapsed))
        payload = record(sequence, timestamp=dt_util.utcnow() - timedelta(minutes=5))
        parsed = parse_notification(
            {
                "type": 1,
                "target": "ConnectedApplianceMessage",
                "arguments": [
                    {"DeviceId": "appliance", "Payload": {"api.async_channel": {"pload": payload}}}
                ],
            },
            "test-owner",
            ["appliance"],
        )
        cloud_appliance.coordinator.apply_update(parsed[1])
        await hass.async_block_till_done()
        event = hass.states.get("event.kitchen_appliance_event")
        assert event.attributes["event_type"] == expected


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


@pytest.mark.parametrize("push_connected", [False, True])
async def test_recovery_delivers_every_event_before_reconnect(
    hass, cloud_appliance, push_connected
):
    client = cloud_appliance.client
    coordinator = cloud_appliance.coordinator
    client.push_connected = push_connected
    coordinator.async_set_update_error(ApiError("Disconnected"))
    events = async_capture_events(hass, "state_changed")
    cloud_appliance.state["notifs"] = [record(1, 201), record(2, 202)]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert [item.attributes["code"] for item in event_changes(events)] == [201, 202]

    client.push_connected = True
    coordinator.async_update_listeners()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert [item.attributes["code"] for item in event_changes(events)] == [201, 202]


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


async def test_events_at_the_last_supported_instant_are_ignored(hass, cloud_appliance):
    last = datetime.max.replace(tzinfo=UTC)
    # Evicting the oldest would move the cutoff past the last supported instant.
    history = [record(i, 201, last) for i in range(MAX_EVENT_HISTORY + 1)]
    await cloud_appliance.update({"notifs": history})
    assert not cloud_appliance.coordinator._event_ids
    assert hass.states.get("event.kitchen_appliance_event").state != "unavailable"


async def test_reload_baselines_previously_received_events(hass, cloud_appliance):
    await cloud_appliance.update({"notifs": [record()]})
    await hass.config_entries.async_reload(cloud_appliance.entry.entry_id)
    await hass.async_block_till_done()
    events = async_capture_events(hass, "state_changed")
    coordinator = cloud_appliance.entry.runtime_data.coordinators["appliance"]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not event_changes(events)


async def test_startup_history_is_a_baseline_even_from_a_fast_clock(hass, cloud_appliance):
    ahead = dt_util.utcnow() + timedelta(minutes=5)
    cloud_appliance.state["notifs"] = [record(1, 201, ahead)]
    await hass.config_entries.async_reload(cloud_appliance.entry.entry_id)
    await hass.async_block_till_done()
    events = async_capture_events(hass, "state_changed")
    coordinator = cloud_appliance.entry.runtime_data.coordinators["appliance"]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not event_changes(events)

    cloud_appliance.state["notifs"] = [record(1, 201, ahead), record(2, 202, ahead)]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert [item.attributes["code"] for item in event_changes(events)] == [202]


async def test_out_of_order_events_and_equal_timestamps_remain_distinct(hass, cloud_appliance):
    events = async_capture_events(hass, "state_changed")
    now = dt_util.utcnow()
    first = record(20, 201, now + timedelta(seconds=2))
    older = record(19, 205, now + timedelta(seconds=1))
    same_time = record(21, 202, now + timedelta(seconds=2))
    await cloud_appliance.update({"notifs": [first]})
    await cloud_appliance.update({"notifs": [same_time, older, first]})
    assert [item.attributes["code"] for item in event_changes(events)] == [201, 205, 202]


async def test_naive_event_timestamps_use_the_home_time_zone(hass, cloud_appliance):
    events = async_capture_events(hass, "state_changed")
    now = dt_util.now() + timedelta(seconds=1)
    # A status read reports the clock in UTC, which a naive timestamp does not borrow.
    cloud_appliance.state["time"] = now.astimezone(UTC).isoformat()
    await cloud_appliance.coordinator.async_refresh()
    await cloud_appliance.update({"notifs": [record(timestamp=now.replace(tzinfo=None))]})
    [event] = event_changes(events)
    assert event.attributes["appliance_timestamp"] == now.isoformat()


async def test_naive_event_timestamps_follow_the_pushed_clock(hass, cloud_appliance):
    events = async_capture_events(hass, "state_changed")
    zone = timezone(dt_util.now().utcoffset() + timedelta(hours=3))
    now = dt_util.now(zone) + timedelta(seconds=1)
    await cloud_appliance.update(
        {"time": now.isoformat(), "notifs": [record(timestamp=now.replace(tzinfo=None))]}
    )
    [event] = event_changes(events)
    assert event.attributes["appliance_timestamp"] == now.isoformat()


async def test_events_in_a_repeated_hour_are_identified_by_instant(hass, cloud_appliance):
    events = async_capture_events(hass, "state_changed")
    # Daylight saving ends that night in Home Assistant's zone, so 01:10 happens twice.
    await cloud_appliance.update(
        {
            "time": "2037-11-01T01:05:00-07:00",
            "notifs": [record(timestamp=datetime(2037, 11, 1, 1, 10))],
        }
    )
    assert cloud_appliance.coordinator.clock_zone is dt_util.get_default_time_zone()
    # The same event reported again in UTC.
    await cloud_appliance.update(
        {"notifs": [record(timestamp=datetime(2037, 11, 1, 8, 10, tzinfo=UTC))]}
    )
    [event] = event_changes(events)
    assert event.attributes["appliance_timestamp"] == "2037-11-01T01:10:00-07:00"


@pytest.mark.parametrize("model_changed", [False, True])
async def test_read_history_survives_a_concurrent_notification_push(
    hass, cloud_appliance, model_changed
):
    import asyncio

    coordinator = cloud_appliance.coordinator
    first = record(1, 201)
    second = record(2, 202, dt_util.utcnow() + timedelta(seconds=1))
    observed = []
    coordinator.async_add_event_listener(observed.append)
    cloud_appliance.state["notifs"] = [first, second]
    reading, release = asyncio.Event(), asyncio.Event()

    async def read(_device_id):
        snapshot = dict(cloud_appliance.state)
        reading.set()
        await release.wait()
        return snapshot

    cloud_appliance.client.state.side_effect = read
    pending = asyncio.create_task(coordinator.async_read_status())
    await reading.wait()
    await cloud_appliance.update(
        {
            **cloud_appliance.state,
            "notifs": [first],
            "appliance_model": "NEW-MODEL" if model_changed else "TEST-OVEN",
        },
        full=model_changed,
    )
    release.set()
    await pending
    assert [event["code"] for event in observed] == ([201] if model_changed else [201, 202])
