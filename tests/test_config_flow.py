"""Normal HA sign-in, appliance selection and reauthentication."""

from unittest.mock import patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.subzero.api import ApiError, Appliance, RateLimited, token_state
from custom_components.subzero.auth import InvalidAuth, LoginChallenge, LoginError
from custom_components.subzero.const import DOMAIN

from .conftest import make_tokens

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")
CREDENTIALS = {"username": "owner@example.test", "password": "test-only-password"}


@pytest.mark.parametrize(
    ("model", "appliance_type", "unit"),
    [("CL4850UFDID", "17.11.2.3", "F"), ("ANOTHER-MODEL", "99.1.2.3", "C")],
)
async def test_create_entry_without_model_or_family_allowlist(
    hass, tokens, model, appliance_type, unit
):
    appliance = Appliance("test-fridge", "Kitchen", unit, appliance_type)
    with (
        patch("custom_components.subzero.config_flow.SubZeroLogin.login", return_value=tokens),
        patch("custom_components.subzero.api.SubZeroClient.appliances", return_value=[appliance]),
        patch(
            "custom_components.subzero.api.SubZeroClient.state",
            return_value={"appliance_model": model},
        ),
        patch("custom_components.subzero.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        assert result["step_id"] == "device"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"device_id": "test-fridge"}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == model
    assert result["data"] == {
        "device_id": "test-fridge",
        "temperature_unit": unit,
        "tokens": token_state(tokens),
    }
    assert "password" not in result["data"]
    assert result["result"].unique_id == "test-fridge"


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (InvalidAuth("Rejected"), "invalid_auth"),
        (LoginChallenge("Verification needed"), "verification_required"),
        (LoginError("Unavailable"), "cannot_connect"),
        (ApiError("Unavailable"), "cannot_connect"),
        (RateLimited(300), "rate_limited"),
    ],
)
async def test_login_failure_keeps_the_form(hass, error, message):
    with patch("custom_components.subzero.config_flow.SubZeroLogin.login", side_effect=error):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}, data=CREDENTIALS
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": message}


async def test_no_appliances(hass, tokens):
    with (
        patch("custom_components.subzero.config_flow.SubZeroLogin.login", return_value=tokens),
        patch("custom_components.subzero.api.SubZeroClient.appliances", return_value=[]),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}, data=CREDENTIALS
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_appliances"


async def test_already_added_appliance_does_not_read_it_again(hass, tokens):
    MockConfigEntry(domain=DOMAIN, unique_id="test-fridge", data={}).add_to_hass(hass)
    with (
        patch("custom_components.subzero.config_flow.SubZeroLogin.login", return_value=tokens),
        patch(
            "custom_components.subzero.api.SubZeroClient.appliances",
            return_value=[Appliance("test-fridge", "Kitchen", "F", "99")],
        ),
        patch("custom_components.subzero.api.SubZeroClient.state") as state,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}, data=CREDENTIALS
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"device_id": "test-fridge"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    state.assert_not_awaited()


@pytest.mark.parametrize("wrong_account", [False, True])
async def test_reauthentication_preserves_account_identity(hass, tokens, wrong_account):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-fridge",
        data={"device_id": "test-fridge", "temperature_unit": "F", "tokens": token_state(tokens)},
    )
    entry.add_to_hass(hass)
    renewed = make_tokens(
        "someone-else" if wrong_account else "test-owner", refresh_token="renewed"
    )
    with (
        patch("custom_components.subzero.config_flow.SubZeroLogin.login", return_value=renewed),
        patch(
            "custom_components.subzero.api.SubZeroClient.appliances",
            return_value=[Appliance("test-fridge", "Kitchen", "F", "99")],
        ),
        patch(
            "custom_components.subzero.api.SubZeroClient.state",
            return_value={"appliance_model": "OTHER"},
        ),
        patch("custom_components.subzero.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data=entry.data
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == ("wrong_account" if wrong_account else "reauth_successful")
    expected = tokens if wrong_account else renewed
    assert entry.data["tokens"] == token_state(expected)
