"""Load real HA platforms and exercise entity discovery and push lifecycle."""

import asyncio
import json
import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

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
    )
    entry.add_to_hass(hass)
    updates = asyncio.Queue()
    connected = asyncio.Event()
    disconnected = asyncio.Event()

    async def watch(device_ids):
        assert isinstance(device_ids, list)
        client.watched.append(device_ids)
        connected.set()
        try:
            while True:
                event = await updates.get()
                if isinstance(event, Exception):
                    raise event
                yield event if isinstance(event, tuple) else ("test-fridge", event)
        finally:
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
        "test-fridge_ref_set_temp",
        "test-fridge_ref_door_ajar",
        "test-fridge_live_reporting_mode",
    }
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "38"
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    assert "ap_ssid" not in coordinator.data
    assert "appliance_serial" not in coordinator.data
    assert coordinator.update_interval is None
    assert entry.version == 2
    assert entry.unique_id == "test-fridge"
    assert entry.data["devices"] == {"test-fridge": {"name": "Kitchen", "temperature_unit": "F"}}
    client.state.assert_awaited_once()


async def test_idle_push_connection_does_not_request_periodic_status(hass, loaded):
    _, client, _, _, _ = loaded
    for hours in (1, 12, 24):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=hours))
        await hass.async_block_till_done()
    client.state.assert_awaited_once()
    client.open_channel.assert_not_awaited()
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "38"


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


async def test_startup_status_does_not_hide_a_rejected_channel(hass, loaded):
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
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "unavailable"
    assert closed.is_set()


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


@pytest.mark.parametrize("loaded", ["C", None], indirect=True)
async def test_celsius_or_unknown_units_skip_temperature_entities(hass, loaded):
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
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "unavailable"
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"

    client.appliances.side_effect = ApiError("Temporarily unavailable")
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert client.appliances.await_count == 3
    assert entry.runtime_data.coordinators["test-fridge"].device["temperature_unit"] == "C"
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "unavailable"
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
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "unavailable"


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
    if unit == "F":
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


@pytest.mark.parametrize("error", [ApiError("offline"), RateLimited(300)])
async def test_stream_failure_marks_entities_unavailable(hass, loaded, error):
    entry, client, updates, _, _ = loaded
    await updates.put(error)
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
    _, client, updates, _, _ = loaded
    monkeypatch.setattr("custom_components.subzero.coordinator.random.uniform", lambda *_: 0)
    await updates.put(error)
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=delay - 1))
    await hass.async_block_till_done()
    assert client.watched == [["test-fridge"]]
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=delay + 1))
    await hass.async_block_till_done()
    assert client.watched == [["test-fridge"], ["test-fridge"]]
    await updates.put(
        StateUpdate({"appliance_model": "ANOTHER-MODEL", "ref_set_temp": 39}, full=True)
    )
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
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "unavailable"
    client.state.return_value = {
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
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=1))
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    assert client.state.await_count == 2


async def test_failed_status_read_recovers_on_an_existing_push_connection(hass, loaded):
    entry, client, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    client.state.side_effect = [
        ApiError("Status timed out"),
        {"appliance_model": "ANOTHER-MODEL", "ref_door_ajar": True, "ref_set_temp": 39},
    ]
    await coordinator.async_refresh()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "unavailable"
    await updates.put(StateUpdate({"ref_door_ajar": True}, full=False))
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "39"
    assert client.state.await_count == 3
    assert client.watched == [["test-fridge"]]
    assert coordinator.push_stats["snapshots"] == 0


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


async def test_canceling_one_recovery_does_not_block_other_appliance_updates(hass, oven_loaded):
    _, client, updates = oven_loaded
    reading = asyncio.Event()
    canceling = asyncio.Event()
    cleanup = asyncio.Event()

    async def state(device_id):
        assert device_id == "test-oven"
        reading.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            canceling.set()
            await cleanup.wait()
            raise

    client.state.side_effect = state
    await updates.put(("test-oven", ApiError("Disconnected")))
    await updates.put(("test-oven", ChannelOpened()))
    await reading.wait()
    try:
        await updates.put(("test-oven", ApiError("Channel lost")))
        await canceling.wait()
        await updates.put(StateUpdate({"ref_door_ajar": True}, full=False))
        await hass.async_block_till_done()
        assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"
        assert hass.states.get("switch.wall_oven_oven_light").state == "unavailable"
    finally:
        cleanup.set()
        await hass.async_block_till_done()


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
async def test_recovery_cannot_overwrite_newer_state_or_outlive_connection(hass, loaded, finish):
    entry, client, updates, _, _ = loaded
    reading = asyncio.Event()
    cancelled = asyncio.Event()

    async def state(device_id):
        reading.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    client.state.side_effect = state
    await updates.put(("test-fridge", ApiError("Disconnected")))
    await updates.put(ChannelOpened())
    await hass.async_block_till_done()
    assert reading.is_set()
    if finish == "unload":
        await hass.config_entries.async_unload(entry.entry_id)
    elif finish == "snapshot":
        await updates.put(
            StateUpdate({"appliance_model": "ANOTHER-MODEL", "ref_door_ajar": True}, full=True)
        )
    else:
        error = ApiError("Disconnected again")
        await updates.put(error if finish == "disconnect" else ("test-fridge", error))
    async with asyncio.timeout(1):
        await cancelled.wait()
    await hass.async_block_till_done()
    assert cancelled.is_set()
    if finish != "unload":
        assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == (
            "on" if finish == "snapshot" else "unavailable"
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
    assert len(entities) == 23
    assert {entity.unique_id for entity in entities if entity.disabled_by is not None} == {
        "test-oven_live_reporting_mode",
    }
    assert hass.states.get("switch.wall_oven_oven_light").state == "off"
    assert hass.states.get("binary_sensor.wall_oven_cooking").state == "off"
    assert hass.states.get("binary_sensor.wall_oven_kitchen_timer_2_active").state == "off"
    assert hass.states.get("sensor.wall_oven_oven_temperature").state == "unknown"
    assert hass.states.get("sensor.wall_oven_oven_setpoint").state == "unknown"
    assert hass.states.get("sensor.wall_oven_probe_temperature").state == "unknown"
    assert hass.states.get("sensor.wall_oven_probe_setpoint").state == "unknown"
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
    await updates.put(("test-oven", StateUpdate({"cav_temp": value}, full=False)))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.wall_oven_oven_temperature").state in {"unknown", "unavailable"}


async def test_push_discovers_all_platforms_for_each_appliance(hass, oven_loaded):
    _, _, updates = oven_loaded
    expected = {
        "climate.wall_oven_lower_oven": "off",
        "sensor.wall_oven_lower_oven_temperature": "73",
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
    await updates.put(("test-oven", StateUpdate({"cav2_temp": 100}, full=False)))
    await updates.put(StateUpdate({"frz_set_temp": 0}, full=False))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.wall_oven_lower_oven_temperature").state == "100"
    assert hass.states.get("climate.kitchen_freezer").state == "cool"
    assert hass.states.get("number.kitchen_freezer_setpoint").state == "0"
    assert hass.states.get("sensor.kitchen_freezer_setpoint").state == "0"
    assert {entity_id: registry.async_get(entity_id).id for entity_id in expected} == original
