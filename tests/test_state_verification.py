"""Completed observations, freshness and independent state recovery."""

import asyncio
import logging
from datetime import timedelta
from unittest.mock import Mock

import pytest
from homeassistant.config_entries import RELOAD_AFTER_UPDATE_DELAY
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.subzero.api import ApiError, RateLimited, StateUpdate
from custom_components.subzero.coordinator import SubZeroCoordinator
from tests.test_integration import loaded as loaded

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture
async def enabled_confirmation_sensor(hass, loaded, freezer):
    entity_id = "sensor.kitchen_state_confirmed"
    er.async_get(hass).async_update_entity(entity_id, disabled_by=None)
    await hass.async_block_till_done()
    freezer.tick(RELOAD_AFTER_UPDATE_DELAY + 1)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    return entity_id


@pytest.fixture
def dated_snapshots(loaded):
    _, client, _, _, _ = loaded
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


async def test_old_full_push_does_not_publish_confirm_or_clear_an_error(
    hass, freezer, loaded, dated_snapshots
):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    fresh, older = dated_snapshots
    client.state.return_value = fresh
    await coordinator.async_read_status()
    confirmed = coordinator.confirmed_at
    coordinator.async_receive_event(dt_util.utcnow(), 0, 101)
    followup = coordinator._followup_task
    assert followup is not None
    error = ApiError("Appliance unavailable")
    coordinator.async_set_update_error(error)
    published = Mock()
    coordinator.async_add_listener(published)
    freezer.tick(1)

    coordinator.apply_update(StateUpdate(older, full=True))
    await hass.async_block_till_done()

    assert coordinator.data["ref_door_ajar"] is True
    assert coordinator.confirmed_at == confirmed
    assert not coordinator.last_update_success
    assert coordinator.last_exception is error
    assert coordinator._followup_task is followup
    assert not followup.cancelling()
    published.assert_not_called()


async def test_queued_old_full_push_does_not_cancel_account_recovery(hass, loaded, dated_snapshots):
    entry, client, updates, _, _ = loaded
    account = entry.runtime_data
    coordinator = account.coordinators["test-fridge"]
    fresh, older = dated_snapshots
    client.state.return_value = fresh
    await coordinator.async_read_status()
    reading, release = asyncio.Event(), asyncio.Event()

    async def read(_device_id):
        reading.set()
        await release.wait()
        return fresh

    client.state.side_effect = read
    coordinator.async_set_update_error(ApiError("Status request failed"))
    account._start_recovery(coordinator)
    await asyncio.wait_for(reading.wait(), 1)
    recovery = account._recoveries["test-fridge"]
    try:
        await updates.put(StateUpdate(older, full=True))
        await hass.async_block_till_done()
        assert account._recoveries["test-fridge"] is recovery
        assert not recovery.done()
        assert not recovery.cancelling()
        assert not coordinator.last_update_success
        assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "unavailable"
    finally:
        release.set()

    await recovery
    await hass.async_block_till_done()
    assert coordinator.last_update_success
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"


async def test_initial_read_discards_old_full_push_but_keeps_later_partial(
    hass, loaded, dated_snapshots
):
    entry, client, _, _, _ = loaded
    fresh, older = dated_snapshots
    coordinator = SubZeroCoordinator(
        hass, entry, client, "test-fridge", {"name": "Kitchen"}, Mock()
    )

    async def read(_device_id):
        coordinator.apply_update(StateUpdate(older, full=True))
        coordinator.apply_update(StateUpdate({"ref_set_temp": 39}, full=False))
        return fresh

    client.state.side_effect = read
    with pytest.raises(HomeAssistantError, match="state changed during"):
        await coordinator.async_read_status()
    assert coordinator.data["ref_door_ajar"] is True
    assert coordinator.data["time"] == fresh["time"]
    assert coordinator.data["uptime"] == fresh["uptime"]
    assert coordinator.data["ref_set_temp"] == 39


async def test_failed_read_cannot_be_rescued_by_an_old_full_push(loaded, freezer, dated_snapshots):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    fresh, older = dated_snapshots
    client.state.return_value = fresh
    await coordinator.async_read_status()
    confirmed = coordinator.confirmed_at
    freezer.tick(1)

    async def read(_device_id):
        coordinator.apply_update(StateUpdate(older, full=True))
        raise ApiError("Status request failed")

    client.state.side_effect = read
    with pytest.raises(HomeAssistantError, match="Status request failed"):
        await coordinator.async_read_status()
    assert coordinator.confirmed_at == confirmed
    assert coordinator.data["ref_door_ajar"] is True
    assert not coordinator.last_update_success


async def test_old_incomplete_snapshot_during_read_is_ignored(loaded, freezer, dated_snapshots):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    fresh, older = dated_snapshots
    del older["ref_set_temp"]

    async def read(_device_id):
        coordinator.apply_update(StateUpdate(older, full=True))
        return fresh

    client.state.side_effect = read
    assert await coordinator.async_read_status() == dt_util.utcnow()
    assert coordinator.data["ref_door_ajar"] is True
    assert coordinator.data["time"] == fresh["time"]
    assert coordinator.data["uptime"] == fresh["uptime"]


async def test_old_full_push_does_not_satisfy_verification(loaded, dated_snapshots):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    fresh, older = dated_snapshots
    client.state.return_value = fresh
    await coordinator.async_read_status()
    client.state.reset_mock()
    client.push_connected = True

    async def read(_device_id):
        coordinator.apply_update(StateUpdate(older, full=True))
        return fresh

    client.state.side_effect = read
    await coordinator.async_verify_state()
    client.state.assert_awaited_once_with("test-fridge")
    client.open_channel.assert_not_awaited()
    assert coordinator.data["ref_door_ajar"] is True
    assert coordinator.verification_stats["complete_snapshots"] == 0
    assert coordinator.verification_stats["status_reads"] == 1
    assert coordinator.verification_stats["last_success"] is not None


@pytest.mark.parametrize(
    ("appliance_time", "uptime"),
    [
        ("2025-01-01T11:00:00+00:00", "14:00:00"),
        ("2025-01-01T13:00:00+00:00", "0:01:00"),
    ],
    ids=["clock-correction", "reboot"],
)
async def test_full_push_accepts_clock_correction_or_reboot(
    loaded, freezer, dated_snapshots, appliance_time, uptime
):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    fresh, older = dated_snapshots
    client.state.return_value = fresh
    await coordinator.async_read_status()
    freezer.tick(1)
    coordinator.apply_update(
        StateUpdate({**older, "time": appliance_time, "uptime": uptime}, full=True)
    )
    assert coordinator.data["ref_door_ajar"] is False
    assert coordinator.confirmed_at == dt_util.utcnow()


async def test_direct_read_rebases_snapshot_age(loaded, dated_snapshots):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    fresh, older = dated_snapshots
    client.state.return_value = fresh
    await coordinator.async_read_status()
    client.state.return_value = older
    await coordinator.async_read_status()
    assert coordinator.data["time"] == older["time"]

    coordinator.apply_update(
        StateUpdate(
            {
                **fresh,
                "time": "2025-01-01T11:30:00+00:00",
                "uptime": "12:30:00",
            },
            full=True,
        )
    )
    assert coordinator.data["ref_door_ajar"] is True
    assert coordinator.data["uptime"] == "12:30:00"


@pytest.mark.parametrize(("uptime", "accepted"), [("12:59:01", True), ("12:59:00", False)])
async def test_old_full_push_allows_uptime_truncation_tolerance(
    loaded, dated_snapshots, uptime, accepted
):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    fresh, older = dated_snapshots
    client.state.return_value = fresh
    await coordinator.async_read_status()
    coordinator.apply_update(StateUpdate({**older, "uptime": uptime}, full=True))
    assert coordinator.data["ref_door_ajar"] is (not accepted)


@pytest.mark.parametrize(
    ("field", "value"),
    [("time", None), ("time", "invalid"), ("uptime", None), ("uptime", "invalid")],
)
async def test_full_push_without_usable_age_metadata_keeps_existing_behavior(
    loaded, dated_snapshots, field, value
):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    fresh, older = dated_snapshots
    client.state.return_value = fresh
    await coordinator.async_read_status()
    if value is None:
        del older[field]
    else:
        older[field] = value
    coordinator.apply_update(StateUpdate(older, full=True))
    assert coordinator.data["ref_door_ajar"] is False
    assert coordinator.last_update_success


async def test_partial_push_does_not_confirm_unchanged_properties(hass, freezer, loaded):
    entry, _, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    confirmed = coordinator.confirmed_at
    assert confirmed is not None
    freezer.tick(1)
    await updates.put(StateUpdate({"ref_set_temp": 39}, full=False))
    await hass.async_block_till_done()
    assert coordinator.confirmed_at == confirmed


async def test_debug_logs_distinguish_observed_and_published_state_without_private_values(
    hass, loaded, caplog
):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    caplog.set_level(logging.DEBUG, logger="custom_components.subzero.coordinator")
    caplog.clear()

    async def read(_device_id):
        coordinator.apply_update(StateUpdate({"ref_door_ajar": True}, full=False))
        return {
            **client.state.return_value,
            "ap_ssid": "redact-network",
            "appliance_serial": "redact-serial",
            "unknown_token": "redact-token",
            "version": {"secret": "redact-nested"},
        }

    client.state.side_effect = read
    with pytest.raises(HomeAssistantError, match="state changed during"):
        await coordinator.async_read_status()
    replies = [record.message for record in caplog.records if "status response" in record.message]
    publications = [
        record.message for record in caplog.records if "state published" in record.message
    ]
    assert len(replies) == 1
    assert "'ref_door_ajar': False" in replies[0]
    assert "'ref_door_ajar': True" in publications[-1]
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"
    for private in ("redact-network", "redact-serial", "redact-token", "redact-nested"):
        assert private not in caplog.text


@pytest.mark.parametrize("opened", [False, True])
@pytest.mark.parametrize("full", [False, True], ids=["partial", "incomplete-snapshot"])
async def test_conflicting_incomplete_push_is_immediate_but_read_cannot_confirm_it(
    hass, freezer, loaded, opened, full
):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    confirmed = coordinator.confirmed_at
    reading, release = asyncio.Event(), asyncio.Event()
    client.state.return_value["ref_door_ajar"] = not opened
    freezer.tick(1)

    async def read(_device_id):
        snapshot = dict(client.state.return_value)
        properties = {"ref_door_ajar": opened}
        if full:
            properties["appliance_model"] = snapshot["appliance_model"]
        coordinator.apply_update(StateUpdate(properties, full=full))
        reading.set()
        await release.wait()
        return snapshot

    client.state.side_effect = read
    request = asyncio.create_task(coordinator.async_read_status())
    await asyncio.wait_for(reading.wait(), 1)
    assert not request.done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == (
        "on" if opened else "off"
    )
    assert coordinator.confirmed_at == confirmed
    release.set()
    with pytest.raises(HomeAssistantError, match="state changed during"):
        await request
    assert coordinator.confirmed_at == confirmed
    assert coordinator.last_update_success

    client.state.side_effect = None
    client.state.return_value["ref_door_ajar"] = opened
    assert await coordinator.async_read_status() == dt_util.utcnow()
    assert coordinator.confirmed_at > confirmed


@pytest.mark.parametrize("full", [False, True], ids=["partial", "incomplete-snapshot"])
async def test_matching_incomplete_push_during_read_can_confirm_state(loaded, freezer, full):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    confirmed = coordinator.confirmed_at
    freezer.tick(1)

    async def read(_device_id):
        properties = {"ref_door_ajar": False}
        if full:
            properties["appliance_model"] = client.state.return_value["appliance_model"]
        coordinator.apply_update(StateUpdate(properties, full=full))
        return dict(client.state.return_value)

    client.state.side_effect = read
    assert await coordinator.async_read_status() == dt_util.utcnow()
    assert coordinator.confirmed_at > confirmed


@pytest.mark.parametrize("full", [False, True], ids=["partial", "incomplete-snapshot"])
async def test_conflicting_incomplete_push_does_not_claim_background_verification(
    loaded, freezer, full
):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    confirmed = coordinator.confirmed_at
    freezer.tick(1)

    async def read(_device_id):
        properties = {"ref_door_ajar": True}
        if full:
            properties["appliance_model"] = client.state.return_value["appliance_model"]
        coordinator.apply_update(StateUpdate(properties, full=full))
        return dict(client.state.return_value)

    client.state.side_effect = read
    await coordinator.async_verify_state()
    assert coordinator.confirmed_at == confirmed
    assert coordinator.verification_stats["last_success"] is None
    assert coordinator.last_update_success
    assert coordinator.data["ref_door_ajar"] is True


@pytest.mark.parametrize("opened", [False, True])
async def test_complete_snapshot_supersedes_an_incomplete_push_during_read(loaded, freezer, opened):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    confirmed = coordinator.confirmed_at
    freezer.tick(1)

    async def read(_device_id):
        coordinator.apply_update(
            StateUpdate(
                {
                    "appliance_model": client.state.return_value["appliance_model"],
                    "ref_door_ajar": True,
                },
                full=True,
            )
        )
        coordinator.apply_update(
            StateUpdate({**client.state.return_value, "ref_door_ajar": opened}, full=True)
        )
        return dict(client.state.return_value)

    client.state.side_effect = read
    assert await coordinator.async_read_status() == dt_util.utcnow()
    assert coordinator.confirmed_at > confirmed
    assert not coordinator._completed_read.conflicted
    assert coordinator.data["ref_door_ajar"] is opened


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("full", [False, True])
async def test_failed_read_with_snapshot_then_conflicting_partial_is_unconfirmed(
    loaded, freezer, background, full
):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    freezer.tick(1)

    async def read(_device_id):
        coordinator.apply_update(StateUpdate(dict(client.state.return_value), full=True))
        properties = {"ref_door_ajar": True}
        if full:
            properties["appliance_model"] = client.state.return_value["appliance_model"]
        coordinator.apply_update(StateUpdate(properties, full=full))
        raise ApiError("Status request failed")

    client.state.side_effect = read
    if background:
        await coordinator.async_verify_state()
        assert coordinator.verification_stats["last_success"] is None
    else:
        with pytest.raises(HomeAssistantError, match="state changed during"):
            await coordinator.async_read_status()
    assert coordinator._completed_read.confirmed_at is None
    assert coordinator.last_update_success
    assert coordinator.data["ref_door_ajar"] is True


async def test_partial_property_missing_from_status_reply_is_unconfirmed(loaded, freezer):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    confirmed = coordinator.confirmed_at
    freezer.tick(1)

    async def read(_device_id):
        coordinator.apply_update(StateUpdate({"frz_door_ajar": True}, full=False))
        return dict(client.state.return_value)

    client.state.side_effect = read
    with pytest.raises(HomeAssistantError, match="state changed during"):
        await coordinator.async_read_status()
    assert coordinator.confirmed_at == confirmed
    assert coordinator.last_update_success
    assert coordinator.data["frz_door_ajar"] is True


@pytest.mark.parametrize("complete", [False, True])
async def test_failed_read_only_confirms_a_complete_concurrent_snapshot(
    hass, freezer, loaded, complete
):
    entry, client, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
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
    await coordinator.async_verify_state()
    if complete:
        assert coordinator.confirmed_at == dt_util.utcnow()
        assert coordinator.verification_stats["last_success"] is not None
        assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    else:
        assert coordinator.confirmed_at == confirmed
        assert coordinator.verification_stats["last_success"] is None
        assert "ref_door_ajar" in coordinator.missing_state_keys["snapshot"]
    client.open_channel.assert_not_awaited()


async def test_incomplete_status_reply_does_not_confirm_old_values(loaded, freezer):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    confirmed = coordinator.confirmed_at
    client.state.return_value = {"appliance_model": "ANOTHER-MODEL", "ref_set_temp": 39}
    freezer.tick(1)
    await coordinator.async_refresh()
    assert coordinator.confirmed_at == confirmed
    assert "ref_door_ajar" in coordinator.missing_state_keys["read"]


@pytest.mark.parametrize("source", ["read", "snapshot"])
async def test_repeated_incomplete_observations_keep_known_state_requirements(
    hass, loaded, freezer, source
):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    complete = dict(client.state.return_value)
    confirmed = coordinator.confirmed_at
    del client.state.return_value["ref_door_ajar"]

    for _ in range(2):
        freezer.tick(1)
        if source == "read":
            with pytest.raises(HomeAssistantError, match="complete state"):
                await coordinator.async_read_status()
        else:
            coordinator.apply_update(StateUpdate(dict(client.state.return_value), full=True))
        await hass.async_block_till_done()
        assert coordinator.confirmed_at == confirmed
        assert coordinator.missing_state_keys[source] == ["ref_door_ajar"]

    client.state.return_value = complete
    assert await coordinator.async_read_status() == dt_util.utcnow()
    freezer.tick(1)
    client.state.return_value = {"appliance_model": "REPLACEMENT", "ref_set_temp": 40}
    assert await coordinator.async_read_status() == dt_util.utcnow()
    assert not coordinator.missing_state_keys["read"]


async def test_invalid_model_does_not_erase_known_state_requirements(loaded, freezer):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    confirmed = coordinator.confirmed_at
    for reply in (
        {"appliance_model": ""},
        {"appliance_model": "ANOTHER-MODEL", "ref_set_temp": 39},
    ):
        client.state.return_value = reply
        freezer.tick(1)
        with pytest.raises(HomeAssistantError, match="complete state"):
            await coordinator.async_read_status()
        assert coordinator.confirmed_at == confirmed


@pytest.mark.parametrize("complete", [False, True])
async def test_status_confirmation_requires_door_state_but_not_notification_history(
    hass, loaded, freezer, complete
):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    coordinator.apply_update(
        StateUpdate({**client.state.return_value, "ref_door_ajar": True, "notifs": []}, full=True)
    )
    confirmed = coordinator.confirmed_at
    assert "notifs" in coordinator.data
    assert "notifs" not in client.state.return_value
    freezer.tick(1)

    if complete:
        assert await coordinator.async_read_status() == dt_util.utcnow()
        assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    else:
        del client.state.return_value["ref_door_ajar"]
        with pytest.raises(HomeAssistantError, match="complete state"):
            await coordinator.async_read_status()
        assert coordinator.confirmed_at == confirmed
        assert "ref_door_ajar" in coordinator.missing_state_keys["read"]


async def test_conflicted_read_still_records_the_keys_reads_report(loaded, freezer):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    snapshot = dict(client.state.return_value)
    client.state.return_value["max_ice_on"] = False
    freezer.tick(1)

    async def conflicted_read(_device_id):
        coordinator.apply_update(StateUpdate({"ref_door_ajar": True}, full=False))
        return dict(client.state.return_value)

    client.state.side_effect = conflicted_read
    with pytest.raises(HomeAssistantError, match="state changed during"):
        await coordinator.async_read_status()
    assert coordinator.data["max_ice_on"] is False
    freezer.tick(1)

    async def failed_read(_device_id):
        coordinator.apply_update(StateUpdate(snapshot, full=True))
        raise ApiError("Status request failed")

    # A snapshot without a key the conflicted reply reported cannot rescue a failed read.
    client.state.side_effect = failed_read
    with pytest.raises(HomeAssistantError, match="complete state"):
        await coordinator.async_read_status()
    assert coordinator.missing_state_keys["snapshot"] == ["max_ice_on"]


async def test_uncertified_read_for_another_model_keeps_known_requirements(loaded, freezer):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    confirmed = coordinator.confirmed_at
    freezer.tick(1)

    async def conflicted_read(_device_id):
        coordinator.apply_update(StateUpdate({"ref_door_ajar": True}, full=False))
        return {"appliance_model": "REPLACEMENT", "ref_set_temp": 40, "ref_door_ajar": False}

    client.state.side_effect = conflicted_read
    with pytest.raises(HomeAssistantError, match="state changed during"):
        await coordinator.async_read_status()
    assert coordinator.data["appliance_model"] == "REPLACEMENT"
    freezer.tick(1)
    coordinator.apply_update(StateUpdate({"appliance_model": "ANOTHER-MODEL"}, full=True))
    assert coordinator.confirmed_at == confirmed
    assert "ref_door_ajar" in coordinator.missing_state_keys["snapshot"]


async def test_failed_read_does_not_replay_updates_a_replacing_snapshot_superseded(
    hass, freezer, loaded, monkeypatch
):
    entry, client, updates, _, _ = loaded
    monkeypatch.setattr("custom_components.subzero.coordinator.random.uniform", lambda *_: 0)
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    reading, release = asyncio.Event(), asyncio.Event()

    async def failed_read(_device_id):
        reading.set()
        await release.wait()
        raise ApiError("Status request failed")

    client.state.side_effect = failed_read
    request = asyncio.create_task(coordinator.async_read_status())
    await asyncio.wait_for(reading.wait(), 1)
    coordinator.apply_update(StateUpdate({"frz_door_ajar": True}, full=False))
    await updates.put(ApiError("Stream disconnected"))
    await hass.async_block_till_done()
    freezer.tick(31)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    await updates.put(StateUpdate(dict(client.state.return_value), full=True))
    await hass.async_block_till_done()
    assert "frz_door_ajar" not in coordinator.data
    release.set()
    # The snapshot could not refresh the door, so the rescued read is not certified.
    with pytest.raises(HomeAssistantError, match="state changed during"):
        await request
    assert "frz_door_ajar" not in coordinator.data
    assert hass.states.get("binary_sensor.kitchen_freezer_door").state == "unavailable"
    assert coordinator.last_update_success
    assert coordinator._transport_error is None


async def test_partial_superseded_by_a_replacing_snapshot_still_conflicts_with_the_reply(
    loaded, freezer
):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    coordinator.async_set_update_error(ApiError("Status request failed"))
    assert not coordinator.last_update_success
    freezer.tick(1)

    async def read(_device_id):
        coordinator.apply_update(StateUpdate({"ref_door_ajar": True}, full=False))
        # An incomplete snapshot replaces the unavailable state, but it says nothing
        # about the door, so the partial still contradicts the reply's closed door.
        coordinator.apply_update(
            StateUpdate({"appliance_model": "ANOTHER-MODEL", "ref_set_temp": 38}, full=True)
        )
        assert coordinator.last_update_success
        return dict(client.state.return_value)

    client.state.side_effect = read
    with pytest.raises(HomeAssistantError, match="state changed during"):
        await coordinator.async_read_status()
    assert coordinator.last_update_success
    assert coordinator.data["ref_door_ajar"] is False


@pytest.mark.parametrize("door_changed", [False, True])
async def test_failed_read_is_not_certified_when_a_later_snapshot_replaces_its_rescue(
    loaded, freezer, door_changed
):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    freezer.tick(1)

    async def failed_read(_device_id):
        coordinator.apply_update(StateUpdate(dict(client.state.return_value), full=True))
        if door_changed:
            coordinator.apply_update(StateUpdate({"ref_door_ajar": True}, full=False))
        coordinator.async_set_update_error(ApiError("Channel error"))
        # Replacing the unavailable state drops the door the complete snapshot certified.
        coordinator.apply_update(
            StateUpdate({"appliance_model": "ANOTHER-MODEL", "ref_set_temp": 38}, full=True)
        )
        raise ApiError("Status request failed")

    client.state.side_effect = failed_read
    with pytest.raises(HomeAssistantError, match="state changed during"):
        await coordinator.async_read_status()
    assert coordinator.last_update_success
    assert "ref_door_ajar" not in coordinator.data


async def test_read_lacking_a_key_a_superseded_snapshot_certified_is_not_confirmed(loaded, freezer):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    freezer.tick(1)

    async def read(_device_id):
        coordinator.apply_update(
            StateUpdate({**client.state.return_value, "ref_door_ajar": True}, full=True)
        )
        coordinator.async_set_update_error(ApiError("Channel error"))
        coordinator.apply_update(
            StateUpdate({"appliance_model": "ANOTHER-MODEL", "ref_set_temp": 38}, full=True)
        )
        return {
            key: value for key, value in client.state.return_value.items() if key != "ref_door_ajar"
        }

    client.state.side_effect = read
    with pytest.raises(HomeAssistantError, match="state changed during"):
        await coordinator.async_read_status()
    assert "ref_door_ajar" not in coordinator.data


async def test_replay_restart_keeps_the_reply_clock_for_ordering_later_snapshots(loaded, freezer):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    coordinator.async_set_update_error(ApiError("Channel error"))
    fresh = {
        **client.state.return_value,
        "ref_door_ajar": True,
        "time": "2026-10-24T12:00:00+00:00",
        "uptime": "13:00:00",
    }
    stale = {
        **client.state.return_value,
        "ref_door_ajar": False,
        "time": "2026-10-24T11:00:00+00:00",
        "uptime": "12:00:00",
    }
    freezer.tick(1)

    async def read(_device_id):
        coordinator.apply_update(
            StateUpdate({"appliance_model": "ANOTHER-MODEL", "ref_set_temp": 38}, full=True)
        )
        coordinator.apply_update(StateUpdate(stale, full=True))
        return dict(fresh)

    client.state.side_effect = read
    assert await coordinator.async_read_status() == dt_util.utcnow()
    assert coordinator.data["ref_door_ajar"] is True


async def test_reply_superseded_by_a_reconnect_snapshot_is_rescued_by_it(
    hass, freezer, loaded, monkeypatch
):
    entry, client, updates, _, _ = loaded
    monkeypatch.setattr("custom_components.subzero.coordinator.random.uniform", lambda *_: 0)
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    await updates.put(StateUpdate({"frz_door_ajar": True}, full=False))
    await hass.async_block_till_done()
    reading, release = asyncio.Event(), asyncio.Event()

    async def read(_device_id):
        reading.set()
        await release.wait()
        # Answered before the interruption, while the freezer door was still open.
        return {**client.state.return_value, "frz_door_ajar": True}

    client.state.side_effect = read
    request = asyncio.create_task(coordinator.async_read_status())
    await asyncio.wait_for(reading.wait(), 1)
    await updates.put(ApiError("Stream disconnected"))
    await hass.async_block_till_done()
    freezer.tick(31)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    await updates.put(StateUpdate(dict(client.state.return_value), full=True))
    await hass.async_block_till_done()
    release.set()
    assert await request == coordinator.confirmed_at
    assert "frz_door_ajar" not in coordinator.data
    assert hass.states.get("binary_sensor.kitchen_freezer_door").state == "unavailable"
    # A superseded reply is not an observation later snapshots are held to.
    coordinator.apply_update(StateUpdate(dict(client.state.return_value), full=True))
    assert coordinator.missing_state_keys["snapshot"] == []


async def test_replaced_snapshot_values_matching_the_reply_do_not_conflict(loaded, freezer):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    typed = {**client.state.return_value, "appliance_type": "1.2.6.1"}
    freezer.tick(1)

    async def read(_device_id):
        coordinator.apply_update(StateUpdate(typed, full=True))
        coordinator.async_set_update_error(ApiError("Channel error"))
        # The replacing snapshot omits the type, which the known one stands in for.
        coordinator.apply_update(
            StateUpdate({"appliance_model": "ANOTHER-MODEL", "ref_set_temp": 38}, full=True)
        )
        return dict(typed)

    client.state.side_effect = read
    assert await coordinator.async_read_status() == dt_util.utcnow()
    assert coordinator.data["appliance_type"] == "1.2.6.1"
    assert coordinator.data["ref_door_ajar"] is False


async def test_read_is_not_certified_when_its_published_state_lacks_a_read_key(
    hass, loaded, freezer
):
    entry, client, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    fixture = dict(client.state.return_value)

    def clocked(hour: int) -> dict:
        return {
            **fixture,
            "time": f"2026-10-24T{hour:02d}:00:00+00:00",
            "uptime": f"{hour:02d}:00:00",
        }

    freezer.tick(1)

    async def read(_device_id):
        await updates.put(ApiError("Stream disconnected"))
        await hass.async_block_till_done()
        # A complete snapshot certifies state after the reconnect, then an incomplete
        # one replaces the unavailable state. The reply is newer than the complete
        # snapshot, which the replay skips, and predates the reconnect, so it is
        # superseded; what is published lacks the door.
        coordinator.apply_update(StateUpdate(clocked(11), full=True))
        coordinator.async_set_update_error(ApiError("Channel error"))
        coordinator.apply_update(
            StateUpdate({"appliance_model": "ANOTHER-MODEL", "ref_set_temp": 38}, full=True)
        )
        return clocked(12)

    client.state.side_effect = read
    with pytest.raises(HomeAssistantError, match="complete state"):
        await coordinator.async_read_status()
    assert "ref_door_ajar" not in coordinator.data


async def test_each_source_requires_only_the_keys_it_has_reported(hass, loaded, freezer):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    # Snapshots carry a key the status reply never does; another only arrives in partials.
    coordinator.apply_update(
        StateUpdate({**client.state.return_value, "frz_door_ajar": False}, full=True)
    )
    coordinator.apply_update(StateUpdate({"ice_maker_on": True}, full=False))
    await hass.async_block_till_done()
    freezer.tick(1)
    assert await coordinator.async_read_status() == dt_util.utcnow()
    assert coordinator.missing_state_keys["read"] == []
    confirmed = coordinator.confirmed_at
    freezer.tick(1)
    coordinator.apply_update(StateUpdate(dict(client.state.return_value), full=True))
    assert coordinator.missing_state_keys["snapshot"] == ["frz_door_ajar"]
    assert coordinator.confirmed_at == confirmed


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


async def test_manual_request_during_background_read_reports_its_own_failure(hass, loaded):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    reading, release = asyncio.Event(), asyncio.Event()
    client.push_connected = True
    client.state.reset_mock()
    coordinator._read_failed = Mock()

    async def read(_device_id):
        if client.state.await_count == 1:
            reading.set()
            await release.wait()
            return dict(client.state.return_value)
        raise ApiError("Manual read failed")

    client.state.side_effect = read
    background = asyncio.create_task(coordinator.async_verify_state())
    await reading.wait()
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
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "unavailable"


async def test_cancelled_queued_manual_read_does_not_change_later_background_failure_policy(
    hass, loaded
):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    reading, release = asyncio.Event(), asyncio.Event()
    client.push_connected = True
    client.state.reset_mock()
    coordinator._read_failed = Mock()

    async def read(_device_id):
        if client.state.await_count == 1:
            reading.set()
            await release.wait()
            return dict(client.state.return_value)
        raise ApiError("Background read failed")

    client.state.side_effect = read
    background = asyncio.create_task(coordinator.async_verify_state())
    await reading.wait()
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
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    published.assert_not_called()


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


async def test_confirmation_sensor_keeps_the_last_observation_when_offline(
    hass, loaded, enabled_confirmation_sensor
):
    entry, _, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    timestamp = hass.states.get(enabled_confirmation_sensor).state
    assert timestamp == coordinator.confirmed_at.replace(microsecond=0).isoformat()
    coordinator.async_set_update_error(ApiError("Offline"))
    assert hass.states.get(enabled_confirmation_sensor).state == timestamp


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


async def test_a_read_started_before_disconnect_does_not_confirm_the_new_gap(hass, freezer, loaded):
    entry, client, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    confirmed = coordinator.confirmed_at
    reading, release = asyncio.Event(), asyncio.Event()

    async def read(_device_id):
        reading.set()
        await release.wait()
        return dict(client.state.return_value)

    client.state.side_effect = read
    request = asyncio.create_task(coordinator.async_read_status())
    await asyncio.wait_for(reading.wait(), 1)
    client.push_connected = False
    await updates.put(RateLimited(300))
    await hass.async_block_till_done()
    freezer.tick(1)
    release.set()
    with pytest.raises(HomeAssistantError, match="complete state"):
        await request
    assert coordinator.confirmed_at == confirmed
    assert coordinator.last_update_success


async def test_incomplete_observations_cannot_revive_expired_transport_state(hass, freezer, loaded):
    entry, client, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    complete = dict(client.state.return_value)
    client.push_connected = False
    await updates.put(RateLimited(300))
    await hass.async_block_till_done()
    freezer.tick(121)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert not coordinator.last_update_success

    coordinator.apply_update(
        StateUpdate({"appliance_model": complete["appliance_model"], "ref_set_temp": 39}, full=True)
    )
    assert not coordinator.last_update_success
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "unavailable"
    client.state.return_value = {
        key: value for key, value in complete.items() if key != "ref_door_ajar"
    }
    with pytest.raises(HomeAssistantError, match="complete state"):
        await coordinator.async_read_status()
    assert not coordinator.last_update_success

    client.state.return_value = complete
    assert await coordinator.async_read_status() == dt_util.utcnow()
    assert coordinator.last_update_success
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"


@pytest.mark.parametrize("key", ["time", "uptime", "ap_rssi"])
async def test_clock_and_signal_readings_in_an_incomplete_snapshot_do_not_conflict(
    freezer, loaded, key
):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    reply = {
        **client.state.return_value,
        "time": "2025-01-01T12:00:00+00:00",
        "uptime": "13:00:00",
        "ap_rssi": -50,
    }
    client.state.return_value = reply
    await coordinator.async_read_status()
    confirmed = coordinator.confirmed_at
    freezer.tick(1)
    pushed = {"time": "2025-01-01T04:00:01-08:00", "uptime": "13:00:01", "ap_rssi": -54}

    async def read(device_id):
        # A snapshot without ref_door_ajar that differs from the reply in one reading.
        coordinator.apply_update(
            StateUpdate(
                {
                    "appliance_model": reply["appliance_model"],
                    "ref_set_temp": reply["ref_set_temp"],
                    key: pushed[key],
                },
                full=True,
            )
        )
        return dict(reply)

    client.state.side_effect = read
    assert await coordinator.async_read_status() > confirmed


async def test_conflicted_read_after_push_returns_ends_the_reconnect_wait(freezer, loaded):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    coordinator.async_transport_error(ApiError("Stream disconnected"))
    assert client.push_connected
    freezer.tick(1)

    async def read(device_id):
        coordinator.apply_update(StateUpdate({"ref_door_ajar": True}, full=False))
        return dict(client.state.return_value)

    client.state.side_effect = read
    with pytest.raises(HomeAssistantError, match="state changed during"):
        await coordinator.async_read_status()
    assert coordinator.data["ref_door_ajar"] is True
    assert coordinator._transport_error is None


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
        await updates.put(StateUpdate({"ref_set_temp": 39}, full=False))
        await hass.async_block_till_done()
    assert coordinator.confirmed_at == confirmed
    assert (
        hass.states.get(enabled_confirmation_sensor).state
        == confirmed.replace(microsecond=0).isoformat()
    )
    with pytest.raises(HomeAssistantError):
        await coordinator.async_read_status()


async def _advance_followup(hass, freezer, coordinator, seconds):
    freezer.tick(seconds)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    if coordinator._read_task is not None:
        await coordinator._read_task
    await hass.async_block_till_done()


async def test_disabling_polling_cancels_a_followup_with_periodic_checks_already_off(
    hass, freezer, loaded
):
    entry, client, _, _, _ = loaded
    account = entry.runtime_data
    coordinator = account.coordinators["test-fridge"]
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
    hass, freezer, loaded
):
    entry, client, updates, _, _ = loaded
    account = entry.runtime_data
    coordinator = account.coordinators["test-fridge"]
    client.state.reset_mock()
    client.state.return_value["ref_door_ajar"] = True
    await updates.put(StateUpdate({"ref_door_ajar": True}, full=False))
    await hass.async_block_till_done()
    reading, release = asyncio.Event(), asyncio.Event()

    async def read(_device_id):
        reading.set()
        await release.wait()
        return dict(client.state.return_value)

    client.state.side_effect = read
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
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"
    assert client.watched == [["test-fridge"]]

    client.state.return_value["ref_door_ajar"] = False
    release.set()
    await status_read
    await _advance_followup(hass, freezer, coordinator, 301)
    client.state.assert_awaited_once_with("test-fridge")
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"

    hass.config_entries.async_update_entry(
        entry, pref_disable_polling=False, options={"status_poll_interval": 60}
    )
    account.async_update_state_checks()
    await _advance_followup(hass, freezer, coordinator, 61)
    assert client.state.await_count == 2
    assert entry.runtime_data is account


@pytest.mark.parametrize("interval", [0, 300])
async def test_followups_repair_a_missed_door_close_and_stop(hass, freezer, loaded, interval):
    entry, client, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    hass.config_entries.async_update_entry(entry, options={"status_poll_interval": interval})
    client.state.reset_mock()
    client.state.return_value["ref_door_ajar"] = True
    await updates.put(StateUpdate({"ref_door_ajar": True}, full=False))
    await hass.async_block_till_done()
    coordinator.async_receive_event(dt_util.utcnow(), 0, 101)
    await _advance_followup(hass, freezer, coordinator, 3)
    assert client.state.await_count == 1
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"
    client.state.return_value["ref_door_ajar"] = False
    await _advance_followup(hass, freezer, coordinator, 61)
    assert client.state.await_count == 2
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    await _advance_followup(hass, freezer, coordinator, 121)
    assert client.state.await_count == 2
    assert coordinator._followup_task is None


async def test_alert_bursts_and_unrelated_updates_do_not_extend_followups(hass, freezer, loaded):
    entry, client, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    client.state.return_value["ref_door_ajar"] = True
    client.state.reset_mock()
    for sequence in range(5):
        await updates.put(
            StateUpdate(
                {
                    "ref_door_ajar": True,
                    "notifs": [
                        {
                            "notif_seq": sequence,
                            "notif_type": 101,
                            "timestamp": dt_util.utcnow().isoformat(),
                        }
                    ],
                },
                full=False,
            )
        )
    await hass.async_block_till_done()
    for seconds in (3, 61, 121, 100):
        await _advance_followup(hass, freezer, coordinator, seconds)
        await updates.put(StateUpdate({"ref_set_temp": 39}, full=False))
        await hass.async_block_till_done()
    assert client.state.await_count == 3
    assert coordinator._followup_task is None


async def test_a_pushed_close_cancels_pending_followups(hass, freezer, loaded):
    entry, client, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    client.state.reset_mock()
    client.state.return_value["ref_door_ajar"] = True
    await updates.put(StateUpdate({"ref_door_ajar": True}, full=False))
    await hass.async_block_till_done()
    coordinator.async_receive_event(dt_util.utcnow(), 0, 101)
    await _advance_followup(hass, freezer, coordinator, 3)
    client.state.assert_awaited_once()
    client.state.reset_mock()
    await updates.put(StateUpdate({"ref_door_ajar": False}, full=False))
    await hass.async_block_till_done()
    await _advance_followup(hass, freezer, coordinator, 61)
    client.state.assert_not_awaited()
    assert coordinator._followup_task is None


async def test_event_listeners_see_state_from_the_same_message(hass, loaded):
    entry, _, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    observed = []
    coordinator.async_add_event_listener(
        lambda _: observed.append(hass.states.get("binary_sensor.kitchen_refrigerator_door").state)
    )
    await updates.put(
        StateUpdate(
            {
                "ref_door_ajar": True,
                "notifs": [
                    {"notif_seq": 1, "notif_type": 101, "timestamp": dt_util.utcnow().isoformat()}
                ],
            },
            full=False,
        )
    )
    await hass.async_block_till_done()
    assert observed == ["on"]


async def test_rate_limits_stop_event_followups_without_claiming_new_state(hass, freezer, loaded):
    entry, client, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    confirmed = coordinator.confirmed_at
    client.state.reset_mock()
    client.state.side_effect = RateLimited(300)
    await updates.put(StateUpdate({"ref_door_ajar": True}, full=False))
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
    hass, freezer, loaded, rate_limited
):
    entry, client, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    reading, release = asyncio.Event(), asyncio.Event()

    async def read(_device_id):
        reading.set()
        await release.wait()
        raise RateLimited(60) if rate_limited else ApiError("Status read failed")

    client.state.side_effect = read
    client.state.return_value["ref_door_ajar"] = True
    await updates.put(StateUpdate({"ref_door_ajar": True}, full=False))
    await hass.async_block_till_done()
    coordinator.async_receive_event(dt_util.utcnow(), 0, 101)
    freezer.tick(3)
    async_fire_time_changed(hass, dt_util.utcnow())
    await asyncio.wait_for(reading.wait(), 1)
    await updates.put(
        StateUpdate(
            {
                "notifs": [
                    {"notif_seq": 1, "notif_type": 101, "timestamp": dt_util.utcnow().isoformat()}
                ]
            },
            full=False,
        )
    )
    await hass.async_block_till_done()
    release.set()
    await coordinator._read_task
    client.state.side_effect = None
    client.state.return_value["ref_door_ajar"] = False
    await updates.put(StateUpdate(dict(client.state.return_value), full=True))
    await hass.async_block_till_done()
    if task := coordinator._followup_task:
        assert task.cancelling()
        await asyncio.gather(task, return_exceptions=True)
    assert coordinator._followup_task is None
    client.state.reset_mock()
    for opened in (True, False):
        await updates.put(StateUpdate({"ref_door_ajar": opened}, full=False))
        await hass.async_block_till_done()
    await _advance_followup(hass, freezer, coordinator, 61)
    client.state.assert_not_awaited()
    assert coordinator._followup_task is None


@pytest.mark.parametrize("failure", ["transient", "rate_limited", "unavailable"])
@pytest.mark.parametrize("new_alert", [False, True])
async def test_alert_during_failed_final_followup_starts_a_new_window(
    hass, freezer, loaded, failure, new_alert
):
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
        assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"


async def test_disabled_polling_also_disables_automatic_event_followups(hass, freezer, loaded):
    entry, client, updates, _, _ = loaded
    hass.config_entries.async_update_entry(entry, pref_disable_polling=True)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    client.state.reset_mock()
    await updates.put(StateUpdate({"ref_door_ajar": True}, full=False))
    await hass.async_block_till_done()
    coordinator.async_receive_event(dt_util.utcnow(), 0, 101)
    await _advance_followup(hass, freezer, coordinator, 10)
    client.state.assert_not_awaited()
    await coordinator.async_read_status()
    client.state.assert_awaited_once()


async def test_unloading_cancels_event_followup_timers(hass, freezer, loaded):
    entry, client, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    await updates.put(StateUpdate({"ref_door_ajar": True}, full=False))
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


@pytest.mark.parametrize(
    "newer", ["failed_read", "rate_limited", "snapshot", "incomplete_snapshot"]
)
async def test_an_older_refresh_cannot_overwrite_a_newer_read_or_snapshot(hass, loaded, newer):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
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
    await reading.wait()
    subsequent = []

    async def failed_read():
        with pytest.raises(HomeAssistantError):
            await coordinator.async_read_status()

    def after_read(_task):
        if newer in {"snapshot", "incomplete_snapshot"}:
            properties = dict(client.state.return_value)
            if newer == "incomplete_snapshot":
                del properties["ref_door_ajar"]
            coordinator.apply_update(StateUpdate(properties, full=True))
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
    if newer == "incomplete_snapshot":
        assert coordinator.confirmed_at == confirmed
    if newer == "rate_limited":
        client.state.side_effect = RateLimited(60)
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()


async def test_verification_resumes_after_auth_failure_is_recovered_by_push(hass, freezer, loaded):
    from custom_components.subzero.auth import InvalidAuth

    entry, client, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    client.state.reset_mock()
    client.state.side_effect = InvalidAuth("Sign-in expired")
    freezer.tick(301)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    await entry.runtime_data._verifications["test-fridge"]
    assert not coordinator.last_update_success
    await updates.put(StateUpdate(dict(client.state.return_value), full=True))
    await hass.async_block_till_done()
    assert coordinator.last_update_success

    client.state.side_effect = None
    freezer.tick(301)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    await entry.runtime_data._verifications["test-fridge"]
    assert client.state.await_count == 2
    client.open_channel.assert_not_awaited()
    assert coordinator.verification_stats["checks"] == 2
    assert coordinator.verification_stats["status_reads"] == 2


async def test_an_unexpected_event_listener_failure_does_not_wedge_future_reads(hass, loaded):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
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
