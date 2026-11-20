"""Synthetic fixtures; tests never use the owner's saved account data."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.subzero.api import Appliance, StateUpdate, token_state
from custom_components.subzero.const import DOMAIN
from tests.common import MockConfigEntry


def make_tokens(user_id="test-owner", *, expires_in=3600, refresh_token="test-refresh"):
    return {
        **{
            name: jwt.encode(
                {"mergedId": user_id, "exp": int(time.time()) + expires_in, "token_use": name},
                "synthetic-key-for-tests-only-32-bytes",
                algorithm="HS256",
            )
            for name in ("access_token", "id_token")
        },
        "refresh_token": refresh_token,
    }


@pytest.fixture
def tokens():
    return make_tokens()


@pytest.fixture(autouse=True)
def fast_timeouts(monkeypatch):
    monkeypatch.setattr("custom_components.subzero.account.INITIAL_STATE_TIMEOUT", 0)
    monkeypatch.setattr("custom_components.subzero.coordinator.CONTROL_CONFIRM_TIMEOUT", 0.05)
    monkeypatch.setattr("custom_components.subzero.coordinator.CONTROL_PUSH_TIMEOUT", 0.01)


@pytest.fixture
async def cloud_appliance(hass, tokens, request, enable_custom_integrations):
    state = dict(request.param)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=3,
        title="Account",
        data={
            "tokens": token_state(tokens),
            "devices": {"appliance": {"name": "Kitchen", "temperature_unit": "F"}},
        },
        # Timing tests use five-minute checks explicitly.
        options={"status_poll_interval": 300},
    )
    entry.add_to_hass(hass)
    updates = asyncio.Queue()

    async def watch(device_ids):
        while True:
            yield await updates.get()

    async def update(properties, *, full=False):
        if full:
            state.clear()
        state.update(properties)
        await updates.put(("appliance", StateUpdate(dict(properties), full=full)))
        await hass.async_block_till_done()

    async def write(device_id, key, value):
        state[key] = value
        await updates.put((device_id, StateUpdate({key: value}, full=False)))

    async def delay(device_id, duration, start_offset, recurring):
        state["delay_duration"] = duration
        if duration:
            state.update(delay_start_offset=start_offset, delay_recurring=recurring)

    async def exit_delay(device_id):
        state["delay_active"] = False
        return dict(state)

    with patch("custom_components.subzero.SubZeroClient") as factory:
        client = factory.return_value
        client.tokens = token_state(tokens)
        client.push_connected = True
        client.open_channel = AsyncMock()
        client.appliances = AsyncMock(return_value=[Appliance("appliance", "Kitchen", "F")])
        client.state = AsyncMock(side_effect=lambda device_id: dict(state))
        client.set_property = AsyncMock(side_effect=write)
        client.set_ice_delay = AsyncMock(side_effect=delay)
        client.exit_ice_delay = AsyncMock(side_effect=exit_delay)
        client.appliance_faults = AsyncMock(return_value=[])
        client.watch = watch
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        device = next(
            device
            for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
            if (DOMAIN, "appliance") in device.identifiers
        )
        yield SimpleNamespace(
            entry=entry,
            client=client,
            state=state,
            update=update,
            coordinator=entry.runtime_data.coordinators["appliance"],
            device_id=device.id,
        )
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


@pytest.fixture
async def loaded(hass, tokens, request, enable_custom_integrations):
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
