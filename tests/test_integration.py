"""Load real HA platforms and exercise entity discovery and push lifecycle."""

import asyncio
import json
import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import pytest
from homeassistant.components.sensor import DATA_COMPONENT as SENSOR_COMPONENT
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.subzero.api import (
    ApiError,
    Appliance,
    ChannelOpened,
    RateLimited,
    StateUpdate,
    parse_notification,
    token_state,
)
from custom_components.subzero.auth import InvalidAuth
from custom_components.subzero.const import DOMAIN
from custom_components.subzero.coordinator import SubZeroAccount

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture
async def loaded(hass, tokens, request):
    hass.config.units = US_CUSTOMARY_SYSTEM
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Kitchen",
        unique_id="test-fridge",
        data={
            "device_id": "test-fridge",
            "temperature_unit": getattr(request, "param", "F"),
            "tokens": token_state(tokens),
        },
        # Timing tests use five-minute checks explicitly.
        options={"status_poll_interval": 300},
    )
    entry.add_to_hass(hass)
    updates = asyncio.Queue()
    connected = asyncio.Event()
    disconnected = asyncio.Event()

    async def watch(device_ids):
        assert isinstance(device_ids, list)
        client.watched.append(device_ids)
        client.push_connected = True
        connected.set()
        try:
            while True:
                event = await updates.get()
                if isinstance(event, Exception):
                    raise event
                yield event if isinstance(event, tuple) else ("test-fridge", event)
        finally:
            client.push_connected = False
            disconnected.set()

    with patch("custom_components.subzero.SubZeroClient") as factory:
        client = factory.return_value
        client.tokens = token_state(tokens)
        client.notification_stats = {
            "received": 0,
            "ignored": 0,
            "invalid": 0,
            "last_received": None,
        }
        client.watched = []
        client.open_channel = AsyncMock()
        client.appliance_faults = AsyncMock(return_value=[])
        client.fault_metadata = AsyncMock(return_value=None)
        client.appliances = AsyncMock(
            return_value=[
                Appliance("test-fridge", "Kitchen", getattr(request, "param", "F")),
                Appliance("test-oven", "Wall oven", "F"),
            ]
        )
        client.state = AsyncMock(
            return_value={
                "appliance_model": "ANOTHER-MODEL",
                "ref_set_temp": 38,
                "ref_door_ajar": False,
                "ap_ssid": "private-network",
                "appliance_serial": "private-serial",
                "version": {"fw": "1.0"},
            }
        )
        client.watch = watch
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await connected.wait()
        yield entry, client, updates, disconnected, factory
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()


async def test_reported_properties_drive_entity_discovery(hass, loaded):
    entry, client, _, _, _ = loaded
    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert {entity.unique_id for entity in entities} == {
        "test-fridge_appliance_event",
        "test-fridge_ref_set_temp",
        "test-fridge_ref_door_ajar",
        "test-fridge_live_reporting_mode",
        "test-fridge_state_confirmed_at",
        "test-fridge_active_faults",
    }
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "38"
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    assert (
        registry.async_get("sensor.kitchen_state_confirmed").disabled_by
        is er.RegistryEntryDisabler.INTEGRATION
    )
    assert hass.states.get("sensor.kitchen_state_confirmed") is None
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    assert "ap_ssid" not in coordinator.data
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "test-fridge"), entry.entry_id
    )
    assert device.serial_number == "private-serial"
    assert coordinator.update_interval is None
    assert entry.version == 2
    assert entry.unique_id == "test-fridge"
    assert entry.data["devices"] == {"test-fridge": {"name": "Kitchen", "temperature_unit": "F"}}
    client.state.assert_awaited_once()


@pytest.mark.parametrize("saved_interval", [None, 600])
async def test_state_checks_default_off_and_preserve_saved_interval(hass, loaded, saved_interval):
    entry, client, updates, _, _ = loaded
    options = {} if saved_interval is None else {"status_poll_interval": saved_interval}
    hass.config_entries.async_update_entry(entry, options=options)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    client.state.reset_mock()
    client.open_channel.reset_mock()
    connections = list(client.watched)
    started = dt_util.utcnow()
    async_fire_time_changed(hass, started + timedelta(seconds=301))
    await hass.async_block_till_done()
    client.state.assert_not_awaited()
    async_fire_time_changed(hass, started + timedelta(seconds=601))
    await hass.async_block_till_done()
    if saved_interval is None:
        async_fire_time_changed(hass, started + timedelta(hours=24))
        await hass.async_block_till_done()
        client.state.assert_not_awaited()
    else:
        client.state.assert_awaited_once_with("test-fridge")
    await updates.put(StateUpdate({"ref_set_temp": 39}, full=False))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "39"
    client.open_channel.assert_not_awaited()
    assert client.watched == connections


@pytest.mark.parametrize("system_option", [False, True])
async def test_disabled_state_checks_do_not_request_periodic_status(hass, loaded, system_option):
    entry, client, _, _, _ = loaded
    if system_option:
        hass.config_entries.async_update_entry(entry, pref_disable_polling=True)
    else:
        hass.config_entries.async_update_entry(entry, options={"status_poll_interval": 0})
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    client.state.reset_mock()
    for hours in (1, 12, 24):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=hours))
        await hass.async_block_till_done()
    client.state.assert_not_awaited()
    client.open_channel.assert_not_awaited()
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "38"


@pytest.mark.parametrize("full", [False, True])
async def test_state_checks_repair_a_missed_close_despite_recent_pushes(hass, loaded, full):
    entry, client, updates, disconnected, _ = loaded
    started = dt_util.utcnow()
    await updates.put(StateUpdate({"ref_door_ajar": True}, full=False))
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"
    client.state.reset_mock()
    async_fire_time_changed(hass, started + timedelta(seconds=240))
    await hass.async_block_till_done()
    properties = {"ref_set_temp": 39}
    if full:
        properties = {**client.state.return_value, **properties, "ref_door_ajar": True}
    await updates.put(StateUpdate(properties, full=full))
    await hass.async_block_till_done()
    client.state.assert_not_awaited()
    async_fire_time_changed(hass, started + timedelta(seconds=301))
    await hass.async_block_till_done()
    await entry.runtime_data._verifications["test-fridge"]
    client.state.assert_awaited_once_with("test-fridge")
    client.open_channel.assert_not_awaited()
    assert client.watched == [["test-fridge"]]
    assert not disconnected.is_set()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    stats = entry.runtime_data.coordinators["test-fridge"].verification_stats
    assert stats["checks"] == 1
    assert stats["status_reads"] == 1
    assert stats["last_success"] is not None


async def test_verification_does_not_overlap_and_stops_on_unload(hass, loaded):
    entry, client, _, disconnected, _ = loaded
    started = dt_util.utcnow()
    reading = asyncio.Event()

    async def blocked_request(device_id):
        reading.set()
        await asyncio.Event().wait()

    client.state.reset_mock()
    client.state.side_effect = blocked_request
    async_fire_time_changed(hass, started + timedelta(seconds=301))
    await hass.async_block_till_done()
    await asyncio.wait_for(reading.wait(), 1)
    async_fire_time_changed(hass, started + timedelta(seconds=602))
    await hass.async_block_till_done()
    client.state.assert_awaited_once_with("test-fridge")
    account = entry.runtime_data
    task = account._verifications["test-fridge"]
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert disconnected.is_set()
    assert task.cancelled()
    assert account.coordinators["test-fridge"]._read_updates is None
    async_fire_time_changed(hass, started + timedelta(seconds=903))
    await hass.async_block_till_done()
    client.state.assert_awaited_once_with("test-fridge")
    client.open_channel.assert_not_awaited()
    assert client.watched == [["test-fridge"]]


async def test_verification_failure_preserves_available_push_state(hass, loaded):
    entry, client, updates, _, _ = loaded
    client.push_connected = True
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    await updates.put(StateUpdate({"ref_door_ajar": True}, full=False))
    await hass.async_block_till_done()
    state = hass.states.get("binary_sensor.kitchen_refrigerator_door")
    confirmed = coordinator.confirmed_at
    completed = coordinator._completed_read
    client.state.side_effect = ApiError("Status unavailable")
    started = dt_util.utcnow()
    async_fire_time_changed(hass, started + timedelta(seconds=301))
    await hass.async_block_till_done()
    await entry.runtime_data._verifications["test-fridge"]
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door") is state
    assert state.state == "on"
    assert coordinator.last_update_success
    assert coordinator.confirmed_at == confirmed
    assert coordinator._completed_read is completed
    assert coordinator.verification_stats["last_success"] is None
    assert not entry.runtime_data._recoveries
    assert client.state.await_count == 2
    client.state.side_effect = None
    async_fire_time_changed(hass, started + timedelta(seconds=602))
    await hass.async_block_till_done()
    await entry.runtime_data._verifications["test-fridge"]
    assert client.state.await_count == 3
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    assert coordinator.verification_stats["last_success"] is not None
    client.open_channel.assert_not_awaited()


async def test_verification_failure_without_push_marks_state_unavailable(hass, loaded):
    entry, client, _, _, _ = loaded
    client.push_connected = False
    client.state.side_effect = ApiError("Status unavailable")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=301))
    await hass.async_block_till_done()
    await entry.runtime_data._verifications["test-fridge"]
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "unavailable"
    assert not entry.runtime_data.coordinators["test-fridge"].last_update_success
    assert not entry.runtime_data._recoveries
    assert client.state.await_count == 2
    client.open_channel.assert_not_awaited()


async def test_rate_limited_verification_read_preserves_available_state(hass, loaded):
    entry, client, _, _, _ = loaded
    client.state.side_effect = RateLimited(900)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=301))
    await hass.async_block_till_done()
    await entry.runtime_data._verifications["test-fridge"]
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    assert entry.runtime_data.coordinators["test-fridge"].verification_stats["last_success"] is None
    assert client.state.await_count == 2
    client.open_channel.assert_not_awaited()


async def test_verification_handles_a_stream_failure_during_its_status_read(hass, loaded):
    entry, client, updates, disconnected, _ = loaded
    client.push_connected = True
    reading = asyncio.Event()
    release = asyncio.Event()

    async def state(device_id):
        reading.set()
        await release.wait()
        return dict(client.state.return_value)

    client.state.side_effect = state
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=301))
    await hass.async_block_till_done()
    await asyncio.wait_for(reading.wait(), 1)
    client.push_connected = False
    await updates.put(RuntimeError("Unexpected stream failure"))
    await disconnected.wait()
    await hass.async_block_till_done()
    release.set()
    task = entry.runtime_data._verifications["test-fridge"]
    await task
    assert task.exception() is None
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"


async def test_staggered_state_checks_keep_other_appliances_reporting(hass, oven_loaded):
    entry, client, _ = oven_loaded
    started = dt_util.utcnow()

    async def state(device_id):
        if device_id == "test-fridge":
            await asyncio.Event().wait()
        return dict(entry.runtime_data.coordinators[device_id].data)

    client.state.reset_mock()
    client.state.side_effect = state
    for seconds in (151, 302, 453, 604):
        async_fire_time_changed(hass, started + timedelta(seconds=seconds))
        await hass.async_block_till_done()
        if seconds in (302, 604):
            await entry.runtime_data._verifications["test-oven"]
    assert [args.args[0] for args in client.state.await_args_list] == [
        "test-fridge",
        "test-oven",
        "test-oven",
    ]
    assert entry.runtime_data.coordinators["test-oven"].verification_stats["checks"] == 2
    client.open_channel.assert_not_awaited()


async def test_verification_auth_failure_starts_reauthentication(hass, loaded):
    entry, client, _, _, _ = loaded
    client.state.side_effect = InvalidAuth("Expired")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=301))
    await hass.async_block_till_done()
    await entry.runtime_data._verifications["test-fridge"]
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "unavailable"
    assert (
        hass.config_entries.flow.async_progress_by_handler(DOMAIN)[0]["context"]["source"]
        == "reauth"
    )
    assert client.state.await_count == 2
    client.open_channel.assert_not_awaited()


async def test_initial_push_can_load_without_a_status_read(hass, loaded, monkeypatch):
    entry, client, _, _, _ = loaded
    monkeypatch.setattr("custom_components.subzero.coordinator.INITIAL_STATE_TIMEOUT", 16)
    closed = asyncio.Event()

    async def watch(device_ids):
        try:
            yield (
                "test-fridge",
                StateUpdate({"appliance_model": "ANOTHER-MODEL", "ref_door_ajar": True}, full=True),
            )
            await asyncio.Event().wait()
        finally:
            closed.set()

    client.watch = watch
    client.state.reset_mock()
    client.state.side_effect = ApiError("Status reads unavailable")
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"
    client.state.assert_not_awaited()
    await hass.config_entries.async_unload(entry.entry_id)
    assert closed.is_set()


async def test_startup_read_keeps_newer_push_state(hass, loaded):
    entry, client, _, _, _ = loaded
    connected = asyncio.Event()
    delivered = asyncio.Event()
    updates = asyncio.Queue()

    async def watch(device_ids):
        connected.set()
        while True:
            yield "test-fridge", await updates.get()
            delivered.set()

    async def state(device_id):
        assert connected.is_set()
        await updates.put(StateUpdate({"ref_door_ajar": True}, full=False))
        await delivered.wait()
        return {"appliance_model": "ANOTHER-MODEL", "ref_door_ajar": False, "ref_set_temp": 38}

    client.watch = watch
    client.state.side_effect = state
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "38"


async def test_serial_follows_newer_push_state(hass, loaded):
    entry, client, _, _, _ = loaded
    connected = asyncio.Event()
    delivered = asyncio.Event()
    updates = asyncio.Queue()

    async def watch(device_ids):
        connected.set()
        while True:
            yield "test-fridge", await updates.get()
            delivered.set()

    async def state(device_id):
        assert connected.is_set()
        await updates.put(
            StateUpdate(
                {"appliance_model": "PUSHED-MODEL", "appliance_serial": "pushed-serial"},
                full=True,
            )
        )
        await delivered.wait()
        return {"appliance_model": "ANOTHER-MODEL", "appliance_serial": "read-serial"}

    client.watch = watch
    client.state.side_effect = state
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    registry = dr.async_get(hass)
    device = registry.async_get_device_by_identifier((DOMAIN, "test-fridge"), entry.entry_id)
    assert (device.model, device.serial_number) == ("PUSHED-MODEL", "pushed-serial")
    await updates.put(StateUpdate({"appliance_serial": "later-serial"}, full=False))
    await hass.async_block_till_done()
    assert registry.async_get(device.id).serial_number == "later-serial"
    await updates.put(StateUpdate({"appliance_model": "REPLACED-MODEL"}, full=True))
    await hass.async_block_till_done()
    assert registry.async_get(device.id).serial_number is None


async def test_startup_push_survives_a_pending_status_read_failure(hass, loaded):
    entry, client, _, _, _ = loaded
    updates = asyncio.Queue()
    delivered = asyncio.Event()

    async def watch(device_ids):
        while True:
            yield "test-fridge", await updates.get()
            delivered.set()

    async def state(device_id):
        await updates.put(
            StateUpdate(
                {"appliance_model": "ANOTHER-MODEL", "ref_door_ajar": True, "ref_set_temp": 38},
                full=True,
            )
        )
        await delivered.wait()
        raise ApiError("Status request timed out")

    client.watch = watch
    client.state.side_effect = state
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "38"


async def test_setup_recovers_a_reconnected_appliance_while_another_initial_read_is_pending(
    hass, freezer, loaded, monkeypatch
):
    entry, client, _, _, _ = loaded
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.config_entries.async_update_entry(
        entry,
        options={
            "devices": {
                **entry.data["devices"],
                "test-oven": {"name": "Wall oven", "temperature_unit": "F"},
            },
            "status_poll_interval": 0,
        },
    )
    monkeypatch.setattr("custom_components.subzero.coordinator.random.uniform", lambda *_: 0)
    updates = asyncio.Queue()
    reading, release, reconnected = asyncio.Event(), asyncio.Event(), asyncio.Event()
    connections = 0
    fridge = dict(client.state.return_value)

    async def watch(device_ids):
        nonlocal connections
        connections += 1
        client.push_connected = True
        try:
            if connections == 1:
                yield "test-fridge", StateUpdate({**fridge, "ref_door_ajar": True}, full=True)
            else:
                yield "test-fridge", ChannelOpened()
                yield (
                    "test-fridge",
                    StateUpdate(
                        {"appliance_model": fridge["appliance_model"], "ref_set_temp": 38},
                        full=True,
                    ),
                )
                reconnected.set()
            while True:
                event = await updates.get()
                if isinstance(event, Exception):
                    raise event
                yield event
        finally:
            client.push_connected = False

    async def state(device_id):
        if device_id == "test-oven":
            reading.set()
            await release.wait()
            return {"appliance_model": "SO3050PMSP", "cav_light_on": False}
        return fridge

    client.watch = watch
    client.state.reset_mock()
    client.state.side_effect = state
    setup = asyncio.create_task(hass.config_entries.async_setup(entry.entry_id))
    try:
        await asyncio.wait_for(reading.wait(), 1)
        await updates.put(ApiError("Stream disconnected during setup"))
        await hass.async_block_till_done()
        freezer.tick(31)
        async_fire_time_changed(hass, dt_util.utcnow())
        await asyncio.wait_for(reconnected.wait(), 1)
        # The reconnect snapshot lacked the door, so a read filled it in at once.
        assert [call.args[0] for call in client.state.await_args_list] == [
            "test-oven",
            "test-fridge",
        ]
    finally:
        release.set()
        assert await setup
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    assert [call.args[0] for call in client.state.await_args_list].count("test-fridge") == 1


async def test_startup_status_can_restore_state_despite_a_rejected_channel(hass, loaded):
    entry, client, _, _, _ = loaded
    updates = asyncio.Queue()
    delivered = asyncio.Event()
    closed = asyncio.Event()

    async def watch(device_ids):
        try:
            while True:
                yield "test-fridge", await updates.get()
                delivered.set()
        finally:
            closed.set()

    async def state(device_id):
        await updates.put(ApiError("Channel rejected"))
        await delivered.wait()
        return {"appliance_model": "ANOTHER-MODEL", "ref_door_ajar": False}

    client.watch = watch
    client.state.side_effect = state
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    assert not closed.is_set()


async def test_startup_stream_auth_failure_stops_before_status_reads(hass, loaded):
    entry, client, _, _, _ = loaded

    async def watch(device_ids):
        raise InvalidAuth("Sign in again")
        yield

    client.watch = watch
    client.state.reset_mock()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    client.state.assert_not_awaited()
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1


async def test_partial_push_merges_new_properties(hass, loaded):
    entry, client, updates, _, _ = loaded
    await updates.put(StateUpdate({"ref_door_ajar": True, "frz_set_temp": 0}, full=False))
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "38"
    assert hass.states.get("sensor.kitchen_freezer_setpoint").state == "0"
    await updates.put(StateUpdate({"ref_door_ajar": False}, full=False))
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    client.state.assert_awaited_once()


async def test_normal_door_cycles_do_not_request_status_or_reopen_channels(hass, freezer, loaded):
    entry, client, updates, disconnected, _ = loaded
    client.state.reset_mock()
    for opened in (True, False, True, False):
        await updates.put(StateUpdate({"ref_door_ajar": opened}, full=False))
        await hass.async_block_till_done()
        assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == (
            "on" if opened else "off"
        )
        freezer.tick(61)
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
        client.state.assert_not_awaited()
    assert entry.runtime_data.coordinators["test-fridge"]._followup_task is None
    client.open_channel.assert_not_awaited()
    assert client.watched == [["test-fridge"]]
    assert not disconnected.is_set()


async def test_debug_log_reports_push_updates_without_network_identifiers(hass, loaded, caplog):
    _, _, updates, _, _ = loaded
    caplog.set_level(logging.DEBUG, logger="custom_components.subzero.coordinator")
    await updates.put(
        StateUpdate(
            {
                "ref_door_ajar": True,
                "ipv4_addr": "192.0.2.1",
                "device_wlan_id": "001122334455",
                "ap_ssid": "private-network",
                "version": {"fw": "2.27", "nested": [[[]]]},
            },
            full=False,
        )
    )
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"
    assert "Kitchen update: {'ref_door_ajar': True}" in caplog.text
    for private in ("192.0.2.1", "001122334455", "private-network"):
        assert private not in caplog.text


@pytest.mark.parametrize(
    "pload",
    [
        {"ref_door_ajar": True},
        {"resp": {"ref_door_ajar": True}},
        {"appliance_model": "ANOTHER-MODEL", "ref_door_ajar": True},
        {"resp": {"appliance_model": "ANOTHER-MODEL", "ref_door_ajar": True}},
        {"appliance_model": "SIBLING-MODEL", "props": {"ref_door_ajar": True}},
    ],
)
async def test_cloud_payload_shapes_update_doors_without_losing_other_state(hass, loaded, pload):
    _, _, updates, _, _ = loaded
    envelope = {
        "DeviceId": "test-fridge",
        "Payload": {"api.async_channel": {"type": 2, "pload": pload}},
    }
    event = {"type": 1, "target": "ConnectedApplianceMessage", "arguments": [json.dumps(envelope)]}
    parsed = parse_notification(event, "test-owner", ["test-fridge"])
    await updates.put(parsed)
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "38"


async def test_status_read_drops_missing_properties(hass, loaded):
    entry, client, _, _, _ = loaded
    client.state.return_value = {"appliance_model": "ANOTHER-MODEL", "ref_door_ajar": False}
    await entry.runtime_data.coordinators["test-fridge"].async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "unavailable"
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"


async def test_token_rotation_is_saved_without_password(hass, loaded):
    entry, _, _, _, factory = loaded
    save_tokens = factory.call_args.args[3]
    renewed = {**entry.data["tokens"], "refresh_token": "renewed"}
    await save_tokens(renewed)
    assert entry.data["tokens"] == renewed
    assert "password" not in entry.data


@pytest.mark.parametrize("push", [False, True])
async def test_firmware_updates_device_registry_without_reload(hass, loaded, push):
    entry, client, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    registry = dr.async_get(hass)
    device = registry.async_get_device_by_identifier((DOMAIN, "test-fridge"), entry.entry_id)
    assert device.sw_version == "1.0"
    if push:
        await updates.put(StateUpdate({"version": {"fw": "2.0"}}, full=False))
    else:
        client.state.return_value["version"] = {"fw": "2.0"}
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert registry.async_get(device.id).sw_version == "2.0"
    assert coordinator.device_info["sw_version"] == "2.0"
    await hass.config_entries.async_unload(entry.entry_id)
    coordinator.apply_update(StateUpdate({"version": {"fw": "3.0"}}, full=False))
    assert registry.async_get(device.id).sw_version == "2.0"


async def test_unload_closes_push_listener(hass, loaded):
    entry, _, _, disconnected, _ = loaded
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert disconnected.is_set()


@pytest.mark.parametrize("loaded", ["K", None], indirect=True)
async def test_unknown_units_skip_temperature_entities(hass, loaded):
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint") is None


@pytest.mark.parametrize("options", [False, True])
async def test_reload_refreshes_saved_units(hass, loaded, options):
    entry, client, _, _, _ = loaded
    selected = {"test-fridge": {"name": "Kitchen", "temperature_unit": "F"}}
    if options:
        hass.config_entries.async_update_entry(entry, options={"devices": selected})
    tokens = entry.data["tokens"]
    client.appliances.return_value = [
        Appliance("test-fridge", "Renamed in app", "C"),
        Appliance("test-oven", "Unselected oven", "F"),
    ]
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert client.appliances.await_count == 2
    source = entry.options if options else entry.data
    assert source["devices"] == {"test-fridge": {"name": "Kitchen", "temperature_unit": "C"}}
    assert entry.data["tokens"] == tokens
    if options:
        assert entry.data["devices"] == selected
    assert set(entry.runtime_data.coordinators) == {"test-fridge"}
    assert entry.runtime_data.coordinators["test-fridge"].device == {
        "name": "Kitchen",
        "temperature_unit": "C",
    }
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "38"
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"

    client.appliances.side_effect = ApiError("Temporarily unavailable")
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert client.appliances.await_count == 3
    assert entry.runtime_data.coordinators["test-fridge"].device["temperature_unit"] == "C"
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "38"
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"

    client.appliances.side_effect = None
    client.appliances.return_value = [Appliance("test-fridge", "Kitchen", "F")]
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert client.appliances.await_count == 4
    source = entry.options if options else entry.data
    assert source["devices"] == selected
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "38"


@pytest.mark.parametrize("options", [False, True])
async def test_missing_metadata_preserves_saved_units(hass, loaded, options):
    entry, client, _, _, _ = loaded
    selected = {
        **entry.data["devices"],
        "test-oven": {"name": "Wall oven", "temperature_unit": "F"},
    }
    if options:
        hass.config_entries.async_update_entry(entry, options={"devices": selected})
    else:
        hass.config_entries.async_update_entry(entry, data={**entry.data, "devices": selected})
    client.appliances.return_value = [Appliance("test-oven", "Wall oven", "C")]
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    source = entry.options if options else entry.data
    assert source["devices"] == {
        "test-fridge": {"name": "Kitchen", "temperature_unit": "F"},
        "test-oven": {"name": "Wall oven", "temperature_unit": "C"},
    }
    assert entry.runtime_data.coordinators["test-fridge"].device["temperature_unit"] == "F"
    assert entry.runtime_data.coordinators["test-oven"].device["temperature_unit"] == "C"
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "38"
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"

    client.appliances.side_effect = ApiError("Appliance list unavailable")
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.coordinators["test-fridge"].device["temperature_unit"] == "F"
    assert entry.runtime_data.coordinators["test-oven"].device["temperature_unit"] == "C"
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "38"


async def test_units_are_saved_even_if_status_fails(hass, loaded):
    entry, client, _, _, _ = loaded
    client.appliances.return_value = [Appliance("test-fridge", "Kitchen", "C")]
    client.state.side_effect = ApiError("Appliance unavailable")
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.data["devices"]["test-fridge"]["temperature_unit"] == "C"

    client.appliances.side_effect = ApiError("Appliance list unavailable")
    client.state.side_effect = None
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.coordinators["test-fridge"].device["temperature_unit"] == "C"
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "38"


@pytest.mark.parametrize("loaded", ["F", "C", None], indirect=True)
async def test_appliance_list_outage_uses_cached_units(hass, loaded, caplog):
    entry, client, _, _, _ = loaded
    saved = dict(entry.data)
    unit = entry.runtime_data.coordinators["test-fridge"].device["temperature_unit"]
    client.appliances.side_effect = ApiError("Temporarily unavailable")
    client.state.return_value["air_filter_on"] = False
    client.state.reset_mock()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data == saved
    client.state.assert_awaited_once_with("test-fridge")
    assert entry.runtime_data.coordinators["test-fridge"].device["temperature_unit"] == unit
    temperature = hass.states.get("sensor.kitchen_refrigerator_setpoint")
    if unit in ("F", "C"):
        assert temperature.state == "38"
    else:
        assert temperature is None
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    assert hass.states.get("switch.kitchen_air_purification").state == "off"
    assert "using cached units where available" in caplog.text


async def test_cached_unit_warning_only_when_setup_proceeds(hass, loaded, caplog):
    entry, client, _, _, _ = loaded
    hass.config_entries.async_update_entry(
        entry,
        options={
            "devices": {
                **entry.data["devices"],
                "test-oven": {"name": "Wall oven", "temperature_unit": "F"},
            }
        },
    )
    client.appliances.side_effect = ApiError("Appliance list unavailable")
    recovered = False

    async def state(device_id):
        if not recovered or device_id == "test-oven":
            raise ApiError("Appliance unavailable")
        return client.state.return_value

    client.state.side_effect = state
    for _ in range(2):
        caplog.clear()
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.SETUP_RETRY
        assert "using cached units" not in caplog.text

    recovered = True
    caplog.clear()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.coordinators["test-fridge"].last_update_success
    assert not entry.runtime_data.coordinators["test-oven"].last_update_success
    warnings = [record for record in caplog.records if "using cached units" in record.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].levelname == "WARNING"


@pytest.mark.parametrize("error", [RateLimited(300), InvalidAuth("Expired")])
async def test_metadata_failure_uses_setup_retry_or_reauth(hass, loaded, error):
    entry, client, _, _, _ = loaded
    client.appliances.side_effect = error
    client.state.reset_mock()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    client.state.assert_not_awaited()
    if isinstance(error, InvalidAuth):
        assert entry.state is ConfigEntryState.SETUP_ERROR
        assert (
            hass.config_entries.flow.async_progress_by_handler(DOMAIN)[0]["context"]["source"]
            == "reauth"
        )
    else:
        assert entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.parametrize("error", [ApiError("Disconnected"), RateLimited(300)])
async def test_stream_failure_preserves_state_until_the_grace_expires(hass, freezer, loaded, error):
    entry, client, updates, _, _ = loaded
    await updates.put(error)
    await hass.async_block_till_done()
    assert entry.runtime_data.coordinators["test-fridge"].last_update_success
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    freezer.tick(119)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    freezer.tick(2)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert not entry.runtime_data.coordinators["test-fridge"].last_update_success
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "unavailable"
    client.state.assert_awaited_once()
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("error", "delay"),
    [(ApiError("Disconnected"), 30), (RateLimited(300), 300), (RecursionError("nested"), 30)],
)
async def test_reconnect_waits_then_recovers_from_push_without_polling(
    hass, loaded, monkeypatch, error, delay
):
    entry, client, updates, _, _ = loaded
    monkeypatch.setattr("custom_components.subzero.coordinator.random.uniform", lambda *_: 0)
    hass.config_entries.async_update_entry(entry, pref_disable_polling=True)
    await updates.put(error)
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=delay - 1))
    await hass.async_block_till_done()
    assert client.watched == [["test-fridge"]]
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=delay + 1))
    await hass.async_block_till_done()
    assert client.watched == [["test-fridge"], ["test-fridge"]]
    await updates.put(StateUpdate({**client.state.return_value, "ref_set_temp": 39}, full=True))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "39"
    client.state.assert_awaited_once()


@pytest.mark.parametrize("stream_error", [False, True])
async def test_reopened_channel_refreshes_unavailable_state(
    hass, loaded, monkeypatch, stream_error
):
    entry, client, updates, _, _ = loaded
    monkeypatch.setattr("custom_components.subzero.coordinator.random.uniform", lambda *_: 0)
    error = ApiError("Disconnected")
    await updates.put(error if stream_error else ("test-fridge", error))
    await hass.async_block_till_done()
    if stream_error:
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
        await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == (
        "off" if stream_error else "unavailable"
    )
    client.state.return_value = {
        **client.state.return_value,
        "appliance_model": "ANOTHER-MODEL",
        "ref_door_ajar": True,
        "ref_set_temp": 39,
        "ap_ssid": "private-network",
    }
    await updates.put(ChannelOpened())
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "39"
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    assert "ap_ssid" not in coordinator.data
    assert coordinator.push_stats["snapshots"] == 0
    assert coordinator.push_stats["updates"] == 0
    assert client.state.await_count == 2
    await updates.put(StateUpdate({"ref_door_ajar": False}, full=False))
    await updates.put(ChannelOpened())
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    assert client.state.await_count == 2


async def test_stream_loss_does_not_restore_an_offline_appliance(hass, oven_loaded):
    entry, client, updates = oven_loaded
    await updates.put(("test-oven", ApiError("Appliance offline")))
    await hass.async_block_till_done()
    await updates.put(ApiError("Stream disconnected"))
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    assert hass.states.get("switch.wall_oven_oven_light").state == "unavailable"
    assert entry.runtime_data.coordinators["test-fridge"].last_update_success
    assert not entry.runtime_data.coordinators["test-oven"].last_update_success


@pytest.mark.parametrize("system_option", [False, True])
@pytest.mark.parametrize("recovery", ["read", "snapshot"])
async def test_reconnect_confirms_state_when_periodic_checks_are_disabled(
    hass, freezer, loaded, monkeypatch, system_option, recovery
):
    entry, client, updates, _, _ = loaded
    monkeypatch.setattr("custom_components.subzero.coordinator.random.uniform", lambda *_: 0)
    if system_option:
        hass.config_entries.async_update_entry(entry, pref_disable_polling=True)
    else:
        hass.config_entries.async_update_entry(entry, options={"status_poll_interval": 0})
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    confirmed = coordinator.confirmed_at
    reads = client.state.await_count
    await updates.put(ApiError("Stream disconnected"))
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    freezer.tick(31)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert client.watched == [["test-fridge"], ["test-fridge"]]
    fresh = {**client.state.return_value, "ref_door_ajar": True}
    if recovery == "read":
        client.state.return_value = fresh
        await updates.put(ChannelOpened())
    else:
        await updates.put(StateUpdate(fresh, full=True))
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"
    assert coordinator.confirmed_at > confirmed
    assert client.state.await_count == reads + (recovery == "read")
    freezer.tick(91)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"


async def test_reconnects_and_partial_pushes_do_not_extend_the_grace(
    hass, freezer, loaded, monkeypatch
):
    entry, client, updates, _, _ = loaded
    monkeypatch.setattr("custom_components.subzero.coordinator.random.uniform", lambda *_: 0)
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    confirmed = coordinator.confirmed_at
    complete = dict(client.state.return_value)
    client.state.side_effect = ApiError("Status unavailable")
    await updates.put(ApiError("Stream disconnected"))
    await hass.async_block_till_done()
    freezer.tick(31)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    await updates.put(StateUpdate({"ref_set_temp": 39}, full=False))
    await hass.async_block_till_done()
    assert coordinator.confirmed_at == confirmed
    recovery = entry.runtime_data._recoveries["test-fridge"]
    freezer.tick(30)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    await updates.put(ApiError("Stream disconnected again"))
    await hass.async_block_till_done()
    await asyncio.gather(recovery, return_exceptions=True)
    await hass.async_block_till_done()
    freezer.tick(58)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "39"
    freezer.tick(2)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "unavailable"
    freezer.tick(2)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert client.push_connected
    await updates.put(StateUpdate(complete, full=True))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "38"


async def test_incomplete_snapshot_ends_a_reconnect_wait_and_a_read_fills_what_it_lacked(
    hass, freezer, loaded, monkeypatch
):
    entry, client, updates, _, _ = loaded
    monkeypatch.setattr("custom_components.subzero.coordinator.random.uniform", lambda *_: 0)
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    confirmed = coordinator.confirmed_at
    reading, release = asyncio.Event(), asyncio.Event()

    async def read(_device_id):
        reading.set()
        await release.wait()
        return {**client.state.return_value, "ref_door_ajar": True}

    client.state.side_effect = read
    await updates.put(ApiError("Stream disconnected"))
    await hass.async_block_till_done()
    freezer.tick(31)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert client.push_connected
    await updates.put(
        StateUpdate({"appliance_model": "ANOTHER-MODEL", "ref_set_temp": 40}, full=True)
    )
    await hass.async_block_till_done()
    # The snapshot confirms state and ends the wait with what it carries.
    assert coordinator.confirmed_at > confirmed
    assert coordinator._transport_error is None
    assert coordinator.last_update_success
    assert "ref_door_ajar" in coordinator.missing_state_keys["snapshot"]
    assert coordinator.confirmed_missing_keys == coordinator.missing_state_keys["snapshot"]
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "40"
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "unavailable"
    # A read fills in the keys the snapshot lacked.
    await asyncio.wait_for(reading.wait(), 1)
    release.set()
    await coordinator._read_task
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "38"


async def test_snapshot_ending_a_reconnect_drops_values_only_pushed_as_partials(
    hass, freezer, loaded, monkeypatch
):
    entry, client, updates, _, _ = loaded
    monkeypatch.setattr("custom_components.subzero.coordinator.random.uniform", lambda *_: 0)
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    await updates.put(StateUpdate({"frz_door_ajar": True}, full=False))
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_freezer_door").state == "on"
    await updates.put(ApiError("Stream disconnected"))
    await hass.async_block_till_done()
    freezer.tick(31)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    reads = client.state.await_count
    await updates.put(StateUpdate(dict(client.state.return_value), full=True))
    await hass.async_block_till_done()
    # The snapshot ends the interruption without a read, but a value only a partial
    # update reported may have changed meanwhile, so it does not survive.
    assert client.state.await_count == reads
    assert coordinator.confirmed_at == dt_util.utcnow()
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "38"
    assert hass.states.get("binary_sensor.kitchen_freezer_door").state == "unavailable"
    await updates.put(StateUpdate({"frz_door_ajar": False}, full=False))
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_freezer_door").state == "off"


async def test_reconnected_read_restores_state_without_a_key_it_lacks(
    hass, freezer, loaded, monkeypatch
):
    entry, client, updates, _, _ = loaded
    monkeypatch.setattr("custom_components.subzero.coordinator.random.uniform", lambda *_: 0)
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    confirmed = coordinator.confirmed_at
    await updates.put(StateUpdate({"frz_door_ajar": False}, full=False))
    await updates.put(ApiError("Stream disconnected"))
    await hass.async_block_till_done()
    freezer.tick(31)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    reads = client.state.await_count
    await updates.put(ChannelOpened())
    await hass.async_block_till_done()
    freezer.tick(91)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert client.state.await_count > reads
    # A key only ever pushed as a partial update is not required of a read.
    assert coordinator.missing_state_keys["read"] == []
    assert coordinator.confirmed_at > confirmed
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"


async def test_manual_status_during_a_stream_outage_has_bounded_freshness(hass, freezer, loaded):
    entry, client, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    await updates.put(RateLimited(300))
    await hass.async_block_till_done()
    freezer.tick(91)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert not client.push_connected
    client.state.return_value["ref_set_temp"] = 39
    await coordinator.async_read_status()
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "39"
    freezer.tick(119)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "39"
    freezer.tick(2)
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "unavailable"
    assert client.state.await_count == 2


async def test_failed_status_read_recovers_on_an_existing_push_connection(hass, loaded):
    entry, client, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    client.state.side_effect = [
        ApiError("Status timed out"),
        {"appliance_model": "ANOTHER-MODEL", "ref_door_ajar": True, "ref_set_temp": 39},
    ]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "39"
    assert client.state.await_count == 3
    await updates.put(StateUpdate({"ref_door_ajar": False}, full=False))
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    assert client.state.await_count == 3
    assert client.watched == [["test-fridge"]]
    assert coordinator.push_stats["snapshots"] == 0


@pytest.mark.parametrize("cause", ["push_closed", "channel_error", "auth", "unloading"])
async def test_failed_status_read_leaves_recovery_to_the_connection(hass, loaded, cause):
    entry, client, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    if cause == "push_closed":
        client.push_connected = False
    elif cause == "channel_error":
        await updates.put(("test-fridge", ApiError("Channel closed")))
        await hass.async_block_till_done()
    elif cause == "unloading":
        entry.mock_state(hass, ConfigEntryState.UNLOAD_IN_PROGRESS)
    client.state.side_effect = InvalidAuth("Expired") if cause == "auth" else ApiError("Failed")
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    entry.mock_state(hass, ConfigEntryState.LOADED)
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "unavailable"
    assert client.state.await_count == 2


async def test_offline_channel_waits_for_reopen_before_status_checks(hass, loaded):
    entry, client, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    await updates.put(("test-fridge", ApiError("Appliance offline")))
    await hass.async_block_till_done()
    client.state.reset_mock()
    started = dt_util.utcnow()
    for seconds in (301, 602):
        async_fire_time_changed(hass, started + timedelta(seconds=seconds))
        await hass.async_block_till_done()
    client.state.assert_not_awaited()
    client.open_channel.assert_not_awaited()
    assert not coordinator.last_update_success
    assert not entry.runtime_data._recoveries
    await updates.put(ChannelOpened())
    await hass.async_block_till_done()
    client.state.assert_awaited_once_with("test-fridge")
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"


async def test_failed_setup_read_leaves_recovery_to_setup_retry(hass, loaded):
    entry, client, _, _, _ = loaded
    client.state.side_effect = ApiError("Status timed out")
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert client.state.await_count == 2


async def test_recovery_preserves_incoming_updates_and_other_appliances(hass, oven_loaded):
    entry, client, updates = oven_loaded
    reading = asyncio.Event()
    release = asyncio.Event()

    async def state(device_id):
        assert device_id == "test-oven"
        reading.set()
        await release.wait()
        return {"appliance_model": "SO3050PMSP", "cav_light_on": False}

    client.state.reset_mock(side_effect=True)
    client.state.side_effect = state
    await updates.put(("test-oven", ApiError("Disconnected")))
    await updates.put(("test-oven", ChannelOpened()))
    await hass.async_block_till_done()
    assert reading.is_set()
    await updates.put(("test-oven", ChannelOpened()))
    await updates.put(StateUpdate({"ref_door_ajar": True}, full=False))
    await updates.put(("test-oven", StateUpdate({"cav_light_on": True}, full=False)))
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"
    assert hass.states.get("switch.wall_oven_oven_light").state == "unavailable"
    client.state.assert_awaited_once_with("test-oven")
    release.set()
    await hass.async_block_till_done()
    assert hass.states.get("switch.wall_oven_oven_light").state == "on"
    assert hass.states.get("sensor.wall_oven_oven_temperature").state == "unavailable"
    assert entry.runtime_data.coordinators["test-oven"].push_stats["snapshots"] == 0


async def test_stream_errors_do_not_cancel_shared_reads_or_block_other_appliances(
    hass, oven_loaded
):
    _, client, updates = oven_loaded
    reading, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def state(device_id):
        assert device_id == "test-oven"
        reading.set()
        try:
            await release.wait()
            return {"appliance_model": "SO3050PMSP", "cav_light_on": True}
        except asyncio.CancelledError:
            cancelled.set()
            raise

    client.state.side_effect = state
    await updates.put(("test-oven", ApiError("Disconnected")))
    await updates.put(("test-oven", ChannelOpened()))
    await reading.wait()
    await updates.put(("test-oven", ApiError("Channel lost")))
    await updates.put(StateUpdate({"ref_door_ajar": True}, full=False))
    await hass.async_block_till_done()
    assert not cancelled.is_set()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"
    release.set()
    await hass.async_block_till_done()
    assert hass.states.get("switch.wall_oven_oven_light").state == "on"


@pytest.mark.parametrize(
    ("error", "delay"),
    [(ApiError("Offline"), 30), (RateLimited(300), 300), (ValueError("Unexpected"), 30)],
)
async def test_failed_recovery_retries_with_backoff(hass, loaded, monkeypatch, error, delay):
    _, client, updates, _, _ = loaded
    monkeypatch.setattr("custom_components.subzero.coordinator.random.uniform", lambda *_: 0)
    client.state.reset_mock()
    client.state.side_effect = [
        error,
        {"appliance_model": "ANOTHER-MODEL", "ref_door_ajar": True},
    ]
    await updates.put(("test-fridge", ApiError("Disconnected")))
    await updates.put(ChannelOpened())
    await hass.async_block_till_done()
    client.state.assert_awaited_once()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "unavailable"
    await updates.put(StateUpdate({"ref_door_ajar": False}, full=False))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=delay - 1))
    await hass.async_block_till_done()
    client.state.assert_awaited_once()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=delay + 1))
    await hass.async_block_till_done()
    assert client.state.await_count == 2
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"


@pytest.mark.parametrize("finish", ["snapshot", "disconnect", "channel_error", "unload"])
async def test_shared_recovery_read_survives_stream_changes_and_stops_on_unload(
    hass, loaded, finish
):
    entry, client, updates, _, _ = loaded
    reading, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def state(device_id):
        reading.set()
        try:
            await release.wait()
            return dict(client.state.return_value)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    client.state.side_effect = state
    await updates.put(("test-fridge", ApiError("Disconnected")))
    await updates.put(ChannelOpened())
    await hass.async_block_till_done()
    assert reading.is_set()
    recovery = entry.runtime_data._recoveries["test-fridge"]
    if finish == "unload":
        await hass.config_entries.async_unload(entry.entry_id)
        assert recovery.cancelled()
        assert cancelled.is_set()
        return
    if finish == "snapshot":
        await updates.put(
            StateUpdate({"appliance_model": "ANOTHER-MODEL", "ref_door_ajar": True}, full=True)
        )
    else:
        error = ApiError("Disconnected again")
        await updates.put(error if finish == "disconnect" else ("test-fridge", error))
    await hass.async_block_till_done()
    await asyncio.gather(recovery, return_exceptions=True)
    assert recovery.cancelled()
    assert not cancelled.is_set()
    release.set()
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == (
        "on" if finish == "snapshot" else "unavailable" if finish == "disconnect" else "off"
    )


async def test_recovery_auth_failure_starts_reauthentication(hass, loaded):
    entry, client, updates, _, _ = loaded
    client.state.side_effect = InvalidAuth("Expired")
    await updates.put(("test-fridge", ApiError("Disconnected")))
    await updates.put(ChannelOpened())
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "unavailable"
    context = hass.config_entries.flow.async_progress_by_handler(DOMAIN)[0]["context"]
    assert context["source"] == "reauth"
    assert context["entry_id"] == entry.entry_id
    client.state.reset_mock()
    await updates.put(StateUpdate({"ref_door_ajar": True}, full=False))
    await updates.put(ChannelOpened())
    await hass.async_block_till_done()
    client.state.assert_not_awaited()


@pytest.mark.parametrize("rate_limited", [False, True])
@pytest.mark.parametrize(
    ("lifetimes", "expected"),
    [
        ([1] * 7, [30, 60, 120, 240, 480, 900, 900]),
        ([181] * 3, [30, 30, 30]),
        ([1, 1, 181, 1], [30, 60, 30, 60]),
    ],
)
async def test_reconnect_backoff_uses_lifetime_without_events(
    hass, tokens, monkeypatch, rate_limited, lifetimes, expected
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            "tokens": token_state(tokens),
            "devices": {"test-fridge": {"name": "Kitchen", "temperature_unit": "F"}},
        },
    )
    entry.add_to_hass(hass)
    now = 0
    delays = []

    async def watch(device_ids):
        nonlocal now
        now += lifetimes[len(delays)]
        raise RateLimited(75) if rate_limited else ApiError("Disconnected")
        yield

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == len(lifetimes):
            raise asyncio.CancelledError

    account = SubZeroAccount(hass, entry, SimpleNamespace(watch=watch))
    monkeypatch.setattr(
        "custom_components.subzero.coordinator.time", SimpleNamespace(monotonic=lambda: now)
    )
    monkeypatch.setattr(
        "custom_components.subzero.coordinator.random", SimpleNamespace(uniform=lambda *_: 0)
    )
    monkeypatch.setattr(
        "custom_components.subzero.coordinator.asyncio", SimpleNamespace(sleep=sleep)
    )
    with pytest.raises(asyncio.CancelledError):
        await account.listen()
    assert delays == [max(delay, 75) if rate_limited else delay for delay in expected]


async def test_expired_login_starts_reauthentication(hass, loaded):
    entry, _, updates, _, _ = loaded
    await updates.put(InvalidAuth("Sign in again"))
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == "reauth"
    assert flows[0]["context"]["entry_id"] == entry.entry_id
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "unavailable"


async def test_invalid_stored_tokens_start_reauthentication(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=3,
        data={
            "tokens": {"access_token": None, "refresh_token": "test-refresh"},
            "devices": {"test-fridge": {"name": "Kitchen", "temperature_unit": "F"}},
        },
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == "reauth"
    assert flows[0]["context"]["entry_id"] == entry.entry_id


async def test_configure_adds_appliances_with_the_saved_login(hass, loaded):
    entry, client, updates, _, _ = loaded
    registry = er.async_get(hass)
    original = registry.async_get("binary_sensor.kitchen_refrigerator_door")
    states = {
        "test-fridge": client.state.return_value,
        "test-oven": {"appliance_model": "OTHER-OVEN", "service_required": False, "unit_on": True},
    }
    client.state.side_effect = lambda device_id: states[device_id]
    with patch("custom_components.subzero.config_flow.SubZeroLogin.login") as login:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["data_schema"]({})["device_ids"] == ["test-fridge"]
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"device_ids": ["test-fridge", "test-oven"]}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    login.assert_not_awaited()
    assert client.appliances.await_count == 3
    assert client.watched == [["test-fridge"], ["test-fridge", "test-oven"]]
    assert registry.async_get(original.entity_id).id == original.id
    assert len(dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)) == 2
    await updates.put(("test-oven", StateUpdate({"service_required": True}, full=False)))
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.wall_oven_service_required").state == "on"
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"


async def test_configure_changes_state_checks_without_reloading(hass, freezer, loaded):
    entry, client, _, disconnected, factory = loaded
    account = entry.runtime_data
    coordinator = account.coordinators["test-fridge"]
    entity_id = "sensor.kitchen_refrigerator_setpoint"
    entity = hass.data[SENSOR_COMPONENT].get_entity(entity_id)
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)
    coordinator.async_receive_event(dt_util.utcnow(), 0, 101)
    followup = coordinator._followup_task
    assert followup is not None
    client.state.reset_mock()

    async def configure(interval):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"device_ids": ["test-fridge"], "status_poll_interval": str(interval)},
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        assert entry.runtime_data is account
        assert account.coordinators["test-fridge"] is coordinator
        assert hass.data[SENSOR_COMPONENT].get_entity(entity_id) is entity

    async def advance(seconds):
        freezer.tick(seconds)
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

    await configure(0)
    assert coordinator._followup_task is followup
    assert not followup.cancelling()
    await advance(3)
    client.state.assert_awaited_once_with("test-fridge")
    assert coordinator._followup_task is None
    client.state.reset_mock()
    await advance(601)
    client.state.assert_not_awaited()

    await configure(60)
    await advance(61)
    client.state.assert_awaited_once_with("test-fridge")

    await configure(120)
    await advance(61)
    assert client.state.await_count == 1
    await advance(60)
    assert client.state.await_count == 2

    await configure(0)
    await advance(121)
    assert client.state.await_count == 2
    assert factory.call_count == 1
    assert client.watched == [["test-fridge"]]
    assert not disconnected.is_set()
    assert all(
        event.data["new_state"] is not None and event.data["new_state"].state != STATE_UNAVAILABLE
        for event in changes
        if event.data["entity_id"] == entity_id
    )
    await configure(60)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await advance(61)
    assert client.state.await_count == 2
    assert disconnected.is_set()


async def test_configure_resumes_staggered_checks_without_duplicate_timers(
    hass, freezer, oven_loaded
):
    entry, client, _ = oven_loaded
    account = entry.runtime_data
    coordinators = dict(account.coordinators)
    connections = list(client.watched)
    client.state.reset_mock()

    async def configure(interval):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "device_ids": ["test-fridge", "test-oven"],
                "status_poll_interval": str(interval),
            },
        )
        await hass.async_block_till_done()
        assert entry.runtime_data is account
        assert account.coordinators == coordinators

    async def advance(seconds):
        freezer.tick(seconds)
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

    await configure(0)
    await advance(301)
    client.state.assert_not_awaited()
    await configure(120)
    await advance(59)
    client.state.assert_not_awaited()
    await advance(2)
    client.state.assert_awaited_once_with("test-fridge")
    await advance(61)
    assert client.state.await_args_list == [call("test-fridge"), call("test-oven")]

    await configure(300)
    await advance(61)
    assert client.state.await_count == 2
    await advance(90)
    assert client.state.await_count == 3
    await advance(151)
    assert client.state.await_count == 4
    assert {args.args[0] for args in client.state.await_args_list[2:]} == {
        "test-fridge",
        "test-oven",
    }
    assert client.watched == connections


async def test_configure_changed_selected_units_still_reload(hass, loaded):
    entry, client, _, disconnected, _ = loaded
    account = entry.runtime_data
    client.appliances.return_value = [Appliance("test-fridge", "Kitchen", "C")]
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"device_ids": ["test-fridge"], "status_poll_interval": "300"}
    )
    await hass.async_block_till_done()
    assert entry.runtime_data is not account
    assert entry.runtime_data.coordinators["test-fridge"].device["temperature_unit"] == "C"
    assert entry.options["devices"]["test-fridge"]["temperature_unit"] == "C"
    assert client.watched == [["test-fridge"], ["test-fridge"]]
    assert disconnected.is_set()


async def test_configure_selection_during_setup_applies_after_setup(hass, loaded):
    entry, client, _, _, _ = loaded
    reading, release = asyncio.Event(), asyncio.Event()
    state = dict(client.state.return_value)
    client.state.reset_mock()

    async def read(device_id):
        if client.state.await_count == 1:
            reading.set()
            await release.wait()
        return dict(state)

    client.state.side_effect = read
    pending = asyncio.create_task(hass.config_entries.async_reload(entry.entry_id))
    try:
        await asyncio.wait_for(reading.wait(), 1)
        assert entry.state is ConfigEntryState.SETUP_IN_PROGRESS
        with patch("custom_components.subzero.config_flow.SubZeroClient", return_value=client):
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                {"device_ids": ["test-oven"], "status_poll_interval": "300"},
            )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert set(entry.options["devices"]) == {"test-oven"}
    finally:
        release.set()
        await pending
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert set(entry.options["devices"]) == {"test-oven"}
    assert set(entry.runtime_data.coordinators) == {"test-oven"}
    assert client.watched == [["test-fridge"], ["test-fridge"], ["test-oven"]]
    assert client.state.await_count == 2


@pytest.mark.parametrize(
    ("selected", "unit"),
    [(["test-oven"], "F"), ([], "F"), (["test-fridge"], "C")],
    ids=["swap-appliance", "clear-selection", "change-unit"],
)
async def test_startup_metadata_preserves_newer_configure_options(hass, loaded, selected, unit):
    entry, client, _, _, _ = loaded
    reading, release = asyncio.Event(), asyncio.Event()
    initial = list(client.appliances.return_value)
    refreshed = [Appliance("test-fridge", "Kitchen", unit), initial[1]]
    client.appliances.reset_mock()
    saved = []

    async def capture_options(_hass, updated):
        saved.append(
            {device_id: dict(device) for device_id, device in updated.options["devices"].items()}
        )

    async def appliances():
        if client.appliances.await_count == 1:
            reading.set()
            await release.wait()
            return initial
        return refreshed

    client.appliances.side_effect = appliances
    remove_listener = entry.add_update_listener(capture_options)
    with patch("custom_components.subzero.FcmAlerts") as alerts:
        alerts.return_value.stop = AsyncMock()
        pending = asyncio.create_task(hass.config_entries.async_reload(entry.entry_id))
        try:
            await asyncio.wait_for(reading.wait(), 1)
            assert entry.state is ConfigEntryState.SETUP_IN_PROGRESS
            with patch("custom_components.subzero.config_flow.SubZeroClient", return_value=client):
                result = await hass.config_entries.options.async_init(entry.entry_id)
                result = await hass.config_entries.options.async_configure(
                    result["flow_id"],
                    {
                        "device_ids": selected,
                        "status_poll_interval": "60",
                        "firebase_alerts": True,
                    },
                )
            expected = dict(entry.options["devices"])
            assert set(expected) == set(selected)
        finally:
            release.set()
            await pending
        await hass.async_block_till_done()
        remove_listener()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.state is ConfigEntryState.LOADED
    assert entry.options == {
        "devices": expected,
        "status_poll_interval": 60,
        "firebase_alerts": True,
    }
    assert saved and all(devices == expected for devices in saved)
    assert {
        device_id: coordinator.device
        for device_id, coordinator in entry.runtime_data.coordinators.items()
    } == expected


async def test_configure_retries_failed_setup_with_the_new_selection(hass, loaded):
    entry, client, _, _, _ = loaded
    assert await hass.config_entries.async_unload(entry.entry_id)
    client.state.side_effect = ApiError("Appliance unavailable")
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    client.state.side_effect = None
    ready = asyncio.Event()

    def mark_ready():
        if entry.state is ConfigEntryState.LOADED:
            ready.set()

    remove_listener = entry.async_on_state_change(mark_ready)
    try:
        with patch("custom_components.subzero.config_flow.SubZeroClient", return_value=client):
            result = await hass.config_entries.options.async_init(entry.entry_id)
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"device_ids": ["test-oven"], "status_poll_interval": "300"}
            )
        await asyncio.wait_for(ready.wait(), 1)
    finally:
        remove_listener()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert set(entry.options["devices"]) == {"test-oven"}
    assert set(entry.runtime_data.coordinators) == {"test-oven"}
    assert client.watched == [["test-fridge"], ["test-fridge"], ["test-oven"]]


async def test_deselecting_removes_only_that_appliance(hass, loaded):
    entry, client, _, _, _ = loaded
    first = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        first["flow_id"], {"device_ids": ["test-fridge", "test-oven"]}
    )
    await hass.async_block_till_done()
    registry = dr.async_get(hass)
    original = registry.async_get_device_by_identifier((DOMAIN, "test-fridge"), entry.entry_id)
    second = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        second["flow_id"], {"device_ids": ["test-fridge"]}
    )
    await hass.async_block_till_done()
    assert (
        registry.async_get_device_by_identifier((DOMAIN, "test-fridge"), entry.entry_id).id
        == original.id
    )
    assert registry.async_get_device_by_identifier((DOMAIN, "test-oven"), entry.entry_id) is None
    assert all(
        entity.unique_id.startswith("test-fridge_")
        for entity in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    )
    assert client.appliances.await_count == 5
    assert client.watched[-1] == ["test-fridge"]


async def test_empty_selection_stops_monitoring_but_keeps_account(hass, loaded):
    entry, client, _, _, _ = loaded
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(result["flow_id"], {"device_ids": []})
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.coordinators == {}
    assert entry.data["tokens"]["user_id"] == "test-owner"
    assert client.watched == [["test-fridge"]]
    assert not dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)


@pytest.mark.parametrize("recover_from_read", [False, True])
async def test_one_offline_appliance_does_not_stop_another(hass, loaded, recover_from_read):
    entry, client, updates, _, _ = loaded
    good_state = client.state.return_value

    async def state(device_id):
        if device_id == "test-oven":
            raise ApiError("Appliance unavailable")
        return good_state

    client.state.side_effect = state
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"device_ids": ["test-fridge", "test-oven"]}
    )
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert not entry.runtime_data.coordinators["test-oven"].last_update_success
    await updates.put(("test-oven", ApiError("Channel unavailable")))
    await updates.put(StateUpdate({"ref_door_ajar": True}, full=False))
    await updates.put(("test-oven", StateUpdate({"cav_light_on": True}, full=False)))
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"
    oven = entry.runtime_data.coordinators["test-oven"]
    assert not oven.last_update_success
    assert oven.push_stats["updates"] == 1
    recovered_state = {"appliance_model": "OTHER-OVEN", "service_required": False}
    if recover_from_read:
        client.state.side_effect = None
        client.state.return_value = recovered_state
        await updates.put(("test-oven", ChannelOpened()))
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=36))
    else:
        await updates.put(("test-oven", StateUpdate(recovered_state, full=True)))
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.wall_oven_service_required").state == "off"
    assert oven.push_stats["snapshots"] == (0 if recover_from_read else 1)


async def test_v1_upgrade_enables_integration_disabled_entities(hass, tokens):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Kitchen",
        unique_id="test-fridge",
        data={"device_id": "test-fridge", "temperature_unit": "F", "tokens": token_state(tokens)},
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    night_ice = registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        "test-fridge_night_ice_on",
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
        suggested_object_id="custom_night_ice",
    )
    wifi = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        "test-fridge_ap_rssi",
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.USER,
        suggested_object_id="custom_wifi",
    )
    with (
        patch(
            "custom_components.subzero.api.SubZeroClient.appliances",
            return_value=[Appliance("test-fridge", "Kitchen", "F")],
        ),
        patch(
            "custom_components.subzero.api.SubZeroClient.state",
            return_value={
                "appliance_model": "CL4850UFDID",
                "night_ice_on": True,
                "ap_rssi": -50,
                "sabbath_on": False,
                "high_use_on": False,
                "short_vacation_on": False,
                "long_vacation_on": False,
            },
        ),
        patch(
            "custom_components.subzero.api.SubZeroClient.appliance_faults",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("custom_components.subzero.coordinator.SubZeroAccount.listen"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert registry.async_get(night_ice.entity_id).id == night_ice.id
        assert registry.async_get(night_ice.entity_id).disabled_by is None
        assert hass.states.get(night_ice.entity_id).state == "on"
        assert registry.async_get(wifi.entity_id).disabled_by is er.RegistryEntryDisabler.USER
        modes = [
            entity
            for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
            if entity.entity_id != wifi.entity_id and entity.domain == "binary_sensor"
        ]
        assert all(entity.disabled_by is None for entity in modes)
        await hass.config_entries.async_unload(entry.entry_id)


@pytest.fixture
async def oven_loaded(hass, loaded):
    entry, client, updates, _, _ = loaded
    fridge = client.state.return_value
    oven = {
        "appliance_model": "SO3050PMSP",
        "version": {"fw": "2.27"},
        "cav_temp": 0,
        "cav_set_temp": 0,
        "cav_probe_temp": 0,
        "cav_probe_set_temp": 0,
        "cav_door_ajar": False,
        "cav_unit_on": False,
        "cav_at_set_temp": False,
        "cav_light_on": False,
        "cav_remote_ready": False,
        "cav_probe_on": False,
        "cav_probe_at_set_temp": False,
        "cav_gourmet_mode_on": False,
        "cav_cook_timer_complete": False,
        "kitchen_timer_active": False,
        "kitchen_timer_complete": False,
        "kitchen_timer2_active": False,
        "kitchen_timer2_complete": False,
        "sabbath_on": False,
        "service_required": False,
        "ap_rssi": -60,
        "ap_ssid": "private-network",
        "remote_svc_reg_token": "private-token",
        "cav_unrecognized_property": True,
    }
    client.state.side_effect = lambda device_id: oven if device_id == "test-oven" else fridge
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"device_ids": ["test-fridge", "test-oven"]}
    )
    await hass.async_block_till_done()
    return entry, client, updates


async def test_oven_entities_follow_the_reported_snapshot(hass, oven_loaded):
    entry, _, _ = oven_loaded
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "test-oven"), entry.entry_id
    )
    assert device.manufacturer == "Wolf"
    assert device.model == "SO3050PMSP"
    entities = er.async_entries_for_device(
        er.async_get(hass), device.id, include_disabled_entities=True
    )
    assert len(entities) == 27
    assert {entity.unique_id for entity in entities if entity.disabled_by is not None} == {
        "test-oven_live_reporting_mode",
        "test-oven_state_confirmed_at",
    }
    assert hass.states.get("switch.wall_oven_oven_light").state == "off"
    assert hass.states.get("binary_sensor.wall_oven_cooking").state == "off"
    assert hass.states.get("binary_sensor.wall_oven_kitchen_timer_2_active").state == "off"
    assert hass.states.get("sensor.wall_oven_oven_temperature").state == "unknown"
    assert hass.states.get("sensor.wall_oven_oven_setpoint").state == "unknown"
    assert hass.states.get("sensor.wall_oven_probe_temperature").state == "unknown"
    assert hass.states.get("sensor.wall_oven_probe_setpoint").state == "unknown"
    assert hass.states.get("number.wall_oven_probe_target_temperature").state == "unavailable"
    assert hass.states.get("sensor.wall_oven_refrigerator_setpoint") is None
    data = entry.runtime_data.coordinators["test-oven"].data
    assert "ap_ssid" not in data
    assert "remote_svc_reg_token" not in data
    assert "cav_unrecognized_property" not in data


async def test_oven_push_updates_leave_the_fridge_alone(hass, oven_loaded):
    _, client, updates = oven_loaded
    reads = client.state.await_count
    await updates.put(
        (
            "test-oven",
            StateUpdate(
                {
                    "cav_unit_on": True,
                    "cav_temp": 320,
                    "cav_set_temp": 350,
                    "cav_light_on": True,
                    "cav_door_ajar": True,
                    "cav_probe_on": True,
                    "cav_probe_temp": 125,
                    "cav_probe_set_temp": 145,
                    "kitchen_timer_active": True,
                },
                full=False,
            ),
        )
    )
    await hass.async_block_till_done()
    temperature = hass.states.get("sensor.wall_oven_oven_temperature")
    assert temperature.state == "320"
    assert temperature.attributes["unit_of_measurement"] == "°F"
    assert temperature.attributes["state_class"] == "measurement"
    assert hass.states.get("sensor.wall_oven_oven_setpoint").state == "350"
    assert hass.states.get("sensor.wall_oven_probe_temperature").state == "125"
    assert hass.states.get("sensor.wall_oven_probe_setpoint").state == "145"
    assert hass.states.get("binary_sensor.wall_oven_cooking").state == "on"
    assert hass.states.get("switch.wall_oven_oven_light").state == "on"
    assert hass.states.get("binary_sensor.wall_oven_oven_door").state == "on"
    assert hass.states.get("binary_sensor.wall_oven_kitchen_timer_active").state == "on"
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "38"
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    await updates.put(
        (
            "test-oven",
            StateUpdate({"cav_probe_on": False, "cav_temp": 0, "cav_set_temp": 0}, full=False),
        )
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.wall_oven_oven_temperature").state == "unknown"
    assert hass.states.get("sensor.wall_oven_oven_setpoint").state == "unknown"
    assert hass.states.get("sensor.wall_oven_probe_temperature").state == "unknown"
    assert hass.states.get("sensor.wall_oven_probe_setpoint").state == "unknown"
    assert client.state.await_count == reads


async def test_oven_discovery_recovers_after_an_unavailable_snapshot(hass, oven_loaded):
    entry, client, updates = oven_loaded
    client.state.side_effect = ApiError("Still unavailable")
    registry = er.async_get(hass)
    original = registry.async_get("switch.wall_oven_oven_light")
    await updates.put(("test-oven", ApiError("Temporarily unavailable")))
    await hass.async_block_till_done()
    assert hass.states.get(original.entity_id).state == "unavailable"
    await updates.put(("test-oven", StateUpdate({"cav_light_on": True}, full=False)))
    await hass.async_block_till_done()
    assert hass.states.get(original.entity_id).state == "unavailable"
    await updates.put(
        (
            "test-oven",
            StateUpdate({"appliance_model": "SO3050PMSP", "cav_light_on": True}, full=True),
        )
    )
    await hass.async_block_till_done()
    assert hass.states.get(original.entity_id).state == "on"
    assert registry.async_get(original.entity_id).id == original.id
    assert entry.runtime_data.coordinators["test-oven"].last_update_success
    assert hass.states.get("sensor.wall_oven_oven_temperature").state == "unavailable"


@pytest.mark.parametrize("value", [True, "350", None])
async def test_oven_temperature_rejects_invalid_readings(hass, oven_loaded, value):
    _, _, updates = oven_loaded
    await updates.put(
        ("test-oven", StateUpdate({"cav_unit_on": True, "cav_temp": value}, full=False))
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.wall_oven_oven_temperature").state in {"unknown", "unavailable"}


async def test_push_discovers_all_platforms_for_each_appliance(hass, oven_loaded):
    _, _, updates = oven_loaded
    expected = {
        "climate.wall_oven_lower_oven": "off",
        "sensor.wall_oven_lower_oven_temperature": "unknown",
        "switch.wall_oven_lower_oven_light": "off",
        "select.wall_oven_lower_oven_cooking_mode": "Bake",
        "number.wall_oven_kitchen_timer_duration": "0",
        "button.wall_oven_start_lower_oven": "unavailable",
    }
    assert all(hass.states.get(entity_id) is None for entity_id in expected)
    await updates.put(
        (
            "test-oven",
            StateUpdate(
                {
                    "cav2_set_temp": 350,
                    "cav2_temp": 73,
                    "cav2_unit_on": False,
                    "cav2_light_on": False,
                    "cav2_cook_mode": 1,
                    "cav2_remote_ready": False,
                    "kitchen_timer_end_time": None,
                },
                full=False,
            ),
        )
    )
    await hass.async_block_till_done()
    for entity_id, state in expected.items():
        assert hass.states.get(entity_id).state == state
    registry = er.async_get(hass)
    original = {entity_id: registry.async_get(entity_id).id for entity_id in expected}
    await updates.put(
        ("test-oven", StateUpdate({"cav2_unit_on": True, "cav2_temp": 100}, full=False))
    )
    await updates.put(StateUpdate({"frz_set_temp": 0}, full=False))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.wall_oven_lower_oven_temperature").state == "100"
    assert hass.states.get("climate.kitchen_freezer").state == "cool"
    assert hass.states.get("number.kitchen_freezer_setpoint").state == "0"
    assert hass.states.get("sensor.kitchen_freezer_setpoint").state == "0"
    assert {entity_id: registry.async_get(entity_id).id for entity_id in expected} == original


DISCARDING_FRIDGE = {
    "appliance_model": "BI-36UFD",
    "appliance_type": "1.2.6.1",
    "ref_set_temp": 38,
    "frz_set_temp": 0,
    "ice_maker_on": True,
    "max_ice_on": False,
    "night_ice_on": False,
    "accent_light_level": 100,
    "sabbath_on": False,
}
DISCARDING_WINE = {
    "appliance_model": "DEU2450WDZ",
    "appliance_type": "1.14.1.0",
    "wine_set_temp": 55,
}


@pytest.mark.parametrize("cloud_appliance", [DISCARDING_FRIDGE], indirect=True)
async def test_properties_the_app_discards_are_ignored(hass, cloud_appliance):
    for update, full in (
        (None, False),
        ({"ice_maker_on": False, "accent_light_level": 110}, False),
        (DISCARDING_FRIDGE, True),
    ):
        if update is not None:
            await cloud_appliance.update(update, full=full)
        data = cloud_appliance.coordinator.data
        assert "ice_maker_on" not in data
        assert "accent_light_level" not in data
        assert data["max_ice_on"] is False
    await cloud_appliance.coordinator.async_refresh()
    assert "ice_maker_on" not in cloud_appliance.coordinator.data
    for entity_id in (
        "select.kitchen_accent_light",
        "select.kitchen_ice_maker",
        "binary_sensor.kitchen_ice_maker_enabled",
    ):
        assert hass.states.get(entity_id) is None


@pytest.mark.parametrize(
    "cloud_appliance",
    [{key: value for key, value in DISCARDING_FRIDGE.items() if key != "appliance_type"}],
    indirect=True,
)
async def test_a_reported_type_discards_stored_properties(hass, cloud_appliance):
    assert hass.states.get("select.kitchen_accent_light").state == "On"
    assert hass.states.get("select.kitchen_ice_maker").state == "On"
    assert hass.states.get("binary_sensor.kitchen_max_ice").state == "off"
    await cloud_appliance.update({"appliance_type": "1.2.6.1"})
    assert "accent_light_level" not in cloud_appliance.coordinator.data
    assert hass.states.get("select.kitchen_accent_light").state == "unavailable"
    assert hass.states.get("select.kitchen_ice_maker").state == "unavailable"
    assert hass.states.get("binary_sensor.kitchen_max_ice").state == "unavailable"


@pytest.mark.parametrize("cloud_appliance", [DISCARDING_FRIDGE], indirect=True)
async def test_upgrade_removes_entities_for_discarded_properties(hass, cloud_appliance):
    entry = cloud_appliance.entry
    registry = er.async_get(hass)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stale = [
        registry.async_get_or_create(domain, DOMAIN, f"appliance_{key}", config_entry=entry)
        for domain, key in (
            ("select", "accent_light_level"),
            ("select", "ice_maker_mode"),
            ("binary_sensor", "ice_maker_on"),
            ("binary_sensor", "max_ice_on"),
            ("sensor", "max_ice_end_time"),
        )
    ]
    kept = registry.async_get_entity_id("number", DOMAIN, "appliance_ref_set_temp")
    assert kept is not None
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert all(registry.async_get(entity.entity_id) is None for entity in stale)
    assert registry.async_get(kept) is not None
    assert hass.states.get(kept).state not in ("unavailable", "unknown")


@pytest.mark.parametrize(
    "cloud_appliance",
    [{"appliance_model": "DEC1850CI", "appliance_type": "1.21.2.5", "winterize_on": True}],
    indirect=True,
)
async def test_ice_status_appears_without_reported_ice_power(hass, cloud_appliance):
    assert hass.states.get("sensor.kitchen_ice_maker_status").state == "Off"


@pytest.mark.parametrize("cloud_appliance", [DISCARDING_FRIDGE], indirect=True)
@pytest.mark.parametrize("reported", [{}, {"appliance_type": None}, {"appliance_type": "bad"}])
async def test_a_response_without_a_type_keeps_the_known_type(hass, cloud_appliance, reported):
    coordinator = cloud_appliance.coordinator
    cloud_appliance.state.pop("appliance_type")
    cloud_appliance.state.update(reported)
    await coordinator.async_refresh()
    assert coordinator.data["appliance_type"] == "1.2.6.1"
    assert "ice_maker_on" not in coordinator.data
    await cloud_appliance.update({"accent_light_level": 100, **reported})
    assert coordinator.data["appliance_type"] == "1.2.6.1"
    assert "accent_light_level" not in coordinator.data
    coordinator.async_set_update_error(ApiError("Offline"))
    untyped = {key: value for key, value in DISCARDING_FRIDGE.items() if key != "appliance_type"}
    await cloud_appliance.update({**untyped, **reported}, full=True)
    assert coordinator.last_update_success
    assert coordinator.data["appliance_type"] == "1.2.6.1"
    assert "accent_light_level" not in coordinator.data
    assert hass.states.get("select.kitchen_accent_light") is None


@pytest.mark.parametrize(
    ("cloud_appliance", "removed"),
    [
        ({**DISCARDING_WINE, "high_use_on": False}, True),
        ({**DISCARDING_WINE, "high_use_on": False, "sabbath_on": False}, False),
        ({**DISCARDING_WINE, "high_use_on": False, "sabbath_on": None}, False),
    ],
    indirect=["cloud_appliance"],
)
async def test_upgrade_removes_a_mode_select_left_without_modes(hass, cloud_appliance, removed):
    entry = cloud_appliance.entry
    registry = er.async_get(hass)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    mode = registry.async_get_or_create(
        "select", DOMAIN, "appliance_operating_mode", config_entry=entry
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert (registry.async_get(mode.entity_id) is None) is removed


DISCARDING_OVEN = {
    "appliance_model": "DO3050TE",
    "appliance_type": "1.15.2.4",
    "cav_set_temp": 350,
    "cav2_set_temp": 350,
    "cav2_probe_on": True,
    "cav2_probe_at_set_temp": False,
    "cav2_probe_temp": 120,
    "cav2_probe_set_temp": 160,
}


@pytest.mark.parametrize("cloud_appliance", [DISCARDING_OVEN], indirect=True)
async def test_a_discarded_probe_hides_its_entities(hass, cloud_appliance):
    entry = cloud_appliance.entry
    registry = er.async_get(hass)
    unique_ids = {
        f"appliance_{key}"
        for key in (
            "cav2_probe_on",
            "cav2_probe_at_set_temp",
            "cav2_probe_temp",
            "cav2_probe_set_temp",
        )
    }
    assert not [
        e
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.unique_id in unique_ids
    ]
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stale = [
        registry.async_get_or_create(
            domain, DOMAIN, "appliance_cav2_probe_set_temp", config_entry=entry
        )
        for domain in ("number", "sensor")
    ]
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert all(registry.async_get(entity.entity_id) is None for entity in stale)


@pytest.mark.parametrize(
    "cloud_appliance", [{**DISCARDING_FRIDGE, "appliance_type": "1.2.6.0"}], indirect=True
)
@pytest.mark.parametrize("pushed", [None, "bad", 7])
async def test_an_unusable_pushed_type_does_not_override_a_read(hass, cloud_appliance, pushed):
    coordinator = cloud_appliance.coordinator
    assert coordinator.data["ice_maker_on"] is True
    reading = asyncio.Event()
    release = asyncio.Event()

    async def slow_state(device_id):
        reading.set()
        await release.wait()
        return {**DISCARDING_FRIDGE, "appliance_type": "1.2.6.1"}

    cloud_appliance.client.state.side_effect = slow_state
    refresh = hass.async_create_task(coordinator.async_refresh())
    await reading.wait()
    coordinator.apply_update(StateUpdate({"appliance_type": pushed}, full=False))
    assert coordinator.data["appliance_type"] == "1.2.6.0"
    release.set()
    await refresh
    assert coordinator.data["appliance_type"] == "1.2.6.1"
    assert "ice_maker_on" not in coordinator.data
