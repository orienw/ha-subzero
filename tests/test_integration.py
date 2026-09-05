"""Load real HA platforms and exercise entity discovery and push lifecycle."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.subzero.api import ApiError, Appliance, RateLimited, StateUpdate, token_state
from custom_components.subzero.auth import InvalidAuth
from custom_components.subzero.const import DOMAIN

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
                Appliance("test-fridge", "Kitchen", "F", "17.11"),
                Appliance("test-oven", "Wall oven", "F", "17.15"),
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
    }
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint").state == "38"
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    assert "ap_ssid" not in coordinator.data
    assert "appliance_serial" not in coordinator.data
    assert coordinator.update_interval.total_seconds() == 1800
    assert entry.version == 2
    assert entry.unique_id == "test-fridge"
    assert entry.data["devices"] == {"test-fridge": {"name": "Kitchen", "temperature_unit": "F"}}
    client.state.assert_awaited_once()


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


async def test_unload_closes_push_listener(hass, loaded):
    entry, _, _, disconnected, _ = loaded
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert disconnected.is_set()


@pytest.mark.parametrize("loaded", ["C", None], indirect=True)
async def test_other_unit_accounts_keep_status_without_guessing_temperature_units(hass, loaded):
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    assert hass.states.get("sensor.kitchen_refrigerator_setpoint") is None


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
    client.appliances.assert_awaited_once()
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
    assert client.appliances.await_count == 2
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
            if entity.entity_id != wifi.entity_id
        ]
        assert all(entity.disabled_by is None for entity in modes)
        await hass.config_entries.async_unload(entry.entry_id)
