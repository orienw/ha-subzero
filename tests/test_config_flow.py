"""Account setup, appliance selection and reauthentication through HA."""

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
DEVICES = {
    "test-fridge": {"name": "Kitchen", "temperature_unit": "F"},
    "test-oven": {"name": "Wall oven", "temperature_unit": "C"},
}
APPLIANCES = [
    Appliance("test-fridge", "Kitchen", "F"),
    Appliance("test-oven", "Wall oven", "C"),
]


@pytest.mark.parametrize("selected", [["test-fridge"], ["test-fridge", "test-oven"]])
async def test_create_account_with_multiple_appliances_and_no_family_allowlist(
    hass, tokens, selected
):
    with (
        patch("custom_components.subzero.config_flow.SubZeroLogin.login", return_value=tokens),
        patch("custom_components.subzero.api.SubZeroClient.appliances", return_value=APPLIANCES),
        patch("custom_components.subzero.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        assert result["step_id"] == "device"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"device_ids": selected}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == CREDENTIALS["username"]
    assert result["data"] == {
        "devices": {key: DEVICES[key] for key in selected},
        "tokens": token_state(tokens),
    }
    assert "password" not in result["data"]
    assert result["result"].unique_id == "test-owner"
    assert result["result"].version == 2


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


@pytest.mark.parametrize("legacy", [True, False])
async def test_existing_account_uses_configure_instead_of_duplicate_setup(hass, tokens, legacy):
    data = {"tokens": token_state(tokens)}
    data.update({"device_id": "test-fridge"} if legacy else {"devices": DEVICES})
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-fridge" if legacy else "test-owner",
        version=1 if legacy else 2,
        data=data,
    ).add_to_hass(hass)
    with (
        patch("custom_components.subzero.config_flow.SubZeroLogin.login", return_value=tokens),
        patch("custom_components.subzero.api.SubZeroClient.appliances") as appliances,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}, data=CREDENTIALS
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    appliances.assert_not_awaited()


@pytest.mark.parametrize("wrong_account", [False, True])
async def test_reauthentication_preserves_account_and_appliance_selection(
    hass, tokens, wrong_account
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-owner",
        version=2,
        data={"devices": DEVICES, "tokens": token_state(tokens)},
        options={"devices": {"test-oven": DEVICES["test-oven"]}},
    )
    entry.add_to_hass(hass)
    renewed = make_tokens(
        "someone-else" if wrong_account else "test-owner", refresh_token="renewed"
    )
    with (
        patch("custom_components.subzero.config_flow.SubZeroLogin.login", return_value=renewed),
        patch("custom_components.subzero.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data=entry.data
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == ("wrong_account" if wrong_account else "reauth_successful")
    assert entry.data["tokens"] == token_state(tokens if wrong_account else renewed)
    assert entry.options["devices"] == {"test-oven": DEVICES["test-oven"]}


@pytest.mark.parametrize("error", [ApiError("Unavailable"), RateLimited(300)])
async def test_options_list_failure_can_be_retried_without_changing_selection(hass, tokens, error):
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, data={"tokens": token_state(tokens), "devices": DEVICES}
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.subzero.api.SubZeroClient.appliances", side_effect=[error, APPLIANCES]
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        assert result["errors"]
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert not result["errors"]
    assert result["data_schema"]({})["device_ids"] == list(DEVICES)
    assert entry.options == {}


async def test_options_expired_tokens_start_reauth(hass, tokens):
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, data={"tokens": token_state(tokens), "devices": DEVICES}
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.subzero.api.SubZeroClient.appliances", side_effect=InvalidAuth("Expired")
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_required"
    assert (
        hass.config_entries.flow.async_progress_by_handler(DOMAIN)[0]["context"]["source"]
        == "reauth"
    )


async def test_options_preserve_temporarily_missing_appliances_and_exclude_other_entries(
    hass, tokens
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Kitchen",
        version=2,
        data={"tokens": token_state(tokens), "devices": {"test-fridge": DEVICES["test-fridge"]}},
    )
    entry.add_to_hass(hass)
    MockConfigEntry(
        domain=DOMAIN,
        title="Legacy oven",
        data={"tokens": token_state(tokens), "device_id": "test-oven", "temperature_unit": "F"},
    ).add_to_hass(hass)
    with patch(
        "custom_components.subzero.api.SubZeroClient.appliances", return_value=[APPLIANCES[1]]
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"]
    assert schema({})["device_ids"] == ["test-fridge"]
    selector = next(iter(schema.schema.values()))
    assert selector.config["options"] == [{"value": "test-fridge", "label": "Kitchen"}]
