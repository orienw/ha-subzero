"""Account setup, appliance selection and reauthentication through HA."""

import asyncio
from contextlib import suppress
from unittest.mock import patch

import aiohttp
import pytest
from homeassistant.data_entry_flow import FlowResultType, UnknownFlow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.subzero.api import ApiError, Appliance, RateLimited, token_state
from custom_components.subzero.auth import (
    InvalidAuth,
    InvalidCaptcha,
    InvalidVerificationCode,
    LoginChallenge,
    LoginError,
    LoginRateLimited,
    MfaCallTimeout,
)
from custom_components.subzero.const import DOMAIN

from .conftest import make_tokens

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")
CREDENTIALS = {"username": "owner@example.test", "password": "test-only-password"}
CAPTCHA_IMAGE = "data:image/png;base64,iVBORw0KGgo="
DEVICES = {
    "test-fridge": {"name": "Kitchen", "temperature_unit": "F"},
    "test-oven": {"name": "Wall oven", "temperature_unit": "C"},
}
APPLIANCES = [
    Appliance("test-fridge", "Kitchen", "F"),
    Appliance("test-oven", "Wall oven", "C"),
]


def _phonefactor_login(*, captcha_required: bool = True):
    async def _login(self, username, password):
        self.settings = {"api": "Phonefactor"}
        self.phone_numbers = [{"Id": 1, "MaskedNumber": "XXX-XXX-4550"}]
        self.captcha_required = captcha_required
        self.captcha_solved = not captcha_required
        raise LoginChallenge("MFA required")

    return _login


def _captcha_loader(*images: str):
    iterator = iter(images)

    async def _get_captcha(self):
        self.captcha_image = next(iterator, CAPTCHA_IMAGE)
        return self.captcha_image

    return _get_captcha


@pytest.mark.parametrize("selected", [["test-fridge"], ["test-fridge", "test-oven"]])
async def test_setup_creates_entry_with_selected_appliances(hass, tokens, selected):
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
        "username": CREDENTIALS["username"],
    }
    assert "password" not in result["data"]
    assert result["result"].unique_id == "test-owner"
    assert result["result"].version == 2
    assert result["result"].minor_version == 3


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


async def test_mfa_sms_login_and_wrong_code(hass, tokens):
    with (
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.login",
            autospec=True,
            side_effect=_phonefactor_login(),
        ),
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.get_captcha_challenge",
            autospec=True,
            side_effect=_captcha_loader(),
        ),
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.request_mfa_verification",
            return_value=None,
        ) as request_mfa,
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.verify_mfa_code",
            side_effect=[
                InvalidVerificationCode("Wrong code"),
                MfaCallTimeout("Expired"),
                tokens,
            ],
        ),
        patch("custom_components.subzero.api.SubZeroClient.appliances", return_value=APPLIANCES),
        patch("custom_components.subzero.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}, data=CREDENTIALS
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "mfa_challenge"
        assert [field.schema for field in result["data_schema"].schema] == ["captcha", "method"]
        assert result["description_placeholders"] == {
            "masked_phone": "XXX-XXX-4550",
            "captcha_image": f"\n\n![CAPTCHA]({CAPTCHA_IMAGE})",
        }

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"captcha": "ABCD", "method": "onewaysms"}
        )
        request_mfa.assert_awaited_once_with(auth_type="onewaysms", captcha_code="ABCD")
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "mfa_code"
        assert result["description_placeholders"] == {"masked_phone": "XXX-XXX-4550"}

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": "000000"}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "mfa_code"
        assert result["errors"] == {"base": "invalid_mfa_code"}

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": "000000"}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "mfa_challenge"
        assert result["errors"] == {"base": "mfa_code_expired"}
        assert [field.schema for field in result["data_schema"].schema] == ["captcha", "method"]
        assert result["description_placeholders"]["captcha_image"] == (
            f"\n\n![CAPTCHA]({CAPTCHA_IMAGE})"
        )

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"captcha": "ABCD", "method": "onewaysms"}
        )
        assert result["step_id"] == "mfa_code"

        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": ""})
        assert result["step_id"] == "mfa_challenge"
        assert result["errors"] == {}
        assert [field.schema for field in result["data_schema"].schema] == ["captcha", "method"]

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"captcha": "ABCD", "method": "onewaysms"}
        )
        assert result["step_id"] == "mfa_code"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": "123456"}
        )
        assert result["step_id"] == "device"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"device_ids": ["test-fridge"]}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_mfa_wrong_captcha_and_solved_captcha_hides_image(hass):
    new_captcha = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="

    async def request_mfa(self, auth_type="onewaysms", captcha_code="", phone_id=None):
        if captcha_code == "WRONG":
            raise InvalidCaptcha("Wrong answer")
        self.captcha_solved = True
        raise LoginRateLimited("Too many requests")

    with (
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.login",
            autospec=True,
            side_effect=_phonefactor_login(),
        ),
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.get_captcha_challenge",
            autospec=True,
            side_effect=_captcha_loader(CAPTCHA_IMAGE, new_captcha),
        ),
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.request_mfa_verification",
            autospec=True,
            side_effect=request_mfa,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}, data=CREDENTIALS
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"captcha": "WRONG", "method": "onewaysms"}
        )
        assert result["step_id"] == "mfa_challenge"
        assert result["errors"] == {"base": "invalid_captcha"}
        assert [field.schema for field in result["data_schema"].schema] == ["captcha", "method"]
        assert result["description_placeholders"]["captcha_image"] == (
            f"\n\n![CAPTCHA]({new_captcha})"
        )

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"captcha": "ABCD", "method": "onewaysms"}
        )
        assert result["step_id"] == "mfa_challenge"
        assert result["errors"] == {"base": "rate_limited"}
        assert [field.schema for field in result["data_schema"].schema] == ["method"]
        assert result["description_placeholders"]["captcha_image"] == ""


async def test_mfa_phone_call_progress_and_timeout(hass, tokens):
    call_attempt = 0

    async def poll_call(self):
        nonlocal call_attempt
        call_attempt += 1
        await asyncio.sleep(0)
        if call_attempt == 1:
            raise MfaCallTimeout("Timed out")
        return tokens

    with (
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.login",
            autospec=True,
            side_effect=_phonefactor_login(captcha_required=False),
        ),
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.request_mfa_verification",
            return_value=None,
        ),
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.poll_mfa_call",
            autospec=True,
            side_effect=poll_call,
        ),
        patch("custom_components.subzero.api.SubZeroClient.appliances", return_value=APPLIANCES),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}, data=CREDENTIALS
        )
        assert result["step_id"] == "mfa_challenge"
        assert [field.schema for field in result["data_schema"].schema] == ["method"]
        assert result["description_placeholders"]["captcha_image"] == ""

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"method": "dialphone"}
        )
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        assert result["step_id"] == "mfa_call"
        assert result["progress_action"] == "wait_for_call"
        await hass.async_block_till_done()

        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "mfa_challenge"
        assert result["errors"] == {"base": "mfa_call_failed"}

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"method": "dialphone"}
        )
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        await hass.async_block_till_done()

        result = await hass.config_entries.flow.async_configure(result["flow_id"])
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "device"


async def test_mfa_call_finish_reached_twice_shares_one_sign_in(hass, tokens):
    release = asyncio.Event()
    lookups = 0

    async def slow_appliances(self):
        nonlocal lookups
        lookups += 1
        await release.wait()
        return APPLIANCES

    with (
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.login",
            autospec=True,
            side_effect=_phonefactor_login(captcha_required=False),
        ),
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.request_mfa_verification",
            return_value=None,
        ),
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.poll_mfa_call",
            return_value=tokens,
        ),
        patch(
            "custom_components.subzero.api.SubZeroClient.appliances",
            autospec=True,
            side_effect=slow_appliances,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}, data=CREDENTIALS
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"method": "dialphone"}
        )
        await hass.async_block_till_done()

        # The frontend and the call task's callback can both advance the flow to mfa_finish.
        first = asyncio.ensure_future(hass.config_entries.flow.async_configure(result["flow_id"]))
        second = asyncio.ensure_future(hass.config_entries.flow.async_configure(result["flow_id"]))
        async with asyncio.timeout(5):
            while not lookups:
                await asyncio.sleep(0)
        for _ in range(5):
            await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second)
    assert [result["step_id"] for result in results] == ["device", "device"]
    assert lookups == 1


async def test_overlapping_call_requests_each_start_a_poll(hass):
    first_request = asyncio.Event()
    requests = 0
    polls = 0

    async def request_mfa(self, auth_type="onewaysms", captcha_code="", phone_id=None):
        nonlocal requests
        requests += 1
        if requests == 1:
            await first_request.wait()
        elif requests > 3:
            raise LoginRateLimited("Too many requests")

    async def poll_call(self):
        nonlocal polls
        polls += 1
        if polls == 1:
            # Resumes the waiting request before Home Assistant's callback for this poll.
            first_request.set()
            raise MfaCallTimeout("Timed out")
        await asyncio.Event().wait()

    with (
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.login",
            autospec=True,
            side_effect=_phonefactor_login(captcha_required=False),
        ),
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.request_mfa_verification",
            autospec=True,
            side_effect=request_mfa,
        ),
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.poll_mfa_call",
            autospec=True,
            side_effect=poll_call,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}, data=CREDENTIALS
        )
        flow_id = result["flow_id"]
        waiting = asyncio.ensure_future(
            hass.config_entries.flow.async_configure(flow_id, {"method": "dialphone"})
        )
        async with asyncio.timeout(5):
            while not requests:
                await asyncio.sleep(0)
        result = await hass.config_entries.flow.async_configure(flow_id, {"method": "dialphone"})
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        async with asyncio.timeout(5):
            result = await waiting
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        assert requests == 2

        hass.config_entries.flow.async_abort(flow_id)
        await hass.async_block_till_done()


async def test_mfa_call_that_fails_at_once_requests_one_call(hass):
    async def failing_poll(self):
        raise LoginError("Invalid poll settings")

    with (
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.login",
            autospec=True,
            side_effect=_phonefactor_login(captcha_required=False),
        ),
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.request_mfa_verification",
            side_effect=[None, None, LoginRateLimited("Too many requests")],
        ) as request_mfa,
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.poll_mfa_call",
            autospec=True,
            side_effect=failing_poll,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}, data=CREDENTIALS
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"method": "dialphone"}
        )
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        await hass.async_block_till_done()

        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert request_mfa.await_count == 1
    assert result["step_id"] == "mfa_challenge"
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.parametrize(
    ("code_side_effect", "appliances_side_effect", "expected_error"),
    [
        (InvalidAuth("Expired"), APPLIANCES, "invalid_auth"),
        (LoginChallenge("Another step"), APPLIANCES, "verification_required"),
        (None, InvalidAuth("Rejected token"), "invalid_auth"),
        (None, ApiError("Unavailable"), "cannot_connect"),
        (None, RateLimited(300), "rate_limited"),
    ],
)
async def test_mfa_errors_after_verification_return_to_user_form(
    hass, tokens, code_side_effect, appliances_side_effect, expected_error
):
    with (
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.login",
            autospec=True,
            side_effect=_phonefactor_login(captcha_required=False),
        ),
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.request_mfa_verification",
            return_value=None,
        ),
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.verify_mfa_code",
            side_effect=code_side_effect or [tokens],
        ),
        patch(
            "custom_components.subzero.api.SubZeroClient.appliances",
            side_effect=(
                appliances_side_effect
                if isinstance(appliances_side_effect, Exception)
                else [appliances_side_effect]
            ),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}, data=CREDENTIALS
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"method": "onewaysms"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": "123456"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": expected_error}
    fields = {field.schema: field for field in result["data_schema"].schema}
    assert fields["username"].description["suggested_value"] == CREDENTIALS["username"]


async def test_mfa_call_followed_by_another_step_returns_to_user_form(hass):
    with (
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.login",
            autospec=True,
            side_effect=_phonefactor_login(captcha_required=False),
        ),
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.request_mfa_verification",
            return_value=None,
        ),
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.poll_mfa_call",
            side_effect=LoginChallenge("Another step"),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}, data=CREDENTIALS
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"method": "dialphone"}
        )
        await hass.async_block_till_done()

        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "verification_required"}


async def test_closing_mfa_dialog_detaches_session_and_cancels_call_poll(hass):
    poll_started = asyncio.Event()
    poll_cancelled = False

    async def slow_poll(self):
        nonlocal poll_cancelled
        poll_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            poll_cancelled = True
            raise

    with (
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.login",
            autospec=True,
            side_effect=_phonefactor_login(captcha_required=False),
        ),
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.request_mfa_verification",
            return_value=None,
        ),
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.poll_mfa_call",
            autospec=True,
            side_effect=slow_poll,
        ),
        patch.object(
            aiohttp.ClientSession, "detach", autospec=True, wraps=aiohttp.ClientSession.detach
        ) as detach,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}, data=CREDENTIALS
        )
        assert result["step_id"] == "mfa_challenge"
        assert detach.call_count == 0

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"method": "dialphone"}
        )
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        await poll_started.wait()

        hass.config_entries.flow.async_abort(result["flow_id"])
        await hass.async_block_till_done()

    assert detach.call_count == 1
    assert poll_cancelled


async def test_closing_dialog_during_sign_in_detaches_the_session(hass):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_login(self, username, password):
        started.set()
        await release.wait()
        await _phonefactor_login(captcha_required=False)(self, username, password)

    with (
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.login",
            autospec=True,
            side_effect=slow_login,
        ),
        patch.object(
            aiohttp.ClientSession, "detach", autospec=True, wraps=aiohttp.ClientSession.detach
        ) as detach,
    ):
        init = asyncio.ensure_future(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "user"}, data=CREDENTIALS
            )
        )
        await started.wait()
        (flow,) = hass.config_entries.flow.async_progress_by_handler(
            DOMAIN, include_uninitialized=True
        )
        hass.config_entries.flow.async_abort(flow["flow_id"])
        assert detach.call_count == 0

        release.set()
        with suppress(UnknownFlow):
            await init
    assert detach.call_count == 1


async def test_closing_dialog_during_code_check_keeps_the_session_until_it_returns(hass, tokens):
    started = asyncio.Event()
    release = asyncio.Event()
    closed_during_check = None

    async def slow_verify(self, code):
        nonlocal closed_during_check
        started.set()
        await release.wait()
        closed_during_check = self.session.closed
        return tokens

    with (
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.login",
            autospec=True,
            side_effect=_phonefactor_login(captcha_required=False),
        ),
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.request_mfa_verification",
            return_value=None,
        ),
        patch(
            "custom_components.subzero.config_flow.SubZeroLogin.verify_mfa_code",
            autospec=True,
            side_effect=slow_verify,
        ),
        patch("custom_components.subzero.api.SubZeroClient.appliances", return_value=APPLIANCES),
        patch.object(
            aiohttp.ClientSession, "detach", autospec=True, wraps=aiohttp.ClientSession.detach
        ) as detach,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}, data=CREDENTIALS
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"method": "onewaysms"}
        )
        check = asyncio.ensure_future(
            hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
        )
        await started.wait()
        hass.config_entries.flow.async_abort(result["flow_id"])
        assert detach.call_count == 0

        release.set()
        with suppress(UnknownFlow):
            await check
    assert closed_during_check is False
    assert detach.call_count == 1


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
async def test_second_setup_of_the_same_account_aborts(hass, tokens, legacy):
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
async def test_reauth_keeps_the_appliance_selection(hass, tokens, wrong_account):
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
    if not wrong_account:
        assert entry.data["username"] == CREDENTIALS["username"]


@pytest.mark.parametrize(
    ("username", "title", "expected"),
    [
        ("saved@example.test", "Renamed account", "saved@example.test"),
        (None, "original@example.test", "original@example.test"),
        (None, "Kitchen", None),
        (None, "Kitchen @ home", None),
    ],
)
async def test_reauth_suggests_the_saved_email(hass, tokens, username, title, expected):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title=title,
        data={
            "devices": DEVICES,
            "tokens": token_state(tokens),
            **({"username": username} if username else {}),
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data=entry.data
    )
    fields = {field.schema: field for field in result["data_schema"].schema}
    assert fields["username"].description.get("suggested_value") == expected
    assert not fields["password"].description
    with patch(
        "custom_components.subzero.config_flow.SubZeroLogin.login",
        side_effect=InvalidAuth("Rejected"),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    fields = {field.schema: field for field in result["data_schema"].schema}
    assert fields["username"].description["suggested_value"] == CREDENTIALS["username"]
    assert not fields["password"].description


@pytest.mark.parametrize("error", [ApiError("Unavailable"), RateLimited(300)])
async def test_options_list_failure_is_retryable(hass, tokens, error):
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


async def test_options_list_keeps_selected_but_missing_appliances(hass, tokens):
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


@pytest.mark.parametrize("interval", [0, 60, 120, 300, 600])
async def test_options_save_state_check_interval(hass, tokens, interval):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={"tokens": token_state(tokens), "devices": DEVICES},
        options={"status_poll_interval": 120},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.subzero.api.SubZeroClient.appliances", return_value=APPLIANCES):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["data_schema"]({})["status_poll_interval"] == "120"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"device_ids": list(DEVICES), "status_poll_interval": str(interval)}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"devices": DEVICES, "status_poll_interval": interval}


@pytest.mark.parametrize("enabled", [False, True])
async def test_options_toggle_firebase_alerts(hass, tokens, enabled):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={"tokens": token_state(tokens), "devices": DEVICES},
        options={"firebase_alerts": not enabled},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.subzero.api.SubZeroClient.appliances", return_value=APPLIANCES):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["data_schema"]({})["firebase_alerts"] is not enabled
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"device_ids": list(DEVICES), "firebase_alerts": enabled}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options.get("firebase_alerts", False) is enabled
