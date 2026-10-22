"""Alert follow-up reads and delayed reminders after missed channel updates."""

import asyncio

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.subzero.api import StateUpdate
from tests.test_integration import loaded as loaded
from tests.test_state_verification import _advance_followup

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def door_alert(when, sequence=0):
    return {"notif_seq": sequence, "notif_type": 101, "timestamp": when.isoformat()}


async def test_an_alert_during_the_last_followup_read_starts_a_new_window(hass, freezer, loaded):
    entry, client, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    client.state.reset_mock()
    client.state.return_value["ref_door_ajar"] = True
    await updates.put(StateUpdate({"ref_door_ajar": True}, full=False))
    await hass.async_block_till_done()
    await _advance_followup(hass, freezer, coordinator, 3)
    await _advance_followup(hass, freezer, coordinator, 61)
    assert client.state.await_count == 2
    reading, release = asyncio.Event(), asyncio.Event()

    async def read(_device_id):
        reading.set()
        await release.wait()
        return dict(client.state.return_value)

    client.state.side_effect = read
    freezer.tick(121)
    async_fire_time_changed(hass, dt_util.utcnow())
    await asyncio.wait_for(reading.wait(), 1)
    await updates.put(
        StateUpdate(
            {"ref_door_ajar": True, "notifs": [door_alert(dt_util.utcnow(), 1)]}, full=False
        )
    )
    await hass.async_block_till_done()
    release.set()
    await coordinator._read_task
    await hass.async_block_till_done()
    assert client.state.await_count == 3
    assert coordinator._followup_task is not None
    client.state.side_effect = None
    client.state.return_value["ref_door_ajar"] = False
    await _advance_followup(hass, freezer, coordinator, 61)
    assert client.state.await_count == 4
    assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    assert coordinator._followup_task is None
