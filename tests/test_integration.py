"""Load real HA platforms and exercise entity discovery and push lifecycle."""

import asyncio
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

from custom_components.subzero.api import ApiError, Appliance, RateLimited, StateUpdate, token_state
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


async def test_only_reported_entities_are_created_and_private_fields_discarded(hass, loaded):
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


async def test_push_merges_updates_and_discovers_new_capabilities(hass, loaded):
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


async def test_full_snapshot_removes_missing_property_without_stale_values(hass, loaded):
    _, _, updates, _, _ = loaded
    await updates.put(
        StateUpdate({"appliance_model": "ANOTHER-MODEL", "ref_door_ajar": False}, full=True)
    )
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
async def test_other_unit_accounts_keep_status_without_guessing_temperature_units(hass, loaded):
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint") is None


@pytest.mark.parametrize("options", [False, True])
async def test_reload_saves_units_for_fallback_without_changing_selection(hass, loaded, options):
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


async def test_refreshed_units_are_saved_even_when_appliance_status_fails(hass, loaded):
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
async def test_metadata_outage_loads_appliances_with_cached_or_unknown_units(hass, loaded, caplog):
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
async def test_stream_failure_marks_entities_unavailable_without_immediate_retry(
    hass, loaded, error
):
    entry, client, updates, _, _ = loaded
    await updates.put(error)
    await hass.async_block_till_done()
    assert not entry.runtime_data.coordinators["test-fridge"].last_update_success
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "unavailable"
    client.state.assert_awaited_once()
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("error", "delay"), [(ApiError("Disconnected"), 30), (RateLimited(300), 300)]
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


async def test_configure_adds_devices_with_saved_login_and_one_stream(hass, loaded):
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


async def test_deselection_removes_only_that_device_and_its_entities(hass, loaded):
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


async def test_one_offline_appliance_does_not_stop_another(hass, loaded):
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
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "on"
    await updates.put(
        (
            "test-oven",
            StateUpdate({"appliance_model": "OTHER-OVEN", "service_required": False}, full=True),
        )
    )
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.wall_oven_service_required").state == "off"


async def test_upgrade_enables_old_defaults_and_preserves_user_disabled_entities(hass, tokens):
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


async def test_oven_entities_use_reported_properties_and_correct_manufacturer(hass, oven_loaded):
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


async def test_oven_push_updates_and_idle_readings_do_not_change_fridge(hass, oven_loaded):
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
    entry, _, updates = oven_loaded
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
