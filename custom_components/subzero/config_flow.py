"""Sign in once and manage the account's appliance selection in Home Assistant."""

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession, async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import ApiError, RateLimited, SubZeroClient
from .app_config import SUBSCRIPTION_KEY
from .auth import InvalidAuth, LoginChallenge, LoginError, SubZeroLogin
from .const import DOMAIN, selected_devices


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


class SubZeroConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2
    MINOR_VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return SubZeroOptionsFlow()

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            session = async_create_clientsession(
                self.hass,
                auto_cleanup=False,
                cookie_jar=aiohttp.CookieJar(quote_cookie=False),
                timeout=aiohttp.ClientTimeout(total=40),
            )
            try:
                tokens = await SubZeroLogin(session, async_get_clientsession(self.hass)).login(
                    user_input["username"], user_input["password"]
                )
                self._client = SubZeroClient(
                    async_get_clientsession(self.hass), SUBSCRIPTION_KEY, tokens
                )
                user_id = self._client.tokens["user_id"].lower()
                if self.source == config_entries.SOURCE_REAUTH:
                    original = self._get_reauth_entry()
                    if user_id != original.data["tokens"]["user_id"].lower():
                        return self.async_abort(reason="wrong_account")
                    return self.async_update_reload_and_abort(
                        original,
                        data_updates={
                            "tokens": self._client.tokens,
                            "username": user_input["username"],
                        },
                    )
                await self.async_set_unique_id(user_id)
                if any(
                    entry.data["tokens"]["user_id"].lower() == user_id
                    for entry in self._async_current_entries()
                ):
                    return self.async_abort(reason="already_configured")
                appliances = await self._client.appliances()
                if not appliances:
                    return self.async_abort(reason="no_appliances")
                configured = configured_devices(self.hass)
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
                self._title = user_input["username"]
                return await self.async_step_device()
            except LoginChallenge:
                errors["base"] = "verification_required"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except RateLimited:
                errors["base"] = "rate_limited"
            except LoginError, ApiError, aiohttp.ClientError, TimeoutError:
                errors["base"] = "cannot_connect"
            finally:
                session.detach()
        username = (user_input or {}).get("username")
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


class SubZeroOptionsFlow(config_entries.OptionsFlowWithReload):
    async def async_step_init(self, user_input=None):
        errors = {}
        entry = self.config_entry
        current = selected_devices(entry)
        if not hasattr(self, "_devices"):

            async def save_tokens(tokens: dict) -> None:
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, "tokens": tokens}
                )

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
            try:
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
                return self.async_show_form(
                    step_id="init", data_schema=vol.Schema({}), errors=errors
                )
        if user_input is not None and "device_ids" in user_input:
            selected = user_input["device_ids"]
            if not set(selected).issubset(self._devices):
                errors["base"] = "invalid_device"
            elif set(selected).intersection(configured_devices(self.hass, entry.entry_id)):
                return self.async_abort(reason="all_configured")
            else:
                return self.async_create_entry(
                    data={"devices": {key: self._devices[key] for key in selected}}
                )
        return self.async_show_form(
            step_id="init",
            data_schema=device_schema(
                self._devices, [key for key in current if key in self._devices]
            ),
            errors=errors,
        )
