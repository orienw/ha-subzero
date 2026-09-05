"""Load real HA platforms and exercise entity discovery and push lifecycle."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.subzero.api import ApiError, RateLimited, StateUpdate, token_state
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

    async def watch(device_id):
        assert device_id == "test-fridge"
        connected.set()
        try:
            while True:
                event = await updates.get()
                if isinstance(event, Exception):
                    raise event
                yield event
        finally:
            disconnected.set()

    with patch("custom_components.subzero.SubZeroClient") as factory:
        client = factory.return_value
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
    assert "ap_ssid" not in entry.runtime_data.data
    assert "appliance_serial" not in entry.runtime_data.data
    assert entry.runtime_data.update_interval.total_seconds() == 1800
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
    assert not entry.runtime_data.last_update_success
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
