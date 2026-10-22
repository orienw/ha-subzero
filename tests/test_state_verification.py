"""Completed observations, freshness and independent state recovery."""

from unittest.mock import Mock

import pytest
from homeassistant.util import dt as dt_util

from custom_components.subzero.api import ApiError, StateUpdate
from tests.test_integration import loaded as loaded

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def test_partial_push_does_not_confirm_unchanged_properties(hass, loaded, freezer):
    entry, _, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    confirmed = coordinator.confirmed_at
    assert confirmed is not None
    freezer.tick(1)
    await updates.put(StateUpdate({"ref_set_temp": 39}, full=False))
    await hass.async_block_till_done()
    assert coordinator.confirmed_at == confirmed


@pytest.mark.parametrize("complete", [False, True])
async def test_failed_read_only_confirms_a_complete_concurrent_snapshot(
    hass, loaded, monkeypatch, freezer, complete
):
    entry, client, updates, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    monkeypatch.setattr("custom_components.subzero.coordinator.SNAPSHOT_TIMEOUT", 0)
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
    renew = Mock(return_value=True)
    await coordinator.async_verify_state(renew)
    if complete:
        assert coordinator.confirmed_at == dt_util.utcnow()
        assert coordinator.verification_stats["last_success"] is not None
        assert hass.states.get("binary_sensor.kitchen_refrigerator_door").state == "off"
    else:
        assert coordinator.confirmed_at == confirmed
        assert coordinator.verification_stats["last_success"] is None
        assert "ref_door_ajar" in coordinator.missing_state_keys["snapshot"]
    renew.assert_not_called()


async def test_incomplete_status_reply_does_not_confirm_old_values(loaded, freezer):
    entry, client, _, _, _ = loaded
    coordinator = entry.runtime_data.coordinators["test-fridge"]
    confirmed = coordinator.confirmed_at
    client.state.return_value = {"appliance_model": "ANOTHER-MODEL", "ref_set_temp": 39}
    freezer.tick(1)
    await coordinator.async_refresh()
    assert coordinator.confirmed_at == confirmed
    assert "ref_door_ajar" in coordinator.missing_state_keys["read"]
