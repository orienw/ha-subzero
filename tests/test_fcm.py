"""Optional Firebase delivery, replay protection and owned subscriptions."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.subzero import async_remove_entry
from custom_components.subzero.alert_loc_keys import ALERT_LOCATION_KEYS
from custom_components.subzero.api import ApiError, RateLimited
from custom_components.subzero.fcm import FcmAlerts, parse_alert

oven = pytest.mark.parametrize(
    "cloud_appliance",
    [
        {
            "appliance_model": "TEST-OVEN",
            "appliance_type": "1.15.2.5",
            "cav_set_temp": 350,
            "notifs": [],
        }
    ],
    indirect=True,
)
fridge = pytest.mark.parametrize(
    "cloud_appliance",
    [
        {
            "appliance_model": "TEST-FRIDGE",
            "appliance_type": "1.1.1.0",
            "ref_set_temp": 38,
            "ref_door_ajar": False,
            "notifs": [],
        }
    ],
    indirect=True,
)


def message(device_id="appliance", code=209, sequence=50, timestamp=None):
    when = timestamp or dt_util.utcnow() + timedelta(seconds=2)
    return {
        "data": {
            "deviceId": device_id,
            "value": str(code),
            "notifSeq": str(sequence),
            "event_time": str(int(when.timestamp() * 1000)),
        }
    }


@pytest.fixture
async def alerts(hass, cloud_appliance):
    client = cloud_appliance.client
    client.alert_registration = AsyncMock(return_value=None)
    client.register_for_alerts = AsyncMock()
    client.alert_types = AsyncMock(return_value=set())
    client.subscribe_alerts = AsyncMock()
    client.unsubscribe_alerts = AsyncMock()
    client.unsubscribe_all_alerts = AsyncMock()
    manager = FcmAlerts(
        hass, cloud_appliance.entry, client, cloud_appliance.entry.runtime_data.coordinators
    )
    yield manager
    await manager.stop()


@pytest.fixture
def push_clients(monkeypatch):
    clients = []

    class Push:
        def __init__(self, callback, config, credentials, save, **kwargs):
            self.callback = callback
            self.credentials = credentials or {"fcm": {"registration": {"token": "test-token"}}}
            self.save = save
            self.tasks = []
            self.do_listen = False
            clients.append(self)

        async def checkin_or_register(self):
            self.save(self.credentials)
            return self.credentials["fcm"]["registration"]["token"]

        async def start(self):
            self.do_listen = True
            self.tasks = [asyncio.create_task(asyncio.Event().wait())]

        async def stop(self):
            self.do_listen = False
            for task in self.tasks:
                task.cancel()

        def is_started(self):
            return self.do_listen

    monkeypatch.setattr("custom_components.subzero.fcm.FcmPushClient", Push)
    return clients


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"data": None},
        message(sequence=-1),
        message(code="bad"),
        {
            "data": {
                "deviceId": "appliance",
                "value": "201",
                "notifSeq": "1",
                "event_time": "999999999999999999999",
            }
        },
    ],
)
def test_malformed_firebase_messages_are_ignored(payload):
    assert parse_alert(payload) is None


@oven
async def test_alerts_preserve_occurrence_and_receipt_time_without_changing_state(
    hass, cloud_appliance, alerts
):
    coordinator = cloud_appliance.coordinator
    confirmed = coordinator.confirmed_at
    timestamp = dt_util.utcnow() + timedelta(seconds=2)
    alerts._on_alert(message(timestamp=timestamp), "persistent-id", None)
    event = hass.states.get("event.kitchen_appliance_event")
    assert event.attributes["event_type"] == "kitchen_timer_under_one_minute"
    assert event.attributes["source"] == "firebase"
    assert (
        event.attributes["appliance_timestamp"]
        == dt_util.utc_from_timestamp(int(timestamp.timestamp() * 1000) / 1000).isoformat()
    )
    assert dt_util.parse_datetime(event.attributes["received_at"]) <= dt_util.utcnow()
    assert coordinator.data["cav_set_temp"] == 350
    assert coordinator.confirmed_at == confirmed
    for payload in (message(device_id="unselected"), {"data": None}):
        alerts._on_alert(payload, "other", None)
    assert alerts.received == 1


@oven
@pytest.mark.parametrize("sequence", [0, 50])
@pytest.mark.parametrize(
    "order",
    [
        ("channel", "history", "firebase"),
        ("firebase", "channel", "history"),
        ("history", "firebase", "channel"),
    ],
)
async def test_cross_source_copies_do_not_replay_repeated_events(
    cloud_appliance, alerts, sequence, order
):
    coordinator = cloud_appliance.coordinator
    observed = []
    coordinator.async_add_event_listener(observed.append)
    base = (dt_util.utcnow() + timedelta(seconds=2)).replace(microsecond=0)
    for index in range(2):
        instant = base + timedelta(seconds=index * 6)
        precise = instant + timedelta(milliseconds=872)
        for source in order:
            if source == "firebase":
                alerts._on_alert(message(sequence=sequence, timestamp=instant), "id", None)
            else:
                await cloud_appliance.update(
                    {
                        **cloud_appliance.state,
                        "notifs": [
                            {
                                "notif_seq": sequence,
                                "notif_type": 209,
                                "timestamp": precise.isoformat(),
                            }
                        ],
                    },
                    full=source == "history",
                )
    assert len(observed) == 2
    assert all(event["sequence"] == sequence for event in observed)


@oven
async def test_old_alerts_are_ignored_and_late_alerts_keep_their_occurrence_time(
    hass, freezer, cloud_appliance, alerts
):
    original = dt_util.utcnow()
    alerts._on_alert(message(timestamp=original - timedelta(minutes=1)), "old", None)
    assert hass.states.get("event.kitchen_appliance_event").state == "unknown"
    freezer.tick(600)
    instant = original + timedelta(seconds=1)
    alerts._on_alert(message(timestamp=instant), "late", None)
    event = hass.states.get("event.kitchen_appliance_event")
    assert (
        event.attributes["appliance_timestamp"]
        == dt_util.utc_from_timestamp(int(instant.timestamp() * 1000) / 1000).isoformat()
    )
    assert dt_util.parse_datetime(event.attributes["received_at"]) == dt_util.utcnow()


@oven
async def test_registration_sync_reuses_our_id_and_skips_healthy_subscriptions(
    cloud_appliance, alerts
):
    client = cloud_appliance.client
    assert await alerts._sync("own-psid", "test-token")
    registration = client.register_for_alerts.await_args.args[0]
    assert registration["psid"] == "own-psid"
    assert registration["mobile_name"] == "Home Assistant"
    assert cloud_appliance.entry.data["fcm_registration_ids"] == {"appliance": registration["id"]}
    assert await alerts._sync("own-psid", "test-token")
    client.alert_registration.assert_awaited_once()
    client.subscribe_alerts.assert_awaited_once()
    client.alert_registration.return_value = {**registration, "fcmToken": "test-token"}
    assert await alerts._sync("own-psid", "rotated-token")
    assert client.register_for_alerts.await_args.kwargs == {"update": True}
    assert client.register_for_alerts.await_args.args[0]["id"] == registration["id"]
    assert client.register_for_alerts.await_args.args[0]["fcmToken"] == "rotated-token"


@oven
@pytest.mark.parametrize("confirmed", [False, True])
async def test_registration_http_500_requires_readback_confirmation(
    cloud_appliance, alerts, confirmed
):
    client = cloud_appliance.client
    client.register_for_alerts.side_effect = ApiError("HTTP 500", status=500)
    client.alert_registration.side_effect = [
        None,
        {"id": "ours", "fcmToken": "test-token"} if confirmed else None,
    ]
    assert await alerts._sync("own-psid", "test-token") is confirmed
    assert client.subscribe_alerts.await_count == int(confirmed)
    assert "appliance" in cloud_appliance.entry.data["fcm_registration_ids"]


@oven
async def test_range_localization_changes_update_our_subscriptions(cloud_appliance, alerts):
    client = cloud_appliance.client
    assert await alerts._sync("own-psid", "token")
    client.alert_types.return_value = {code for code in ALERT_LOCATION_KEYS if code // 100 == 2}
    cloud_appliance.coordinator.data["appliance_type"] = "1.8.2.5"
    cloud_appliance.coordinator.data["cav2_set_temp"] = 350
    assert await alerts._sync("own-psid", "token")
    keys = client.subscribe_alerts.await_args.args[2]
    assert "right" in keys[201] and "left" in keys[202]
    assert "right" in keys[218] and "left" in keys[219]
    assert 201 in client.unsubscribe_alerts.await_args.args[2]


@oven
async def test_cleanup_retains_failed_ids_and_only_removes_owned_subscriptions(
    cloud_appliance, alerts
):
    client = cloud_appliance.client
    alerts._save(fcm_psid="own-psid", fcm_registration_ids={"removed-appliance": "ours"})
    client.unsubscribe_all_alerts.side_effect = [ApiError("Offline"), None]
    assert not await alerts._remove_subscriptions("own-psid", set())
    assert alerts.entry.data["fcm_registration_ids"] == {"removed-appliance": "ours"}
    assert await alerts._remove_subscriptions("own-psid", set())
    assert alerts.entry.data["fcm_registration_ids"] == {}
    assert all(
        call.args == ("own-psid", "removed-appliance")
        for call in client.unsubscribe_all_alerts.await_args_list
    )


@oven
async def test_saved_credentials_and_registration_ids_are_copied(alerts):
    ids = {"appliance": "ours"}
    credentials = {"fcm": {"registration": {"token": "old-token"}}}
    alerts._save(fcm_registration_ids=ids)
    alerts._save_credentials(credentials)
    ids.clear()
    credentials["fcm"]["registration"]["token"] = "changed"
    assert alerts.entry.data["fcm_registration_ids"] == {"appliance": "ours"}
    assert alerts.entry.data["fcm_credentials"]["fcm"]["registration"]["token"] == "old-token"


@oven
async def test_enabling_and_disabling_starts_and_cleans_up_our_listener(
    hass, cloud_appliance, alerts, push_clients
):
    entry = cloud_appliance.entry
    assert entry.runtime_data.alerts is None
    hass.config_entries.async_update_entry(entry, options={"firebase_alerts": True})
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    manager = entry.runtime_data.alerts
    assert manager is not None and manager.subscription_healthy
    assert len(push_clients) == 1
    own_psid = entry.data["fcm_psid"]
    assert len(own_psid) == 16
    assert entry.data["fcm_credentials"] == push_clients[0].credentials
    hass.config_entries.async_update_entry(entry, options={})
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert len(push_clients) == 1
    assert not push_clients[0].do_listen
    assert all(task.done() for task in push_clients[0].tasks)
    assert entry.data["fcm_registration_ids"] == {}
    cloud_appliance.client.unsubscribe_all_alerts.assert_awaited_once_with(own_psid, "appliance")
    manager._on_alert(message(), "after-stop", None)
    assert manager.received == 0


@oven
async def test_a_failed_sdk_listener_is_restarted_and_all_tasks_stop(
    hass, freezer, cloud_appliance, alerts, push_clients
):
    alerts.start()
    await hass.async_block_till_done()
    first = push_clients[0]
    first.tasks[0].cancel()
    await asyncio.gather(*first.tasks, return_exceptions=True)
    freezer.tick(61)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert len(push_clients) == 2
    await alerts.stop()
    assert all(task.done() for push in push_clients for task in push.tasks)


@oven
async def test_removing_an_unloaded_entry_cleans_saved_subscriptions(hass, cloud_appliance, alerts):
    entry = cloud_appliance.entry
    alerts._save(fcm_psid="own-psid", fcm_registration_ids={"appliance": "ours"})
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await async_remove_entry(hass, entry)
    cloud_appliance.client.unsubscribe_all_alerts.assert_awaited_once_with("own-psid", "appliance")


@fridge
@pytest.mark.parametrize("rate_limited", [False, True])
async def test_door_alert_missed_close_and_delayed_reminder(
    hass, freezer, cloud_appliance, alerts, rate_limited
):
    coordinator = cloud_appliance.coordinator
    events = []
    notifications = async_capture_events(hass, "test_door_notification")
    notified = asyncio.Event()
    hass.bus.async_listen("test_door_notification", lambda _: notified.set())
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "alias": "Door reminder",
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
                        {"event": "test_door_notification", "event_data": {"stage": "initial"}},
                        {"delay": {"minutes": 10}},
                        {
                            "action": "subzero.read_status",
                            "data": {"device_id": cloud_appliance.device_id},
                        },
                        {
                            "condition": "state",
                            "entity_id": "binary_sensor.kitchen_refrigerator_door",
                            "state": "on",
                        },
                        {"event": "test_door_notification", "event_data": {"stage": "reminder"}},
                    ],
                }
            ]
        },
    )
    coordinator.async_add_event_listener(events.append)
    await cloud_appliance.update({"ref_door_ajar": True})
    confirmed = coordinator.confirmed_at
    alerts._on_alert(message(code=101), "door-alert", None)
    await asyncio.wait_for(notified.wait(), 1)
    assert len(events) == 1
    assert events[0]["code"] == 101
    assert coordinator.confirmed_at == confirmed
    if rate_limited:
        cloud_appliance.client.state.side_effect = RateLimited(900)
    freezer.tick(3)
    async_fire_time_changed(hass, dt_util.utcnow())
    await asyncio.sleep(0)
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"
    cloud_appliance.state["ref_door_ajar"] = False
    freezer.tick(61)
    async_fire_time_changed(hass, dt_util.utcnow())
    await asyncio.sleep(0)
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == (
        "on" if rate_limited else "off"
    )
    freezer.tick(600)
    async_fire_time_changed(hass, dt_util.utcnow())
    await asyncio.wait_for(hass.async_block_till_done(), 1)
    assert [event.data["stage"] for event in notifications] == ["initial"]
    assert len(events) == 1
    assert hass.states.get("automation.door_reminder").attributes["current"] == 0
