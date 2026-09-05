"""Sign in and select an appliance through Home Assistant."""

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_create_clientsession, async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .api import ApiError, RateLimited, SubZeroClient
from .app_config import SUBSCRIPTION_KEY
from .auth import InvalidAuth, LoginChallenge, LoginError, SubZeroLogin
from .const import DOMAIN


class SubZeroConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
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
                tokens = await SubZeroLogin(session).login(
                    user_input["username"], user_input["password"]
                )
                self._client = SubZeroClient(
                    async_get_clientsession(self.hass), SUBSCRIPTION_KEY, tokens
                )
                if self.source == config_entries.SOURCE_REAUTH:
                    original = self._get_reauth_entry()
                    if (
                        self._client.tokens["user_id"].lower()
                        != original.data["tokens"]["user_id"].lower()
                    ):
                        return self.async_abort(reason="wrong_account")
                self._appliances = {
                    appliance.id: appliance for appliance in await self._client.appliances()
                }
                if not self._appliances:
                    return self.async_abort(reason="no_appliances")
                if self.source == config_entries.SOURCE_REAUTH:
                    device_id = self._get_reauth_entry().data["device_id"]
                    if device_id not in self._appliances:
                        return self.async_abort(reason="wrong_account")
                    return await self.async_step_device({"device_id": device_id})
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
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("username"): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.EMAIL)
                    ),
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
            device_id = user_input["device_id"]
            if device_id not in self._appliances:
                errors["base"] = "invalid_device"
            else:
                await self.async_set_unique_id(device_id)
                if self.source != config_entries.SOURCE_REAUTH:
                    self._abort_if_unique_id_configured()
                appliance = self._appliances[device_id]
                try:
                    state = await self._client.state(device_id)
                    data = {
                        "device_id": device_id,
                        "temperature_unit": appliance.temperature_unit,
                        "tokens": self._client.tokens,
                    }
                    if self.source == config_entries.SOURCE_REAUTH:
                        return self.async_update_reload_and_abort(
                            self._get_reauth_entry(), data_updates=data
                        )
                    return self.async_create_entry(
                        title=state.get("appliance_model") or appliance.name, data=data
                    )
                except InvalidAuth:
                    errors["base"] = "invalid_auth"
                except RateLimited:
                    errors["base"] = "rate_limited"
                except ApiError:
                    errors["base"] = "cannot_connect"
        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(
                {
                    vol.Required("device_id"): vol.In(
                        {key: value.name for key, value in self._appliances.items()}
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data):
        return await self.async_step_user()
