"""Status reads, pushed state, confirmation and recovery."""

import asyncio
import logging
from datetime import timedelta
from unittest.mock import Mock

import pytest
from homeassistant.config_entries import RELOAD_AFTER_UPDATE_DELAY
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.subzero.api import ApiError, RateLimited, StateUpdate
from custom_components.subzero.coordinator import SubZeroCoordinator
from tests.common import async_fire_time_changed

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

DOOR = "binary_sensor.kitchen_refrigerator_door"
SETPOINT = "sensor.kitchen_refrigerator_setpoint"
MODEL = "ANOTHER-MODEL"
# A snapshot without the door, which leaves the door unknown where it replaces state.
INCOMPLETE = {"appliance_model": MODEL, "ref_set_temp": 38}


def partial(**properties) -> StateUpdate:
    return StateUpdate(properties, full=False)


def snapshot(properties: dict | None = None, **more) -> StateUpdate:
    return StateUpdate({**(properties or {}), **more}, full=True)


def read_during(coordinator, client, *updates: StateUpdate, reply=None, error=None):
    """Deliver pushed updates during a status read, then return its reply or raise."""

    async def read(_device_id):
        answer = dict(client.state.return_value if reply is None else reply)
        for update in updates:
            coordinator.apply_update(update)
        if error is not None:
            raise error
        return answer

    client.state.side_effect = read


def blocked_read(client, *, reply=None, error=None):
    """Hold every status read until released; returns the (reading, release) events."""
    reading, release = asyncio.Event(), asyncio.Event()

    async def read(_device_id):
        reading.set()
        await release.wait()
        if error is not None:
            raise error
        return dict(client.state.return_value if reply is None else reply)

    client.state.side_effect = read
    return reading, release


def first_read_blocks(client, *, then=None):
    """Hold the first status read until released; later reads answer, or fail with ``then``."""
    reading, release = asyncio.Event(), asyncio.Event()
    client.state.reset_mock()

    async def read(_device_id):
        answer = dict(client.state.return_value)
        if client.state.await_count == 1:
            reading.set()
            await release.wait()
        elif then is not None:
            raise then
        return answer

    client.state.side_effect = read
    return reading, release


async def read_confirms(coordinator):
    result = await coordinator.async_read_status()
    assert result.confirmed_at == dt_util.utcnow()
    return result


async def reconnect(hass, freezer, updates, client):
    """Drop the push stream and let it reconnect with a complete snapshot."""
    await updates.put(ApiError("Stream disconnected"))
    await hass.async_block_till_done()
    freezer.tick(31)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    await updates.put(snapshot(client.state.return_value))
    await hass.async_block_till_done()


@pytest.fixture
def fridge(loaded):
    """The Kitchen refrigerator's coordinator and the client it reads from.

    Captured before the test runs, so a test that reloads the entry looks the new
    coordinator up itself.
    """
    entry, client, _, _, _ = loaded
    return entry.runtime_data.coordinators["test-fridge"], client


@pytest.fixture
async def enabled_confirmation_sensor(hass, loaded, freezer):
    entity_id = "sensor.kitchen_state_confirmed"
    er.async_get(hass).async_update_entity(entity_id, disabled_by=None)
    await hass.async_block_till_done()
    freezer.tick(RELOAD_AFTER_UPDATE_DELAY + 1)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    return entity_id


# Older full pushes


@pytest.fixture
def dated_snapshots(fridge):
    """A fresh reply and an older snapshot, by appliance clock and uptime."""
    _, client = fridge
    fresh = {
        **client.state.return_value,
        "ref_door_ajar": True,
        "time": "2025-01-01T12:00:00+00:00",
        "uptime": "13:00:00",
    }
    older = {
        **fresh,
        "ref_door_ajar": False,
        "time": "2025-01-01T04:00:00-07:00",
        "uptime": "12:00:00",
    }
    return fresh, older


@pytest.fixture
async def dated(fridge, dated_snapshots):
    """The coordinator, client and dated snapshots after the fresh reply was read."""
    coordinator, client = fridge
    client.state.return_value = dated_snapshots[0]
    await coordinator.async_read_status()
    return coordinator, client, *dated_snapshots


async def test_old_full_push_does_not_publish_confirm_or_clear_an_error(hass, freezer, dated):
    coordinator, _, _, older = dated
    confirmed = coordinator.confirmed_at
    coordinator.async_receive_event(dt_util.utcnow(), 0, 101)
    followup = coordinator._followup_task
    assert followup is not None
    error = ApiError("Appliance unavailable")
    coordinator.async_set_update_error(error)
    published = Mock()
    coordinator.async_add_listener(published)
    freezer.tick(1)

    coordinator.apply_update(snapshot(older))
    await hass.async_block_till_done()

    assert coordinator.data["ref_door_ajar"] is True
    assert coordinator.confirmed_at == confirmed
    assert not coordinator.last_update_success
    assert coordinator.last_exception is error
    assert coordinator._followup_task is followup
    assert not followup.cancelling()
    published.assert_not_called()


async def test_old_full_push_still_shows_the_channel_is_open(dated):
    coordinator, client, _, older = dated
    coordinator.on_channel_error(ApiError("Channel refused"))
    reads = client.state.await_count
    # The snapshot is ignored as state, but it arrived on the appliance's channel.
    assert coordinator.apply_update(snapshot(older), recover=True) is False
    await asyncio.sleep(0)
    assert client.state.await_count == reads + 1
    assert coordinator.live_updates_established
    assert coordinator.data["ref_door_ajar"] is True


@pytest.mark.parametrize(
    ("appliance_time", "uptime"),
    [
        ("2025-01-01T11:00:00+00:00", "14:00:00"),
        ("2025-01-01T13:00:00+00:00", "0:01:00"),
    ],
    ids=["clock-correction", "reboot"],
)
async def test_full_push_accepts_clock_correction_or_reboot(freezer, dated, appliance_time, uptime):
    coordinator, _, _, older = dated
    freezer.tick(1)
    coordinator.apply_update(snapshot(older, time=appliance_time, uptime=uptime))
    assert coordinator.data["ref_door_ajar"] is False
    assert coordinator.confirmed_at == dt_util.utcnow()


async def test_direct_read_rebases_snapshot_age(dated):
    coordinator, client, fresh, older = dated
    client.state.return_value = older
    await coordinator.async_read_status()
    assert coordinator.data["time"] == older["time"]

    coordinator.apply_update(snapshot(fresh, time="2025-01-01T11:30:00+00:00", uptime="12:30:00"))
    assert coordinator.data["ref_door_ajar"] is True
    assert coordinator.data["uptime"] == "12:30:00"


@pytest.mark.parametrize(("uptime", "accepted"), [("12:59:01", True), ("12:59:00", False)])
async def test_old_full_push_allows_uptime_truncation_tolerance(dated, uptime, accepted):
    coordinator, _, _, older = dated
    coordinator.apply_update(snapshot(older, uptime=uptime))
    assert coordinator.data["ref_door_ajar"] is (not accepted)


@pytest.mark.parametrize(
    ("field", "value"),
    [("time", None), ("time", "invalid"), ("uptime", None), ("uptime", "invalid")],
)
async def test_full_push_without_usable_age_metadata_is_accepted(dated, field, value):
    coordinator, _, _, older = dated
    if value is None:
        del older[field]
    else:
        older[field] = value
    coordinator.apply_update(snapshot(older))
    assert coordinator.data["ref_door_ajar"] is False
    assert coordinator.last_update_success


# Pushes during a read


async def test_partial_push_does_not_confirm_unchanged_properties(hass, freezer, loaded, fridge):
    _, _, updates, _, _ = loaded
    coordinator, _ = fridge
    confirmed = coordinator.confirmed_at
    assert confirmed is not None
    freezer.tick(1)
    await updates.put(partial(ref_set_temp=39))
    await hass.async_block_till_done()
    assert coordinator.confirmed_at == confirmed


async def test_debug_logs_distinguish_observed_and_published_state_without_private_values(
    hass, fridge, caplog
):
    coordinator, client = fridge
    caplog.set_level(logging.DEBUG, logger="custom_components.subzero.coordinator")
    caplog.clear()
    read_during(
        coordinator,
        client,
        partial(ref_door_ajar=True),
        reply={
            **client.state.return_value,
            "ap_ssid": "redact-network",
            "appliance_serial": "redact-serial",
            "unknown_token": "redact-token",
            "version": {"secret": "redact-nested"},
        },
    )
    await coordinator.async_read_status()
    replies = [record.message for record in caplog.records if "status response" in record.message]
    publications = [
        record.message for record in caplog.records if "state published" in record.message
    ]
    assert len(replies) == 1
    assert "'ref_door_ajar': False" in replies[0]
    assert "'ref_door_ajar': True" in publications[-1]
    assert hass.states.get(DOOR).state == "on"
    for private in ("redact-network", "redact-serial", "redact-token", "redact-nested"):
        assert private not in caplog.text


@pytest.mark.parametrize("background", [False, True], ids=["read_status", "verify_state"])
@pytest.mark.parametrize(
    "pushed",
    [partial(ref_door_ajar=True), partial(frz_door_ajar=True)],
    ids=["disagrees-with-the-reply", "key-the-reply-lacks"],
)
async def test_a_partial_push_during_a_read_stands_and_the_read_still_confirms(
    fridge, freezer, pushed, background
):
    coordinator, client = fridge
    confirmed = coordinator.confirmed_at
    freezer.tick(1)
    read_during(coordinator, client, pushed)
    if background:
        await coordinator.async_verify_state()
        assert coordinator.verification_stats["last_success"] is not None
    else:
        await coordinator.async_read_status()
    assert coordinator.confirmed_at > confirmed
    assert coordinator.last_update_success
    for key, value in pushed.properties.items():
        assert coordinator.data[key] is value


async def test_recovery_read_does_not_bring_back_what_a_replacing_snapshot_dropped(fridge):
    coordinator, client = fridge
    coordinator.async_transport_error(ApiError("Stream disconnected"))
    coordinator.on_channel_opened(recover=False)
    reply = {k: v for k, v in client.state.return_value.items() if k != "ref_door_ajar"}
    # The snapshot replaces state without the door, so the earlier update goes with
    # the rest, and a reply that also lacks the door leaves it unknown.
    read_during(coordinator, client, partial(ref_door_ajar=True), snapshot(INCOMPLETE), reply=reply)
    await coordinator.async_read_status()
    assert coordinator.live_updates_established
    assert "ref_door_ajar" not in coordinator.data


@pytest.mark.parametrize("opens", ["during-the-read", "as-the-read-finishes"])
async def test_recovery_reads_again_when_the_channel_opens_around_its_read(fridge, opens):
    coordinator, client = fridge
    coordinator.async_transport_error(ApiError("Stream disconnected"))
    reading, release = first_read_blocks(client)
    coordinator.start_recovery()
    recovery = coordinator._recovery_task
    await asyncio.wait_for(reading.wait(), 1)
    if opens == "during-the-read":
        coordinator.on_channel_opened()
    else:
        coordinator._read_task.add_done_callback(lambda _: coordinator.on_channel_opened())
    release.set()
    await asyncio.wait_for(recovery, 1)
    assert coordinator.live_updates_established
    assert client.state.await_count == 2


async def test_entities_are_told_when_the_channel_opens_again(fridge):
    coordinator, _ = fridge
    coordinator.async_transport_error(ApiError("Stream disconnected"))
    assert not coordinator.receiving_live_updates
    notified = []
    coordinator.async_add_listener(lambda: notified.append(coordinator.receiving_live_updates))
    coordinator.on_channel_opened(recover=False)
    coordinator.on_channel_opened(recover=False)
    assert notified == [True]


async def test_recovery_runs_after_a_sign_in_failure_that_has_cleared(fridge):
    from custom_components.subzero.auth import InvalidAuth

    coordinator, client = fridge
    client.state.side_effect = InvalidAuth("Sign-in expired")
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    client.state.side_effect = None
    await coordinator.async_read_status()
    assert coordinator.live_updates_established
    reads = client.state.await_count
    coordinator.async_transport_error(ApiError("Stream disconnected"))
    coordinator.on_channel_opened()
    await coordinator._recovery_task
    assert client.state.await_count == reads + 1
    assert coordinator.live_updates_established


async def test_read_without_a_usable_model_does_not_establish_live_updates(fridge):
    coordinator, client = fridge
    coordinator.async_transport_error(ApiError("Stream disconnected"))
    coordinator.on_channel_opened(recover=False)
    read_during(coordinator, client, partial(appliance_model=""))
    with pytest.raises(HomeAssistantError, match="usable state"):
        await coordinator.async_read_status()
    assert not coordinator.live_updates_established


async def test_snapshot_after_a_failed_initial_read_still_needs_a_confirming_read(hass, loaded):
    entry, client, _, _, _ = loaded
    coordinator = SubZeroCoordinator(hass, entry, client, "test-fridge", {"name": "Kitchen"})
    client.state.side_effect = RateLimited(600)
    with pytest.raises(ConfigEntryNotReady):
        await coordinator.async_config_entry_first_refresh()
    assert not coordinator.last_update_success
    coordinator.apply_update(snapshot(INCOMPLETE))
    assert coordinator.last_update_success
    assert not coordinator.live_updates_established


async def test_replacing_snapshot_keeps_the_type_pushed_before_it_over_the_reply(fridge):
    coordinator, client = fridge
    coordinator.async_transport_error(ApiError("Stream disconnected"))
    coordinator.on_channel_opened(recover=False)
    reply = {**client.state.return_value, "appliance_type": "1.2.6.1", "accent_light_level": 1}
    # The snapshot omits the type, so the one pushed before it stays known, and the
    # reply's own type does not bring back a property that type discards.
    read_during(
        coordinator,
        client,
        partial(appliance_type="17.1.1.0"),
        snapshot(INCOMPLETE),
        reply=reply,
    )
    await coordinator.async_read_status()
    assert coordinator.data["appliance_type"] == "17.1.1.0"
    assert "accent_light_level" not in coordinator.data


@pytest.fixture
async def discarding_fridge(fridge):
    """The refrigerator after a read whose type, 17.1.1.0, discards the accent light level."""
    coordinator, client = fridge
    reply = {**client.state.return_value, "appliance_type": "17.1.1.0"}
    reply.pop("accent_light_level", None)
    client.state.return_value = reply
    await coordinator.async_read_status()
    return coordinator, client


@pytest.mark.parametrize(
    ("pushed", "other_model", "kept"),
    [
        (partial(accent_light_level=2), False, False),
        (partial(appliance_type="17.1.1.1", accent_light_level=2), False, True),
        (partial(accent_light_level=2), True, True),
    ],
    ids=["discarded-by-the-published-type", "kept-by-the-pushed-type", "another-model-replies"],
)
async def test_a_pushed_property_is_published_only_when_the_published_type_keeps_it(
    discarding_fridge, pushed, other_model, kept
):
    coordinator, client = discarding_fridge
    reply = dict(client.state.return_value)
    if other_model:
        # The reply's own type, not the reference's, decides what it discards.
        del reply["appliance_type"]
        reply.update(appliance_model="OTHER-FRIDGE", accent_light_level=1)
    read_during(coordinator, client, pushed, reply=reply)
    await coordinator.async_read_status()
    if kept:
        assert coordinator.data["accent_light_level"] == 2
    else:
        assert "accent_light_level" not in coordinator.data


# Snapshots during a read


@pytest.mark.parametrize("complete", [False, True])
async def test_failed_read_with_concurrent_snapshot_fails_without_marking_unavailable(
    hass, freezer, loaded, fridge, complete
):
    _, _, updates, _, _ = loaded
    coordinator, client = fridge
    await updates.put(partial(ref_door_ajar=True))
    await hass.async_block_till_done()
    freezer.tick(1)
    pushed = (
        snapshot(client.state.return_value)
        if complete
        else snapshot(appliance_model=MODEL, ref_set_temp=39)
    )
    read_during(coordinator, client, pushed, error=ApiError("Status request failed"))
    with pytest.raises(HomeAssistantError, match="Status request failed"):
        await coordinator.async_read_status()
    # The read still fails for its caller, while the snapshot's state and
    # availability remain intact.
    assert coordinator.confirmed_at == dt_util.utcnow()
    assert coordinator.last_update_success
    if complete:
        assert hass.states.get(DOOR).state == "off"
    else:
        assert hass.states.get(DOOR).state == "on"
        assert hass.states.get(SETPOINT).state == "39"


@pytest.mark.parametrize("after", ["nothing", "snapshot", "channel-error"])
async def test_reply_spanning_a_connection_change_is_not_published(hass, freezer, fridge, after):
    coordinator, client = fridge
    reading, release = blocked_read(client, reply={**client.state.return_value, "ref_set_temp": 41})
    request = asyncio.create_task(coordinator.async_read_status())
    await asyncio.wait_for(reading.wait(), 1)
    freezer.tick(1)
    coordinator.async_transport_error(ApiError("Stream disconnected"))
    if after != "nothing":
        coordinator.apply_update(snapshot(INCOMPLETE))
    if after == "channel-error":
        coordinator.on_channel_error(ApiError("Channel error"))
    release.set()
    with pytest.raises(HomeAssistantError, match="push connection changed"):
        await request
    # The reply may be older than what arrived on the new connection, and it cannot
    # undo an error that followed it.
    assert coordinator.data["ref_set_temp"] == 38
    assert ("ref_door_ajar" in coordinator.data) is (after == "nothing")
    assert coordinator.last_update_success is (after != "channel-error")
    assert not coordinator.live_updates_established


async def test_reply_straddling_a_reconnect_still_delivers_the_events_in_its_history(fridge):
    coordinator, client = fridge
    client.state.return_value["notifs"] = []
    await coordinator.async_read_status()
    observed = []
    coordinator.async_add_event_listener(observed.append)
    client.state.return_value = {**client.state.return_value, "notifs": [alert(1)]}
    reading, release = first_read_blocks(client, then=RateLimited(600))
    read = asyncio.create_task(coordinator._async_read_result(report_error=False))
    await asyncio.wait_for(reading.wait(), 1)
    coordinator.async_transport_error(ApiError("Stream disconnected"))
    coordinator.apply_update(snapshot(INCOMPLETE))
    release.set()
    await read
    assert [event["sequence"] for event in observed] == [1]


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
        alert(1, started - timedelta(seconds=1)),
        alert(2),
    ]
    for _ in range(2):
        if history_source == "snapshot":
            await updates.put(StateUpdate(dict(client.state.return_value), full=True))
        else:
            await coordinator.async_read_status()
        await hass.async_block_till_done()
    assert [(event["sequence"], event["source"]) for event in observed] == [(2, "history")]


# Incomplete observations


async def test_incomplete_status_reply_confirms_and_drops_the_keys_it_lacks(hass, fridge, freezer):
    coordinator, client = fridge
    confirmed = coordinator.confirmed_at
    client.state.return_value = {"appliance_model": MODEL, "ref_set_temp": 39}
    freezer.tick(1)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert coordinator.confirmed_at > confirmed
    # A read replaces state, so the door is unknown rather than stale.
    assert "ref_door_ajar" not in coordinator.data
    assert hass.states.get(DOOR).state == "unavailable"
    assert hass.states.get(SETPOINT).state == "39"


async def test_incomplete_snapshot_restores_availability_after_the_grace_window(
    hass, freezer, loaded, fridge
):
    entry, _, updates, _, _ = loaded
    coordinator, client = fridge
    # Outage status checks would keep the state from expiring.
    hass.config_entries.async_update_entry(entry, pref_disable_polling=True)
    complete = dict(client.state.return_value)
    client.state.return_value = {
        key: value for key, value in complete.items() if key != "ref_door_ajar"
    }
    client.push_connected = False
    await updates.put(RateLimited(300))
    await hass.async_block_till_done()
    freezer.tick(121)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert not coordinator.last_update_success

    # The snapshot restores what it carries. The door it lacks is unknown, not stale,
    # and the status read that confirms state replaces it with what reads report.
    coordinator.apply_update(snapshot(appliance_model=complete["appliance_model"], ref_set_temp=39))
    await hass.async_block_till_done()
    assert coordinator.last_update_success
    assert not coordinator.live_updates_established
    assert hass.states.get(DOOR).state == "unavailable"
    assert hass.states.get(SETPOINT).state == "39"
    freezer.tick(1)
    await read_confirms(coordinator)
    assert coordinator.last_update_success
    assert hass.states.get(DOOR).state == "unavailable"
    assert hass.states.get(SETPOINT).state == "38"

    freezer.tick(1)
    client.state.return_value = complete
    await read_confirms(coordinator)
    assert coordinator.last_update_success
    assert hass.states.get(DOOR).state == "off"


# Shared reads


async def test_update_entity_reads_status_on_every_call(hass, fridge):
    _, client = fridge
    assert await async_setup_component(hass, "homeassistant", {})
    client.state.reset_mock()
    for opened in (True, False):
        client.state.return_value["ref_door_ajar"] = opened
        await hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": DOOR}, blocking=True
        )
        assert hass.states.get(DOOR).state == ("on" if opened else "off")
    assert client.state.await_count == 2


async def test_calls_during_a_read_share_a_newer_followup(hass, fridge):
    coordinator, client = fridge
    reading, release = first_read_blocks(client)
    first = asyncio.create_task(coordinator.async_read_status())
    await asyncio.wait_for(reading.wait(), 1)
    client.state.return_value["ref_door_ajar"] = True
    later = [asyncio.create_task(coordinator.async_read_status()) for _ in range(5)]
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, *later)
    assert client.state.await_count == 2
    assert hass.states.get(DOOR).state == "on"


async def test_manual_request_during_background_read_reports_its_own_failure(hass, fridge):
    coordinator, client = fridge
    client.push_connected = True
    coordinator.start_recovery = Mock()
    reading, release = first_read_blocks(client, then=ApiError("Manual read failed"))
    background = asyncio.create_task(coordinator.async_verify_state())
    await asyncio.wait_for(reading.wait(), 1)
    manual = asyncio.create_task(coordinator.async_read_status())
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(HomeAssistantError, match="Manual read failed"):
        await manual
    await background
    await hass.async_block_till_done()
    assert client.state.await_count == 2
    assert not coordinator.last_update_success
    assert "Manual read failed" in str(coordinator.last_exception)
    assert hass.states.get(DOOR).state == "unavailable"


async def test_cancelled_queued_manual_read_does_not_change_later_background_failure_policy(
    hass, fridge
):
    coordinator, client = fridge
    client.push_connected = True
    coordinator.start_recovery = Mock()
    reading, release = first_read_blocks(client, then=ApiError("Background read failed"))
    background = asyncio.create_task(coordinator.async_verify_state())
    await asyncio.wait_for(reading.wait(), 1)
    manual = asyncio.create_task(coordinator.async_read_status())
    await asyncio.sleep(0)
    manual.cancel()
    with pytest.raises(asyncio.CancelledError):
        await manual
    assert not coordinator._read_task.cancelled()
    release.set()
    await background
    confirmed = coordinator.confirmed_at
    published = Mock()
    coordinator.async_add_listener(published)

    await coordinator.async_verify_state()
    await hass.async_block_till_done()

    assert client.state.await_count == 2
    assert coordinator.last_update_success
    assert coordinator.confirmed_at == confirmed
    assert hass.states.get(DOOR).state == "off"
    published.assert_not_called()


async def test_cancelled_caller_does_not_cancel_the_shared_read(hass, fridge):
    coordinator, client = fridge
    reading, release = blocked_read(
        client, reply={**client.state.return_value, "ref_door_ajar": True}
    )
    caller = asyncio.create_task(coordinator.async_read_status())
    await asyncio.wait_for(reading.wait(), 1)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert not coordinator._read_task.cancelled()
    release.set()
    await coordinator._read_task
    assert hass.states.get(DOOR).state == "on"


# The read_status action and the confirmation sensor


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
    assert hass.states.get(DOOR).state == "on"


async def test_read_status_action_rejects_an_unloaded_device(hass):
    from custom_components.subzero.services import async_setup_services

    async_setup_services(hass)
    with pytest.raises(ServiceValidationError, match="loaded Sub-Zero"):
        await hass.services.async_call(
            "subzero", "read_status", {"device_id": "missing"}, blocking=True
        )


@pytest.mark.parametrize("failure", ["error", "unusable"])
async def test_explicit_confirmation_fails_instead_of_trusting_cached_state(fridge, failure):
    coordinator, client = fridge
    confirmed = coordinator.confirmed_at
    if failure == "error":
        client.state.side_effect = ApiError("Could not read status")
    else:
        client.state.return_value = {"appliance_model": ""}
    with pytest.raises(
        HomeAssistantError, match="Could not read status" if failure == "error" else "usable state"
    ):
        await coordinator.async_read_status()
    assert coordinator.confirmed_at == confirmed


async def test_confirmation_sensor_keeps_the_last_observation_when_offline(
    hass, loaded, enabled_confirmation_sensor
):
    entry, _, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    timestamp = hass.states.get(enabled_confirmation_sensor).state
    assert timestamp == coordinator.confirmed_at.replace(microsecond=0).isoformat()
    coordinator.async_set_update_error(ApiError("Offline"))
    assert hass.states.get(enabled_confirmation_sensor).state == timestamp


# Scheduled checks and the push transport


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
    client.state.assert_awaited_once()
    assert hass.states.get(DOOR).state == "on"


async def test_token_updates_do_not_postpone_state_checks(hass, freezer, loaded):
    entry, client, _, _, _ = loaded
    account = entry.runtime_data
    client.state.reset_mock()
    freezer.tick(200)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "tokens": {**entry.data["tokens"], "refresh_token": "refreshed-token"},
        },
    )
    account.async_update_state_checks()
    await hass.async_block_till_done()
    freezer.tick(101)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    client.state.assert_awaited_once_with("test-fridge")
    assert entry.runtime_data is account


async def test_rate_limited_checks_leave_confirmation_age_unchanged(
    hass, loaded, freezer, enabled_confirmation_sensor
):
    entry, client, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    confirmed = coordinator.confirmed_at
    client.state.side_effect = RateLimited(600)
    started = dt_util.utcnow()
    for seconds in (301, 602, 903):
        freezer.move_to(started + timedelta(seconds=seconds))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        await entry.runtime_data._verifications["test-fridge"]
        await updates.put(partial(ref_set_temp=39))
        await hass.async_block_till_done()
    assert coordinator.confirmed_at == confirmed
    assert (
        hass.states.get(enabled_confirmation_sensor).state
        == confirmed.replace(microsecond=0).isoformat()
    )
    with pytest.raises(HomeAssistantError):
        await coordinator.async_read_status()


@pytest.mark.parametrize(
    "newer", ["failed_read", "rate_limited", "snapshot", "incomplete_snapshot"]
)
async def test_an_older_refresh_cannot_overwrite_a_newer_read_or_snapshot(
    hass, loaded, fridge, newer
):
    entry, _, _, _, _ = loaded
    coordinator, client = fridge
    reading, release, recovery_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    client.state.reset_mock()
    confirmed = coordinator.confirmed_at

    async def read(_device_id):
        if client.state.await_count == 1:
            reading.set()
            await release.wait()
            if newer in {"snapshot", "incomplete_snapshot"}:
                raise ApiError("The older read failed")
            return dict(client.state.return_value)
        if client.state.await_count == 2:
            if newer == "failed_read":
                raise ApiError("A newer read failed")
            if newer == "rate_limited":
                raise RateLimited(60)
        await recovery_release.wait()
        return dict(client.state.return_value)

    client.state.side_effect = read
    refresh = asyncio.create_task(coordinator.async_refresh())
    await asyncio.wait_for(reading.wait(), 1)
    subsequent = []

    async def failed_read():
        with pytest.raises(HomeAssistantError):
            await coordinator.async_read_status()

    def after_read(_task):
        if newer in {"snapshot", "incomplete_snapshot"}:
            properties = dict(client.state.return_value)
            if newer == "incomplete_snapshot":
                del properties["ref_door_ajar"]
            coordinator.apply_update(snapshot(properties))
        else:
            subsequent.append(
                entry.async_create_background_task(hass, failed_read(), "Newer status read")
            )

    coordinator._read_task.add_done_callback(after_read)
    release.set()
    await refresh
    if subsequent:
        await subsequent[0]
    assert coordinator.last_update_success is (newer != "failed_read")
    if newer in {"snapshot", "incomplete_snapshot"}:
        assert coordinator.confirmed_at > confirmed
    if newer == "rate_limited":
        client.state.side_effect = RateLimited(60)
        with pytest.raises(HomeAssistantError):
            await coordinator.async_read_status()


async def test_auth_failure_is_recovered_by_the_read_a_snapshot_prompts(
    hass, freezer, loaded, fridge
):
    from custom_components.subzero.auth import InvalidAuth

    entry, _, updates, _, _ = loaded
    coordinator, client = fridge
    client.state.reset_mock()
    client.state.side_effect = InvalidAuth("Sign-in expired")
    freezer.tick(301)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    await entry.runtime_data._verifications["test-fridge"]
    assert not coordinator.last_update_success
    # A snapshot does not show that sign-in works. It prompts one read, which fails.
    await updates.put(snapshot(client.state.return_value))
    await hass.async_block_till_done()
    assert not coordinator.last_update_success
    assert client.state.await_count == 2

    client.state.side_effect = None
    await updates.put(snapshot(client.state.return_value))
    await hass.async_block_till_done()
    assert coordinator.last_update_success
    assert coordinator.live_updates_established
    assert client.state.await_count == 3
    freezer.tick(301)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    await entry.runtime_data._verifications["test-fridge"]
    assert client.state.await_count == 4
    assert coordinator.verification_stats["checks"] == 2
    assert coordinator.verification_stats["status_reads"] == 2


async def test_an_unexpected_event_listener_failure_does_not_wedge_future_reads(fridge):
    coordinator, client = fridge
    observed = []
    client.state.return_value["notifs"] = []
    await coordinator.async_read_status()

    def listener(event):
        observed.append(event)
        raise RuntimeError("Event listener failed")

    coordinator.async_add_event_listener(listener)
    client.state.reset_mock()
    client.state.return_value["notifs"] = [
        {"notif_seq": 1, "notif_type": 101, "timestamp": dt_util.utcnow().isoformat()}
    ]
    with pytest.raises(RuntimeError, match="Event listener failed"):
        await coordinator.async_read_status()
    await coordinator.async_read_status()
    assert client.state.await_count == 2
    assert len(observed) == 1


# Follow-up reads after a door alert


def alert(sequence: int, when=None) -> dict:
    return {
        "notif_seq": sequence,
        "notif_type": 101,
        "timestamp": (when or dt_util.utcnow()).isoformat(),
    }


async def _advance_followup(hass, freezer, coordinator, seconds):
    freezer.tick(seconds)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    if coordinator._read_task is not None:
        await coordinator._read_task
    await hass.async_block_till_done()


async def test_disabling_polling_cancels_a_followup_with_periodic_checks_already_off(
    hass, freezer, loaded, fridge
):
    entry, _, _, _, _ = loaded
    account = entry.runtime_data
    coordinator, client = fridge
    client.state.reset_mock()
    hass.config_entries.async_update_entry(entry, options={"status_poll_interval": 0})
    account.async_update_state_checks()
    coordinator.async_receive_event(dt_util.utcnow(), 0, 101)
    followup = coordinator._followup_task
    hass.config_entries.async_update_entry(entry, pref_disable_polling=True)
    account.async_update_state_checks()
    await hass.async_block_till_done()
    assert followup.cancelled()
    assert coordinator._followup_task is None
    assert not coordinator._followup_alert
    await _advance_followup(hass, freezer, coordinator, 601)
    client.state.assert_not_awaited()


async def test_disabling_polling_cancels_followups_but_finishes_the_status_read(
    hass, freezer, loaded, fridge
):
    entry, _, updates, _, _ = loaded
    account = entry.runtime_data
    coordinator, client = fridge
    client.state.reset_mock()
    client.state.return_value["ref_door_ajar"] = True
    await updates.put(partial(ref_door_ajar=True))
    await hass.async_block_till_done()
    reading, release = blocked_read(client)
    coordinator.async_receive_event(dt_util.utcnow(), 0, 101)
    followup = coordinator._followup_task
    freezer.tick(3)
    async_fire_time_changed(hass, dt_util.utcnow())
    await asyncio.wait_for(reading.wait(), 1)
    status_read = coordinator._read_task
    hass.config_entries.async_update_entry(entry, pref_disable_polling=True)
    account.async_update_state_checks()
    await hass.async_block_till_done()
    assert followup.cancelled()
    assert coordinator._followup_task is None
    assert not coordinator._followup_alert
    assert not status_read.done()
    assert hass.states.get(DOOR).state == "on"
    assert client.watched == [["test-fridge"]]

    client.state.return_value["ref_door_ajar"] = False
    release.set()
    await status_read
    await _advance_followup(hass, freezer, coordinator, 301)
    client.state.assert_awaited_once_with("test-fridge")
    assert hass.states.get(DOOR).state == "off"

    hass.config_entries.async_update_entry(
        entry, pref_disable_polling=False, options={"status_poll_interval": 60}
    )
    account.async_update_state_checks()
    await _advance_followup(hass, freezer, coordinator, 61)
    assert client.state.await_count == 2
    assert entry.runtime_data is account


@pytest.mark.parametrize("interval", [0, 300])
async def test_followups_repair_a_missed_door_close_and_stop(
    hass, freezer, loaded, fridge, interval
):
    entry, _, updates, _, _ = loaded
    coordinator, client = fridge
    hass.config_entries.async_update_entry(entry, options={"status_poll_interval": interval})
    client.state.reset_mock()
    client.state.return_value["ref_door_ajar"] = True
    await updates.put(partial(ref_door_ajar=True))
    await hass.async_block_till_done()
    coordinator.async_receive_event(dt_util.utcnow(), 0, 101)
    await _advance_followup(hass, freezer, coordinator, 3)
    assert client.state.await_count == 1
    assert hass.states.get(DOOR).state == "on"
    client.state.return_value["ref_door_ajar"] = False
    await _advance_followup(hass, freezer, coordinator, 61)
    assert client.state.await_count == 2
    assert hass.states.get(DOOR).state == "off"
    await _advance_followup(hass, freezer, coordinator, 121)
    assert client.state.await_count == 2
    assert coordinator._followup_task is None


async def test_alert_bursts_and_unrelated_updates_do_not_extend_followups(
    hass, freezer, loaded, fridge
):
    _, _, updates, _, _ = loaded
    coordinator, client = fridge
    client.state.return_value["ref_door_ajar"] = True
    client.state.reset_mock()
    for sequence in range(5):
        await updates.put(partial(ref_door_ajar=True, notifs=[alert(sequence)]))
    await hass.async_block_till_done()
    for seconds in (3, 61, 121, 100):
        await _advance_followup(hass, freezer, coordinator, seconds)
        await updates.put(partial(ref_set_temp=39))
        await hass.async_block_till_done()
    assert client.state.await_count == 3
    assert coordinator._followup_task is None


async def test_a_pushed_close_cancels_pending_followups(hass, freezer, loaded, fridge):
    _, _, updates, _, _ = loaded
    coordinator, client = fridge
    client.state.reset_mock()
    client.state.return_value["ref_door_ajar"] = True
    await updates.put(partial(ref_door_ajar=True))
    await hass.async_block_till_done()
    coordinator.async_receive_event(dt_util.utcnow(), 0, 101)
    await _advance_followup(hass, freezer, coordinator, 3)
    client.state.assert_awaited_once()
    client.state.reset_mock()
    await updates.put(partial(ref_door_ajar=False))
    await hass.async_block_till_done()
    await _advance_followup(hass, freezer, coordinator, 61)
    client.state.assert_not_awaited()
    assert coordinator._followup_task is None


async def test_event_listeners_see_state_from_the_same_message(hass, loaded, fridge):
    _, _, updates, _, _ = loaded
    coordinator, _ = fridge
    observed = []
    coordinator.async_add_event_listener(lambda _: observed.append(hass.states.get(DOOR).state))
    await updates.put(partial(ref_door_ajar=True, notifs=[alert(1)]))
    await hass.async_block_till_done()
    assert observed == ["on"]


async def test_rate_limits_stop_event_followups_without_claiming_new_state(
    hass, freezer, loaded, fridge
):
    _, _, updates, _, _ = loaded
    coordinator, client = fridge
    confirmed = coordinator.confirmed_at
    client.state.reset_mock()
    client.state.side_effect = RateLimited(300)
    await updates.put(partial(ref_door_ajar=True))
    await hass.async_block_till_done()
    coordinator.async_receive_event(dt_util.utcnow(), 0, 101)
    await _advance_followup(hass, freezer, coordinator, 3)
    await _advance_followup(hass, freezer, coordinator, 181)
    client.state.assert_awaited_once()
    assert coordinator.confirmed_at == confirmed
    assert coordinator.last_update_success
    assert coordinator._followup_task is None


@pytest.mark.parametrize("rate_limited", [False, True])
async def test_failed_followup_does_not_keep_an_old_alert_pending(
    hass, freezer, loaded, fridge, rate_limited
):
    _, _, updates, _, _ = loaded
    coordinator, client = fridge
    reading, release = blocked_read(
        client, error=RateLimited(60) if rate_limited else ApiError("Status read failed")
    )
    client.state.return_value["ref_door_ajar"] = True
    await updates.put(partial(ref_door_ajar=True))
    await hass.async_block_till_done()
    coordinator.async_receive_event(dt_util.utcnow(), 0, 101)
    freezer.tick(3)
    async_fire_time_changed(hass, dt_util.utcnow())
    await asyncio.wait_for(reading.wait(), 1)
    await updates.put(partial(notifs=[alert(1)]))
    await hass.async_block_till_done()
    release.set()
    await coordinator._read_task
    client.state.side_effect = None
    client.state.return_value["ref_door_ajar"] = False
    await updates.put(snapshot(client.state.return_value))
    await hass.async_block_till_done()
    if task := coordinator._followup_task:
        assert task.cancelling()
        await asyncio.gather(task, return_exceptions=True)
    assert coordinator._followup_task is None
    client.state.reset_mock()
    for opened in (True, False):
        await updates.put(partial(ref_door_ajar=opened))
        await hass.async_block_till_done()
    await _advance_followup(hass, freezer, coordinator, 61)
    client.state.assert_not_awaited()
    assert coordinator._followup_task is None


@pytest.mark.parametrize("failure", ["transient", "rate_limited", "unavailable"])
@pytest.mark.parametrize("new_alert", [False, True])
async def test_alert_during_failed_final_followup_starts_a_new_window(
    hass, freezer, loaded, fridge, failure, new_alert
):
    _, _, updates, _, _ = loaded
    coordinator, client = fridge
    client.state.reset_mock()
    client.state.return_value["ref_door_ajar"] = True
    await updates.put(partial(ref_door_ajar=True))
    await hass.async_block_till_done()
    coordinator.async_receive_event(dt_util.utcnow(), 0, 101)
    await _advance_followup(hass, freezer, coordinator, 3)
    await _advance_followup(hass, freezer, coordinator, 61)
    assert client.state.await_count == 2
    reading, release = asyncio.Event(), asyncio.Event()

    async def read(_device_id):
        reading.set()
        await release.wait()
        if failure == "unavailable":
            client.push_connected = False
            coordinator.async_set_update_error(ApiError("Disconnected"))
        raise RateLimited(300) if failure == "rate_limited" else ApiError("Status read failed")

    client.state.side_effect = read
    freezer.tick(121)
    async_fire_time_changed(hass, dt_util.utcnow())
    await asyncio.wait_for(reading.wait(), 1)
    if new_alert:
        coordinator.async_receive_event(dt_util.utcnow(), 1, 101)
    release.set()
    await coordinator._read_task
    await hass.async_block_till_done()
    restart = new_alert and failure == "transient"
    assert (coordinator._followup_task is not None) is restart

    client.state.side_effect = None
    client.state.return_value["ref_door_ajar"] = False
    await _advance_followup(hass, freezer, coordinator, 3)
    assert client.state.await_count == 3
    await _advance_followup(hass, freezer, coordinator, 58)
    assert client.state.await_count == 3 + restart
    assert coordinator._followup_task is None
    if restart:
        assert hass.states.get(DOOR).state == "off"


async def test_disabled_polling_also_disables_automatic_event_followups(
    hass, freezer, loaded, fridge
):
    entry, _, updates, _, _ = loaded
    coordinator, client = fridge
    hass.config_entries.async_update_entry(entry, pref_disable_polling=True)
    await hass.async_block_till_done()
    client.state.reset_mock()
    await updates.put(partial(ref_door_ajar=True))
    await hass.async_block_till_done()
    coordinator.async_receive_event(dt_util.utcnow(), 0, 101)
    await _advance_followup(hass, freezer, coordinator, 10)
    client.state.assert_not_awaited()
    await coordinator.async_read_status()
    client.state.assert_awaited_once()


async def test_unloading_cancels_event_followup_timers(hass, freezer, loaded, fridge):
    entry, _, updates, _, _ = loaded
    coordinator, client = fridge
    await updates.put(partial(ref_door_ajar=True))
    await hass.async_block_till_done()
    coordinator.async_receive_event(dt_util.utcnow(), 0, 101)
    task = coordinator._followup_task
    assert task is not None
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    client.state.reset_mock()
    await _advance_followup(hass, freezer, coordinator, 185)
    client.state.assert_not_awaited()
    assert task.cancelled()
