"""Push updates with occasional reconciliation and bounded reconnect attempts."""

import asyncio
import logging
import random
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ApiError, RateLimited, SubZeroClient
from .auth import InvalidAuth
from .const import DOMAIN, STATE_KEYS

_LOGGER = logging.getLogger(__name__)


class SubZeroCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: SubZeroClient):
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=30),
            always_update=False,
        )
        self.client = client
        self.device_id = entry.data["device_id"]

    async def _async_update_data(self) -> dict:
        try:
            data = await self.client.state(self.device_id)
        except InvalidAuth as error:
            raise ConfigEntryAuthFailed(str(error)) from error
        except RateLimited as error:
            raise UpdateFailed(str(error), retry_after=error.retry_after) from error
        except ApiError as error:
            raise UpdateFailed(str(error)) from error
        return {key: value for key, value in data.items() if key in STATE_KEYS}

    async def listen(self) -> None:
        backoff = 30
        while True:
            started = time.monotonic()
            delay = backoff
            try:
                async for update in self.client.watch(self.device_id):
                    if not self.last_update_success and not update.full:
                        continue
                    properties = {
                        key: value for key, value in update.properties.items() if key in STATE_KEYS
                    }
                    updated = properties if update.full else {**self.data, **properties}
                    if updated != self.data or not self.last_update_success:
                        self.async_set_updated_data(updated)
                    if time.monotonic() - started >= 120:
                        backoff = 30
                raise ApiError("Sub-Zero's notification stream ended.")
            except InvalidAuth as error:
                self.async_set_update_error(error)
                self.config_entry.async_start_reauth(self.hass)
                return
            except RateLimited as error:
                self.async_set_update_error(error)
                delay = max(backoff, error.retry_after)
            except ApiError as error:
                self.async_set_update_error(error)
                delay = backoff
            await asyncio.sleep(delay + random.uniform(0, 5))
            backoff = min(backoff * 2, 900)
