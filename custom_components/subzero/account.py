"""Account stream orchestration, initial setup, and periodic state checks."""

import asyncio
import logging
import random
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from itertools import cycle

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.event import async_track_time_interval

from .api import (
    ApiError,
    ChannelOpened,
    ConnectionLimitReached,
    RateLimited,
    SubZeroClient,
)
from .auth import InvalidAuth
from .const import (
    CONF_FIREBASE_ALERTS,
    DEFAULT_STATUS_POLL_INTERVAL,
    MAX_RECONNECT_DELAY,
    RECONNECT_DELAY,
    STATUS_POLL_INTERVALS,
)
from .coordinator import SubZeroCoordinator
from .faults import SubZeroFaultsCoordinator

_LOGGER = logging.getLogger(__name__)
INITIAL_STATE_TIMEOUT = 16
# The state-check interval while push is down, within TRANSPORT_GRACE_SECONDS.
OUTAGE_STATUS_INTERVAL = 60


def status_poll_interval(entry: ConfigEntry) -> int:
    interval = entry.options.get("status_poll_interval", DEFAULT_STATUS_POLL_INTERVAL)
    return interval if interval in STATUS_POLL_INTERVALS else DEFAULT_STATUS_POLL_INTERVAL


def firebase_alerts_enabled(entry: ConfigEntry) -> bool:
    return entry.options.get(CONF_FIREBASE_ALERTS, False) is True


def selected_devices(entry: ConfigEntry) -> dict[str, dict]:
    """Return the appliance selection, including entries awaiting migration."""
    if "device_id" in entry.data:
        return {
            entry.data["device_id"]: {
                "name": entry.title,
                "temperature_unit": entry.data.get("temperature_unit"),
            }
        }
    return entry.options.get("devices", entry.data["devices"])


class SubZeroAccount:
    """Share account tokens and one notification stream across selected appliances."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: SubZeroClient):
        self.hass = hass
        self.entry = entry
        self.client = client
        self.alerts = None
        self.options_lock = asyncio.Lock()
        self.coordinators = {
            device_id: SubZeroCoordinator(hass, entry, client, device_id, device)
            for device_id, device in selected_devices(entry).items()
        }
        self._fault_metadata: dict[tuple[str, str], dict] = {}
        self.fault_coordinators = {
            device_id: SubZeroFaultsCoordinator(
                hass, entry, client, coordinator, self._fault_metadata
            )
            for device_id, coordinator in self.coordinators.items()
        }
        self._initial_states = {device_id: asyncio.Event() for device_id in self.coordinators}
        self._verification_order = cycle(self.coordinators.values())
        self._verifications: dict[str, asyncio.Task] = {}
        self._state_check_interval: int | None = None
        self._outage_checks = False
        self._cancel_state_checks: Callable[[], None] | None = None
        self.entry.async_on_unload(self._async_cancel_state_checks)

    async def async_setup(self) -> None:
        if not self.coordinators:
            return
        selected = selected_devices(self.entry)
        metadata_error: ApiError | None = None
        try:
            appliances = await self.client.appliances()
        except InvalidAuth as error:
            raise ConfigEntryAuthFailed(str(error)) from error
        except RateLimited as error:
            raise ConfigEntryNotReady(str(error)) from error
        except ApiError as error:
            metadata_error = error
        else:
            units = {appliance.id: appliance.temperature_unit for appliance in appliances}
            for coordinator in self.coordinators.values():
                if coordinator.device_id in units:
                    coordinator.device["temperature_unit"] = units[coordinator.device_id]
            devices = {
                device_id: dict(coordinator.device)
                for device_id, coordinator in self.coordinators.items()
            }
            if selected_devices(self.entry) == selected and devices != selected:
                if "devices" in self.entry.options:
                    self.hass.config_entries.async_update_entry(
                        self.entry, options={**self.entry.options, "devices": devices}
                    )
                else:
                    self.hass.config_entries.async_update_entry(
                        self.entry, data={**self.entry.data, "devices": devices}
                    )
        self.entry.async_create_background_task(self.hass, self.listen(), "Sub-Zero notifications")
        try:
            try:
                async with asyncio.timeout(INITIAL_STATE_TIMEOUT):
                    await asyncio.gather(*(ready.wait() for ready in self._initial_states.values()))
            except TimeoutError:
                pass
            errors = []
            for coordinator in self.coordinators.values():
                if coordinator.last_update_success and coordinator.confirmed_at is not None:
                    continue
                if isinstance(coordinator.last_exception, InvalidAuth):
                    raise ConfigEntryAuthFailed(str(coordinator.last_exception))
                try:
                    await coordinator.async_config_entry_first_refresh()
                except ConfigEntryNotReady as error:
                    coordinator.data = {}
                    errors.append((coordinator, error))
            errors = [
                (coordinator, error)
                for coordinator, error in errors
                # Push may have delivered its state while setup read the others.
                if not (coordinator.last_update_success and coordinator.confirmed_at is not None)
            ]
            if errors and len(errors) == len(self.coordinators):
                raise errors[0][1]
            for coordinator, error in errors:
                # Setup goes on for the others, so Home Assistant does not report this one.
                _LOGGER.warning(
                    "Could not read %s from Sub-Zero during setup, so it stays unavailable "
                    "until its state arrives: %s",
                    coordinator.device["name"],
                    error.__cause__,
                )
        finally:
            self._initial_states.clear()
        for coordinator in self.coordinators.values():
            coordinator.start_recovery_if_needed()
        if metadata_error is not None:
            _LOGGER.warning(
                "Could not refresh appliance units; using cached units where available: %s",
                metadata_error,
            )
        for coordinator in self.fault_coordinators.values():
            self.entry.async_create_background_task(
                self.hass, coordinator.async_refresh(), "Sub-Zero faults"
            )
        self.async_update_state_checks()

    @callback
    def async_update_state_checks(self) -> None:
        """Apply the state-check interval without interrupting appliance updates."""
        if self.entry.pref_disable_polling:
            for coordinator in self.coordinators.values():
                coordinator.cancel_followup()
        interval = 0 if self.entry.pref_disable_polling else status_poll_interval(self.entry)
        self._outage_checks = not self.entry.pref_disable_polling and self._push_interrupted()
        if self._outage_checks:
            # Each current read keeps the appliance available for another
            # TRANSPORT_GRACE_SECONDS until push returns.
            interval = min(interval or OUTAGE_STATUS_INTERVAL, OUTAGE_STATUS_INTERVAL)
        if interval == self._state_check_interval:
            return
        self._state_check_interval = interval
        self._async_cancel_state_checks()
        if interval and self.coordinators:
            self._cancel_state_checks = async_track_time_interval(
                self.hass,
                self._verify_next_appliance,
                timedelta(seconds=interval / len(self.coordinators)),
                cancel_on_shutdown=True,
            )

    def _push_interrupted(self) -> bool:
        """Whether a reachable appliance waits for push to return and reconfirm its state."""
        return any(coordinator.waiting_for_push for coordinator in self.coordinators.values())

    @callback
    def _async_cancel_state_checks(self) -> None:
        if self._cancel_state_checks is not None:
            self._cancel_state_checks()
            self._cancel_state_checks = None

    @callback
    def _verify_next_appliance(self, _now: datetime) -> None:
        if self._outage_checks and not self._push_interrupted():
            # Push returned and reconfirmed state; go back to the configured interval.
            self.async_update_state_checks()
            return
        coordinator = next(self._verification_order)
        pending = self._verifications.get(coordinator.device_id)
        if (
            self.entry.pref_disable_polling
            or not self._state_check_interval
            or not coordinator.can_verify_state
            or (pending is not None and not pending.done())
        ):
            return
        self._verifications[coordinator.device_id] = self.entry.async_create_background_task(
            self.hass,
            coordinator.async_verify_state(),
            "Sub-Zero state verification",
        )

    @callback
    def set_error(self, error: Exception) -> None:
        for coordinator in self.coordinators.values():
            coordinator.on_account_error(error)
        for ready in self._initial_states.values():
            ready.set()

    async def listen(self) -> None:
        backoff = RECONNECT_DELAY
        while True:
            started = time.monotonic()
            delivered = False
            try:
                async for device_id, update in self.client.watch(list(self.coordinators)):
                    delivered = True
                    coordinator = self.coordinators[device_id]
                    recover = device_id not in self._initial_states
                    if isinstance(update, ApiError):
                        coordinator.on_channel_error(update)
                        if ready := self._initial_states.get(device_id):
                            ready.set()
                    elif isinstance(update, ChannelOpened):
                        coordinator.on_channel_opened(recover=recover)
                    else:
                        try:
                            accepted = coordinator.apply_update(update, recover=recover)
                        except Exception:
                            _LOGGER.exception(
                                "Skipped an update for %s that could not be applied",
                                coordinator.device["name"],
                            )
                            continue
                        if update.full and accepted and coordinator.transport_healthy:
                            if ready := self._initial_states.get(device_id):
                                ready.set()
                raise ApiError("Sub-Zero's notification stream ended.")
            except InvalidAuth as error:
                self.set_error(error)
                self.entry.async_start_reauth(self.hass)
                return
            except ApiError as error:
                for coordinator in self.coordinators.values():
                    coordinator.async_transport_error(error, lost=delivered)
                for ready in self._initial_states.values():
                    ready.set()
                # Status reads keep appliances available while push is down.
                self.async_update_state_checks()
                retry_after = error.retry_after if isinstance(error, RateLimited) else 0
                full = isinstance(error, ConnectionLimitReached)
            except Exception as error:
                _LOGGER.exception("Sub-Zero's notification stream failed unexpectedly")
                self.set_error(error)
                retry_after = 0
                full = False
            finally:
                for coordinator in self.coordinators.values():
                    coordinator.cancel_recovery()
                for coordinator in self.coordinators.values():
                    await coordinator.async_stop_recovery()
            if full:
                # A full service admits a connection when another client leaves, so
                # the delay does not grow; it is already randomized.
                await asyncio.sleep(retry_after)
                continue
            if time.monotonic() - started >= 120:
                # A connection that worked for a while is tried again at once, then with
                # increasing delays.
                delay = retry_after
                backoff = RECONNECT_DELAY
            else:
                delay = max(backoff, retry_after)
                backoff = min(backoff * 2, MAX_RECONNECT_DELAY)
            await asyncio.sleep(delay + random.uniform(0, 5))
