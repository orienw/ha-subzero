"""Optional Firebase delivery, replay protection and owned subscriptions."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from firebase_messaging.fcmpushclient import ErrorType
from homeassistant.config_entries import ConfigEntryState
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
            self.initial_credentials = credentials
            self.sequential_error_counters = {}
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


@fridge
@pytest.mark.parametrize("first_source", ["channel", "firebase"])
async def test_distinct_alerts_survive_missing_copies_and_clock_skew(
    cloud_appliance, alerts, freezer, first_source
):
    observed = []
    cloud_appliance.coordinator.async_add_event_listener(observed.append)
    freezer.tick(30)
    first = dt_util.utcnow().replace(microsecond=0)

    async def channel(when):
        await cloud_appliance.update(
            {
                **cloud_appliance.state,
                "notifs": [{"notif_seq": 0, "notif_type": 101, "timestamp": when.isoformat()}],
            }
        )

    if first_source == "channel":
        await channel(first + timedelta(seconds=9))
    else:
        alerts._on_alert(message(code=101, sequence=0, timestamp=first), "first", None)

    freezer.tick(61)
    second = first + timedelta(seconds=61)
    if first_source == "channel":
        alerts._on_alert(message(code=101, sequence=0, timestamp=second), "second", None)
    else:
        await channel(second - timedelta(seconds=9))

    assert len(observed) == 2


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


@fridge
async def test_enabling_and_disabling_starts_and_cleans_up_our_listener(
    hass, cloud_appliance, alerts, push_clients
):
    entry = cloud_appliance.entry
    account = entry.runtime_data
    coordinator = cloud_appliance.coordinator
    changes = async_capture_events(hass, "state_changed")
    entity_id = "binary_sensor.kitchen_refrigerator_door"

    async def configure(enabled):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "device_ids": ["appliance"],
                "firebase_alerts": enabled,
                "status_poll_interval": "300",
            },
        )
        await hass.async_block_till_done()
        assert entry.runtime_data is account
        assert account.coordinators["appliance"] is coordinator
        assert hass.states.get(entity_id).state != "unavailable"
        return account.alerts

    assert account.alerts is None
    manager = await configure(True)
    assert manager is not None and manager.subscription_healthy
    assert len(push_clients) == 1
    own_psid = entry.data["fcm_psid"]
    assert len(own_psid) == 16
    credentials = entry.data["fcm_credentials"]
    assert credentials == push_clients[0].credentials
    await cloud_appliance.update({"ref_door_ajar": True})
    assert hass.states.get(entity_id).state == "on"
    await configure(False)
    assert len(push_clients) == 1
    assert not push_clients[0].do_listen
    assert all(task.done() for task in push_clients[0].tasks)
    assert entry.data["fcm_registration_ids"] == {}
    assert entry.data["fcm_credentials"] == credentials
    cloud_appliance.client.unsubscribe_all_alerts.assert_awaited_once_with(own_psid, "appliance")
    manager._on_alert(message(), "after-stop", None)
    assert manager.received == 0
    await cloud_appliance.update({"ref_door_ajar": False})
    assert hass.states.get(entity_id).state == "off"
    resumed = await configure(True)
    assert resumed is not None and resumed.subscription_healthy
    assert len(push_clients) == 2
    assert push_clients[1].initial_credentials == credentials
    assert entry.data["fcm_psid"] == own_psid
    assert not any(
        event.data["entity_id"] == entity_id
        and (event.data["new_state"] is None or event.data["new_state"].state == "unavailable")
        for event in changes
    )
    await hass.config_entries.async_unload(entry.entry_id)
    assert all(task.done() for push in push_clients for task in push.tasks)


@fridge
async def test_disabling_firebase_during_checkin_does_not_start_subscription_sync(
    hass, cloud_appliance, alerts, push_clients, monkeypatch
):
    entry = cloud_appliance.entry
    account = entry.runtime_data
    client = cloud_appliance.client
    checkin_started = asyncio.Event()
    release_checkin = asyncio.Event()

    async def delayed_checkin(push):
        checkin_started.set()
        await release_checkin.wait()
        push.save(push.credentials)
        return push.credentials["fcm"]["registration"]["token"]

    monkeypatch.setattr(
        "custom_components.subzero.fcm.FcmPushClient.checkin_or_register", delayed_checkin
    )
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "firebase_alerts": True}
    )
    try:
        await asyncio.wait_for(checkin_started.wait(), 1)
        starting = account.alerts
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, "firebase_alerts": False}
        )
        await asyncio.wait_for(starting._stop.wait(), 1)
    finally:
        release_checkin.set()
    await asyncio.wait_for(hass.async_block_till_done(), 1)
    for request in (
        client.alert_registration,
        client.register_for_alerts,
        client.alert_types,
        client.subscribe_alerts,
        client.unsubscribe_all_alerts,
    ):
        request.assert_not_awaited()
    assert entry.runtime_data is account
    assert account.alerts is None
    assert len(push_clients) == 1
    assert not push_clients[0].do_listen
    assert all(task.done() for task in push_clients[0].tasks)


@fridge
async def test_persisted_credentials_and_interval_changes_do_not_restart_firebase(
    hass, cloud_appliance, alerts, push_clients
):
    entry = cloud_appliance.entry
    account = entry.runtime_data
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "firebase_alerts": True}
    )
    await hass.async_block_till_done()
    manager = account.alerts
    assert manager is not None and manager.subscription_healthy
    manager._save_credentials({**entry.data["fcm_credentials"], "persisted": True})
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, "tokens": {**entry.data["tokens"], "refresh_token": "renewed"}},
        options={**entry.options, "status_poll_interval": 60},
    )
    await hass.async_block_till_done()
    assert entry.runtime_data is account
    assert account.alerts is manager
    assert len(push_clients) == 1
    assert push_clients[0].do_listen
    cloud_appliance.client.subscribe_alerts.assert_awaited_once()
    cloud_appliance.client.unsubscribe_all_alerts.assert_not_awaited()


@fridge
@pytest.mark.parametrize("unload", [False, True])
async def test_firebase_changes_wait_for_stop_and_respect_unload(
    hass, cloud_appliance, alerts, push_clients, monkeypatch, unload
):
    entry = cloud_appliance.entry
    account = entry.runtime_data
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "firebase_alerts": True}
    )
    await hass.async_block_till_done()
    assert account.alerts is not None and account.alerts.subscription_healthy
    first = push_clients[0]
    stop_started = asyncio.Event()
    release_stop = asyncio.Event()
    original_stop = type(first).stop

    async def delayed_stop(push):
        if push is first:
            stop_started.set()
            await release_stop.wait()
        await original_stop(push)

    monkeypatch.setattr(type(first), "stop", delayed_stop)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "firebase_alerts": False}
    )
    try:
        await asyncio.wait_for(stop_started.wait(), 1)
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, "firebase_alerts": True}
        )
        await asyncio.sleep(0)
        assert len(push_clients) == 1
        if unload:
            unloading = asyncio.create_task(hass.config_entries.async_unload(entry.entry_id))
            await asyncio.sleep(0)
            assert entry.state is ConfigEntryState.UNLOAD_IN_PROGRESS
    finally:
        release_stop.set()
    await asyncio.wait_for(hass.async_block_till_done(), 1)
    assert not first.do_listen
    assert all(task.done() for task in first.tasks)
    if unload:
        assert await unloading
        assert len(push_clients) == 1
        assert entry.state is ConfigEntryState.NOT_LOADED
    else:
        assert entry.runtime_data is account
        assert account.alerts.subscription_healthy
        assert len(push_clients) == 2
        assert push_clients[1].do_listen
    cloud_appliance.client.unsubscribe_all_alerts.assert_not_awaited()


@fridge
async def test_reenabling_firebase_waits_for_server_cleanup_before_subscribing(
    hass, cloud_appliance, alerts, push_clients
):
    entry = cloud_appliance.entry
    account = entry.runtime_data
    client = cloud_appliance.client
    subscribed = set()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    replacement_read = asyncio.Event()
    cleanup_cancelled = False
    server_cleanup = None

    async def read_subscriptions(psid, device_id):
        if cleanup_started.is_set():
            replacement_read.set()
        return set(subscribed)

    async def subscribe(psid, device_id, keys):
        subscribed.update(keys)

    async def remote_cleanup():
        await release_cleanup.wait()
        if cleanup_cancelled:
            await replacement_read.wait()
        subscribed.clear()

    async def delayed_cleanup(psid, device_id):
        nonlocal cleanup_cancelled, server_cleanup
        server_cleanup = asyncio.create_task(remote_cleanup())
        cleanup_started.set()
        try:
            await asyncio.shield(server_cleanup)
        except asyncio.CancelledError:
            cleanup_cancelled = True
            raise

    client.alert_types.side_effect = read_subscriptions
    client.subscribe_alerts.side_effect = subscribe
    client.unsubscribe_all_alerts.side_effect = delayed_cleanup
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "firebase_alerts": True}
    )
    await hass.async_block_till_done()
    assert account.alerts is not None and account.alerts.subscription_healthy
    desired = set(subscribed)
    assert desired
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "firebase_alerts": False}
    )
    try:
        await asyncio.wait_for(cleanup_started.wait(), 1)
        cleaning = account.alerts
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, "firebase_alerts": True}
        )
        await asyncio.wait_for(cleaning._stop.wait(), 1)
        await asyncio.sleep(0)
        release_cleanup.set()
        await asyncio.wait_for(hass.async_block_till_done(), 1)
        await asyncio.wait_for(server_cleanup, 1)
    finally:
        release_cleanup.set()
    assert entry.runtime_data is account
    assert account.alerts.subscription_healthy
    assert subscribed == desired
    assert len(push_clients) == 2
    assert not push_clients[0].do_listen
    assert all(task.done() for task in push_clients[0].tasks)
    assert push_clients[1].do_listen
    assert client.subscribe_alerts.await_count == 2


@fridge
async def test_disabling_firebase_waits_for_server_subscription_before_cleanup(
    hass, cloud_appliance, alerts, push_clients
):
    entry = cloud_appliance.entry
    account = entry.runtime_data
    client = cloud_appliance.client
    subscribed = set()
    subscribe_started = asyncio.Event()
    release_subscribe = asyncio.Event()
    cleanup_finished = asyncio.Event()
    subscribe_cancelled = False
    server_subscription = None

    async def remote_subscribe(keys):
        await release_subscribe.wait()
        if subscribe_cancelled:
            await cleanup_finished.wait()
        subscribed.update(keys)

    async def delayed_subscribe(psid, device_id, keys):
        nonlocal subscribe_cancelled, server_subscription
        server_subscription = asyncio.create_task(remote_subscribe(keys))
        subscribe_started.set()
        try:
            await asyncio.shield(server_subscription)
        except asyncio.CancelledError:
            subscribe_cancelled = True
            raise

    async def cleanup(psid, device_id):
        subscribed.clear()
        cleanup_finished.set()

    client.subscribe_alerts.side_effect = delayed_subscribe
    client.unsubscribe_all_alerts.side_effect = cleanup
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "firebase_alerts": True}
    )
    try:
        await asyncio.wait_for(subscribe_started.wait(), 1)
        subscribing = account.alerts
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, "firebase_alerts": False}
        )
        await asyncio.wait_for(subscribing._stop.wait(), 1)
        await asyncio.sleep(0)
        release_subscribe.set()
        await asyncio.wait_for(hass.async_block_till_done(), 1)
        await asyncio.wait_for(server_subscription, 1)
    finally:
        release_subscribe.set()
    assert entry.runtime_data is account
    assert cleanup_finished.is_set()
    assert not subscribed
    assert entry.data["fcm_registration_ids"] == {}
    assert len(push_clients) == 1
    assert not push_clients[0].do_listen
    assert all(task.done() for task in push_clients[0].tasks)


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


@oven
@pytest.mark.parametrize("registration_fails", [False, True])
async def test_rejected_gcm_credentials_are_replaced_without_changing_our_subscription_identity(
    hass, freezer, cloud_appliance, alerts, push_clients, monkeypatch, registration_fails
):
    old = {"fcm": {"registration": {"token": "rejected-token"}}}
    alerts._save_credentials(old)
    alerts.start()
    await hass.async_block_till_done()
    first = push_clients[0]
    psid = alerts.entry.data["fcm_psid"]
    ids = dict(alerts.entry.data["fcm_registration_ids"])
    first.sequential_error_counters[ErrorType.LOGIN] = 3
    first.do_listen = False
    for task in first.tasks:
        task.cancel()
    await asyncio.gather(*first.tasks, return_exceptions=True)
    if registration_fails:
        original = type(first).checkin_or_register

        async def checkin(push):
            if len(push_clients) == 2:
                raise RuntimeError("Registration temporarily failed")
            return await original(push)

        monkeypatch.setattr(type(first), "checkin_or_register", checkin)
    freezer.tick(61)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert push_clients[1].initial_credentials is None
    if registration_fails:
        freezer.tick(301)
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        assert push_clients[2].initial_credentials is None
    assert alerts.entry.data["fcm_credentials"]["fcm"]["registration"]["token"] == "test-token"
    assert alerts.entry.data["fcm_psid"] == psid
    assert alerts.entry.data["fcm_registration_ids"] == ids
