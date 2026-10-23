"""Sign in once and manage the account's appliance selection in Home Assistant."""

import asyncio
from functools import wraps

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession, async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import ApiError, RateLimited, SubZeroClient
from .app_config import SUBSCRIPTION_KEY
from .auth import (
    InvalidAuth,
    InvalidCaptcha,
    InvalidVerificationCode,
    LoginChallenge,
    LoginError,
    LoginRateLimited,
    MfaCallTimeout,
    SubZeroLogin,
)
from .const import CONF_FIREBASE_ALERTS, DOMAIN, STATUS_POLL_INTERVALS
from .coordinator import firebase_alerts_enabled, selected_devices, status_poll_interval


def device_schema(devices: dict, selected: list[str]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("device_ids", default=selected): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        {"value": key, "label": value["name"]} for key, value in devices.items()
                    ],
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )
        }
    )


def configured_devices(hass, exclude_entry_id=None) -> set[str]:
    return {
        device_id
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.entry_id != exclude_entry_id
        for device_id in selected_devices(entry)
    }


def holds_login_session(step):
    """Keep the sign-in session until the step returns, even if the flow is removed."""

    @wraps(step)
    async def wrapper(self, user_input=None):
        self._running_steps += 1
        try:
            return await step(self, user_input)
        finally:
            self._running_steps -= 1
            if self._removed and not self._running_steps:
                self._cleanup_login()

    return wrapper


class SubZeroConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2
    MINOR_VERSION = 3
    _client: SubZeroClient
    _devices: dict[str, dict]
    _title: str | None = None
    _login: SubZeroLogin | None = None
    _login_session: aiohttp.ClientSession | None = None
    _call_task: asyncio.Task[dict] | None = None
    _mfa_tokens: dict | None = None
    _mfa_finish: asyncio.Task | None = None
    _mfa_error: str = ""
    _user_error: str = ""
    _removed = False
    _running_steps = 0

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return SubZeroOptionsFlow()

    def _cleanup_login(self) -> None:
        if self._call_task is not None:
            self._call_task.cancel()
            self._call_task = None
        if self._login_session is not None:
            self._login_session.detach()
            self._login_session = None
        self._login = None
        self._mfa_error = ""

    @callback
    def async_remove(self) -> None:
        # Closing the dialog can remove the flow while a step still waits on Sub-Zero. That
        # step releases the session when it returns, so its next request doesn't fail.
        self._removed = True
        if not self._running_steps:
            self._cleanup_login()

    async def _async_return_to_user(self, error: str):
        self._cleanup_login()
        self._user_error = error
        return await self.async_step_user()

    async def _async_finish_login(self, tokens: dict):
        self._cleanup_login()
        try:
            client = SubZeroClient(async_get_clientsession(self.hass), SUBSCRIPTION_KEY, tokens)
            user_id = client.tokens["user_id"].lower()
            if self.source == config_entries.SOURCE_REAUTH:
                original = self._get_reauth_entry()
                original_id = original.data["tokens"].get("user_id") or original.unique_id
                if original_id is None or user_id != original_id.lower():
                    return self.async_abort(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    original,
                    data_updates={
                        "tokens": client.tokens,
                        "username": self._title,
                    },
                )
            await self.async_set_unique_id(user_id)
            if any(
                (entry.data["tokens"].get("user_id") or entry.unique_id or "").lower() == user_id
                for entry in self._async_current_entries()
            ):
                return self.async_abort(reason="already_configured")
            appliances = await client.appliances()
        except InvalidAuth:
            return await self._async_return_to_user("invalid_auth")
        except RateLimited:
            return await self._async_return_to_user("rate_limited")
        except ApiError, aiohttp.ClientError, TimeoutError:
            return await self._async_return_to_user("cannot_connect")
        if not appliances:
            return self.async_abort(reason="no_appliances")
        configured = configured_devices(self.hass)
        # Set together after the lookup, so a finish that overlaps another can't pair one
        # sign-in's tokens with another's appliances.
        self._client = client
        self._devices = {
            appliance.id: {
                "name": appliance.name,
                "temperature_unit": appliance.temperature_unit,
            }
            for appliance in appliances
            if appliance.id not in configured
        }
        if not self._devices:
            return self.async_abort(reason="all_configured")
        return await self.async_step_device()

    @holds_login_session
    async def async_step_user(self, user_input=None):
        errors = {}
        if self._user_error:
            errors["base"] = self._user_error
            self._user_error = ""
        if user_input is not None:
            self._cleanup_login()
            self._mfa_tokens = None
            self._mfa_finish = None
            self._title = user_input["username"]
            session = async_create_clientsession(
                self.hass,
                auto_cleanup=False,
                cookie_jar=aiohttp.CookieJar(quote_cookie=False),
                timeout=aiohttp.ClientTimeout(total=40),
            )
            login = SubZeroLogin(session, async_get_clientsession(self.hass))
            keep_session = False
            try:
                tokens = await login.login(user_input["username"], user_input["password"])
            except LoginChallenge:
                if login.is_phonefactor_challenge:
                    try:
                        if login.captcha_required:
                            await login.get_captcha_challenge()
                    except LoginError, aiohttp.ClientError, TimeoutError:
                        errors["base"] = "cannot_connect"
                    else:
                        self._login_session = session
                        self._login = login
                        keep_session = True
                        return await self.async_step_mfa_challenge()
                else:
                    errors["base"] = "verification_required"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except LoginRateLimited, RateLimited:
                errors["base"] = "rate_limited"
            except LoginError, ApiError, aiohttp.ClientError, TimeoutError:
                errors["base"] = "cannot_connect"
            else:
                return await self._async_finish_login(tokens)
            finally:
                if not keep_session:
                    session.detach()
        username = (user_input or {}).get("username") or self._title
        if username is None and self.source == config_entries.SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            try:
                username = vol.Email()(entry.data.get("username", entry.title))
            except vol.Invalid:
                pass
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "username", description={"suggested_value": username} if username else {}
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL)),
                    vol.Required("password"): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    @holds_login_session
    async def async_step_mfa_challenge(self, user_input=None):
        if self._login is None:
            return await self.async_step_user()
        errors = {}
        if self._mfa_error:
            errors["base"] = self._mfa_error
            self._mfa_error = ""
        login = self._login
        if user_input is not None:
            auth_type = user_input.get("method", "onewaysms")
            captcha_code = user_input.get("captcha", "")
            try:
                await login.request_mfa_verification(auth_type=auth_type, captcha_code=captcha_code)
            except InvalidCaptcha:
                errors["base"] = "invalid_captcha"
                try:
                    await login.get_captcha_challenge()
                except LoginError, aiohttp.ClientError, TimeoutError:
                    errors["base"] = "cannot_connect"
            except LoginRateLimited, RateLimited:
                errors["base"] = "rate_limited"
            except InvalidAuth:
                return await self._async_return_to_user("invalid_auth")
            except LoginError, ApiError, aiohttp.ClientError, TimeoutError:
                errors["base"] = "cannot_connect"
            else:
                login.captcha_solved = not login.captcha_required
                login.captcha_image = ""
                if auth_type == "dialphone":
                    # Each request places a new call, so it gets a new poll. A poll left from
                    # an overlapping request would otherwise finish this step during the submit.
                    if self._call_task is not None:
                        self._call_task.cancel()
                        self._call_task = None
                    return await self.async_step_mfa_call()
                return await self.async_step_mfa_code()

        show_captcha = login.captcha_required and not login.captcha_solved
        if show_captcha and not login.captcha_image and errors.get("base") != "cannot_connect":
            try:
                await login.get_captcha_challenge()
            except LoginError, aiohttp.ClientError, TimeoutError:
                errors["base"] = "cannot_connect"

        fields: dict = {}
        if show_captcha:
            fields[vol.Required("captcha")] = TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT)
            )
        fields[vol.Required("method", default="onewaysms")] = SelectSelector(
            SelectSelectorConfig(
                options=[
                    {"value": "onewaysms", "label": "Text message (SMS)"},
                    {"value": "dialphone", "label": "Phone call"},
                ],
                mode=SelectSelectorMode.LIST,
            )
        )
        captcha_image = (
            f"\n\n![CAPTCHA]({login.captcha_image})" if show_captcha and login.captcha_image else ""
        )
        return self.async_show_form(
            step_id="mfa_challenge",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={
                "masked_phone": login.masked_phone,
                "captcha_image": captcha_image,
            },
        )

    @holds_login_session
    async def async_step_mfa_code(self, user_input=None):
        if self._login is None:
            return await self.async_step_user()
        errors = {}
        login = self._login
        if user_input is not None:
            code = user_input.get("code", "").strip()
            if not code:
                return await self.async_step_mfa_challenge()
            try:
                tokens = await login.verify_mfa_code(code)
            except MfaCallTimeout:
                self._mfa_error = "mfa_code_expired"
                return await self.async_step_mfa_challenge()
            except InvalidVerificationCode:
                errors["base"] = "invalid_mfa_code"
            except LoginRateLimited, RateLimited:
                errors["base"] = "rate_limited"
            except InvalidAuth:
                return await self._async_return_to_user("invalid_auth")
            except LoginChallenge:
                return await self._async_return_to_user("verification_required")
            except LoginError, ApiError, aiohttp.ClientError, TimeoutError:
                errors["base"] = "cannot_connect"
            else:
                return await self._async_finish_login(tokens)

        return self.async_show_form(
            step_id="mfa_code",
            data_schema=vol.Schema(
                {
                    vol.Optional("code", default=""): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.TEXT)
                    ),
                }
            ),
            errors=errors,
            description_placeholders={"masked_phone": login.masked_phone},
        )

    async def async_step_mfa_call(self, user_input=None):
        if self._login is None:
            return await self.async_step_user()
        if self._call_task is None:
            # Started lazily so this always shows progress first. A poll that failed before
            # its first wait would otherwise end here, and Home Assistant would resubmit the
            # challenge form with the same input, requesting another call.
            self._call_task = self.hass.async_create_task(
                self._login.poll_mfa_call(), eager_start=False
            )
        if not self._call_task.done():
            return self.async_show_progress(
                step_id="mfa_call",
                progress_action="wait_for_call",
                progress_task=self._call_task,
                description_placeholders={"masked_phone": self._login.masked_phone},
            )
        task = self._call_task
        self._call_task = None
        try:
            self._mfa_tokens = task.result()
        except MfaCallTimeout:
            self._mfa_error = "mfa_call_failed"
            return self.async_show_progress_done(next_step_id="mfa_challenge")
        except LoginRateLimited, RateLimited:
            self._mfa_error = "rate_limited"
            return self.async_show_progress_done(next_step_id="mfa_challenge")
        except InvalidAuth:
            self._cleanup_login()
            self._user_error = "invalid_auth"
            return self.async_show_progress_done(next_step_id="user")
        except LoginChallenge:
            self._cleanup_login()
            self._user_error = "verification_required"
            return self.async_show_progress_done(next_step_id="user")
        except LoginError, ApiError, aiohttp.ClientError, TimeoutError:
            self._mfa_error = "cannot_connect"
            return self.async_show_progress_done(next_step_id="mfa_challenge")
        self._mfa_finish = None
        return self.async_show_progress_done(next_step_id="mfa_finish")

    async def async_step_mfa_finish(self, user_input=None):
        # The frontend and the call task's callback can both advance the flow here. They
        # share one finish of the sign-in, so neither can act on a different outcome.
        if self._mfa_finish is None:
            if self._mfa_tokens is None:
                return await self.async_step_user()
            self._mfa_finish = self.hass.async_create_task(
                self._async_finish_login(self._mfa_tokens)
            )
        return await asyncio.shield(self._mfa_finish)

    async def async_step_device(self, user_input=None):
        errors = {}
        if user_input is not None:
            selected = user_input["device_ids"]
            if not selected:
                errors["base"] = "select_device"
            elif not set(selected).issubset(self._devices):
                errors["base"] = "invalid_device"
            elif set(selected).intersection(configured_devices(self.hass)):
                return self.async_abort(reason="all_configured")
            else:
                return self.async_create_entry(
                    title=self._title,
                    data={
                        "tokens": self._client.tokens,
                        "username": self._title,
                        "devices": {key: self._devices[key] for key in selected},
                    },
                )
        return self.async_show_form(
            step_id="device",
            data_schema=device_schema(self._devices, list(self._devices)),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data):
        return await self.async_step_user()


class SubZeroOptionsFlow(config_entries.OptionsFlow):
    _devices: dict[str, dict] | None = None

    async def async_step_init(self, user_input=None):
        errors = {}
        entry = self.config_entry
        current = selected_devices(entry)
        if self._devices is None:

            async def save_tokens(tokens: dict) -> None:
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, "tokens": tokens}
                )

            try:
                runtime = getattr(entry, "runtime_data", None)
                client = (
                    runtime.client
                    if runtime is not None
                    else SubZeroClient(
                        async_get_clientsession(self.hass),
                        SUBSCRIPTION_KEY,
                        entry.data["tokens"],
                        save_tokens,
                    )
                )
                appliances = await client.appliances()
                configured = configured_devices(self.hass, entry.entry_id)
                self._devices = {
                    appliance.id: {
                        "name": current.get(appliance.id, {}).get("name") or appliance.name,
                        "temperature_unit": appliance.temperature_unit,
                    }
                    for appliance in appliances
                    if appliance.id not in configured
                }
                for device_id, device in current.items():
                    if device_id not in configured:
                        self._devices.setdefault(device_id, device)
            except InvalidAuth:
                entry.async_start_reauth(self.hass)
                return self.async_abort(reason="reauth_required")
            except RateLimited:
                errors["base"] = "rate_limited"
            except ApiError:
                errors["base"] = "cannot_connect"
            if errors:
                configured = configured_devices(self.hass, entry.entry_id)
                saved = {
                    device_id: device
                    for device_id, device in current.items()
                    if device_id not in configured
                }
                if not saved:
                    return self.async_show_form(
                        step_id="init", data_schema=vol.Schema({}), errors=errors
                    )
                self._devices = saved
        if user_input is not None and "device_ids" in user_input:
            selected = user_input["device_ids"]
            if not set(selected).issubset(self._devices):
                errors["base"] = "invalid_device"
            elif set(selected).intersection(configured_devices(self.hass, entry.entry_id)):
                return self.async_abort(reason="all_configured")
            else:
                options = {
                    "devices": {key: self._devices[key] for key in selected},
                    "status_poll_interval": int(
                        user_input.get("status_poll_interval", status_poll_interval(entry))
                    ),
                }
                if user_input.get(CONF_FIREBASE_ALERTS, firebase_alerts_enabled(entry)) is True:
                    options[CONF_FIREBASE_ALERTS] = True
                if (
                    entry.state is not config_entries.ConfigEntryState.LOADED
                    and self.hass.config_entries.async_update_entry(entry, options=options)
                ):
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_create_entry(data=options)
        return self.async_show_form(
            step_id="init",
            data_schema=device_schema(
                self._devices, [key for key in current if key in self._devices]
            ).extend(
                {
                    vol.Optional(
                        CONF_FIREBASE_ALERTS, default=firebase_alerts_enabled(entry)
                    ): BooleanSelector(),
                    vol.Required(
                        "status_poll_interval", default=str(status_poll_interval(entry))
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[str(seconds) for seconds in STATUS_POLL_INTERVALS],
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key="status_poll_interval",
                        )
                    ),
                }
            ),
            errors=errors,
        )
