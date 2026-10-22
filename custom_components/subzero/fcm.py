"""Receive appliance alerts from the mobile app's Firebase push channel."""

import asyncio
import copy
import logging
import secrets
import time
import uuid
from collections.abc import Callable, Iterable
from contextlib import suppress
from datetime import UTC, datetime

from firebase_messaging import FcmPushClient, FcmRegisterConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .alert_loc_keys import ALERT_LOCATION_KEYS, RANGE_ALERT_LOCATION_KEYS
from .api import ApiError, SubZeroClient
from .app_config import FIREBASE_CONFIG
from .auth import InvalidAuth
from .controls import (
    appliance_type,
    has_left_and_right_ovens,
    is_dishwasher,
    is_fridge,
    is_ice_maker,
    is_oven,
    is_wine,
)
from .coordinator import SubZeroCoordinator

_LOGGER = logging.getLogger(__name__)
RETRY_INTERVAL = 300
REFRESH_INTERVAL = 60
HEALTH_CHECK_INTERVAL = 60


def parse_alert(message: dict) -> tuple[str, datetime, int, int] | None:
    """Read only the appliance identity, code, sequence, and event time."""
    if not isinstance(message, dict) or not isinstance(data := message.get("data"), dict):
        return None
    device_id = data.get("deviceId")
    values = (data.get("value"), data.get("notifSeq"), data.get("event_time"))
    if (
        not isinstance(device_id, str)
        or not device_id
        or not all(isinstance(value, str) and value.isdecimal() for value in values)
    ):
        return None
    try:
        code, sequence, millis = map(int, values)
        timestamp = datetime.fromtimestamp(millis / 1000, UTC)
    except OverflowError, ValueError, OSError:
        return None
    if not 1 < timestamp.year < 9999:
        return None
    return device_id, timestamp, sequence, code


async def async_remove_alert_subscriptions(
    client: SubZeroClient,
    psid: str,
    device_ids: Iterable[str],
    removed: Callable[[str], None] | None = None,
    failed: Callable[[str, ApiError], None] | None = None,
) -> bool:
    """Unsubscribe appliances from alerts to a phone ID; return whether all succeeded.

    `removed` and `failed` are called for each appliance as it finishes, so its
    result counts even if a later one times out.
    """
    succeeded = True
    for device_id in list(device_ids):
        try:
            await client.unsubscribe_all_alerts(psid, device_id)
        except ApiError as error:
            succeeded = False
            if failed is not None:
                failed(device_id, error)
        else:
            if removed is not None:
                removed(device_id)
    return succeeded


class FcmAlerts:
    """Keep a separate, stable Firebase client for this Home Assistant account."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: SubZeroClient,
        coordinators: dict[str, SubZeroCoordinator],
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self.coordinators = coordinators
        self.push: FcmPushClient | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._subscribed: dict[str, tuple[str, dict[int, str]]] = {}
        self.received = 0
        self.last_received: str | None = None
        self.last_error: str | None = None
        self.last_sync: str | None = None
        self.subscription_healthy = False

    @callback
    def _save(self, **changes: object) -> None:
        # Copies, so a caller's later changes to its dict still differ from the
        # entry's data and are saved.
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, **copy.deepcopy(changes)}
        )

    @callback
    def _save_credentials(self, credentials: dict) -> None:
        if not self._stop.is_set():
            self._save(fcm_credentials=credentials)

    @callback
    def _on_alert(self, message: dict, _persistent_id: str, _context: object) -> None:
        parsed = parse_alert(message)
        if parsed is None:
            return
        device_id, timestamp, sequence, code = parsed
        if not self._stop.is_set() and (coordinator := self.coordinators.get(device_id)):
            self.received += 1
            self.last_received = datetime.now(UTC).isoformat()
            coordinator.async_receive_event(timestamp, sequence, code, source="firebase")

    @callback
    def start(self) -> None:
        self._task = self.entry.async_create_background_task(
            self.hass, self._run(), "Sub-Zero Firebase alerts"
        )

    @callback
    def start_cleanup(self) -> None:
        """Remove our alert subscriptions without connecting to Firebase."""
        self._task = self.entry.async_create_background_task(
            self.hass, self._cleanup_loop(), "Remove Sub-Zero Firebase alerts"
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        await self._close_push()

    async def _close_push(self) -> None:
        push, self.push = self.push, None
        if push is not None:
            if push.do_listen or push.tasks:
                await push.stop()
            await asyncio.gather(*push.tasks, return_exceptions=True)

    async def _cleanup_loop(self) -> None:
        psid = self.entry.data.get("fcm_psid")
        if not isinstance(psid, str) or not psid:
            return
        while not self._stop.is_set():
            try:
                removed = await self._remove_subscriptions(psid, set())
            except InvalidAuth:
                self.last_error = "InvalidAuth"
                self.entry.async_start_reauth(self.hass)
                return
            except Exception as error:
                self.last_error = type(error).__name__
                _LOGGER.warning(
                    "Could not remove Firebase alert subscriptions: %s", type(error).__name__
                )
                removed = False
            if removed:
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=RETRY_INTERVAL)
            except TimeoutError:
                pass

    async def _run(self) -> None:
        psid = self.entry.data.get("fcm_psid")
        if not isinstance(psid, str) or not psid:
            psid = secrets.token_hex(8)
            self._save(fcm_psid=psid)
        next_sync = 0.0
        try:
            while not self._stop.is_set():
                delay = HEALTH_CHECK_INTERVAL
                try:
                    if (
                        self.push is None
                        or not self.push.do_listen
                        or not self.push.tasks
                        or any(task.done() for task in self.push.tasks)
                    ):
                        await self._close_push()
                        self.push = FcmPushClient(
                            self._on_alert,
                            FcmRegisterConfig(**FIREBASE_CONFIG),
                            copy.deepcopy(self.entry.data.get("fcm_credentials")),
                            self._save_credentials,
                            http_client_session=async_get_clientsession(self.hass),
                        )
                        token = await self.push.checkin_or_register()
                        await self.push.start()
                        self.last_error = None
                        next_sync = 0.0
                    else:
                        token = self.push.credentials["fcm"]["registration"]["token"]
                    if time.monotonic() >= next_sync:
                        self.subscription_healthy = await self._sync(psid, token)
                        if self.subscription_healthy:
                            self.last_sync = datetime.now(UTC).isoformat()
                        interval = REFRESH_INTERVAL if self.subscription_healthy else RETRY_INTERVAL
                        next_sync = time.monotonic() + interval
                    delay = min(delay, max(1, next_sync - time.monotonic()))
                # Firebase and appliance requests can fail independently.
                except InvalidAuth:
                    self.entry.async_start_reauth(self.hass)
                    return
                except Exception as error:
                    self.last_error = type(error).__name__
                    self.subscription_healthy = False
                    _LOGGER.warning("Firebase alert setup failed: %s", type(error).__name__)
                    delay = RETRY_INTERVAL
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
        finally:
            await self._close_push()

    async def _sync(self, psid: str, token: str) -> bool:
        """Renew our own appliance registrations and subscribe to supported codes."""
        succeeded = await self._remove_subscriptions(psid, set(self.coordinators))
        ids = dict(self.entry.data.get("fcm_registration_ids", {}))
        for device_id, coordinator in self.coordinators.items():
            data = coordinator.data
            if not data:
                succeeded = False
                continue
            if is_dishwasher(data):
                family = 3
            elif is_oven(data):
                family = 2
            elif is_fridge(data) or is_wine(data) or is_ice_maker(data):
                family = 1
            elif appliance_type(data) is None:
                succeeded = False
                continue
            else:
                # Hoods do not have an appliance alert family in the app.
                continue
            desired = {
                code: key for code, key in ALERT_LOCATION_KEYS.items() if code // 100 == family
            }
            if family == 2 and has_left_and_right_ovens(data):
                desired.update(RANGE_ALERT_LOCATION_KEYS)
            previous = self._subscribed.get(device_id)
            if previous == (token, desired):
                continue
            try:
                existing = await self.client.alert_registration(psid, device_id)
                registration_id = (
                    existing["id"] if existing else ids.get(device_id, str(uuid.uuid4()))
                )
                if ids.get(device_id) != registration_id:
                    ids[device_id] = registration_id
                    self._save(fcm_registration_ids=ids)
                registration = {
                    "id": registration_id,
                    "fcmToken": token,
                    "deviceId": device_id,
                    "psid": psid,
                    "mobile_name": "Home Assistant",
                }
                if existing is None or existing.get("fcmToken") != token:
                    try:
                        await self.client.register_for_alerts(
                            registration, update=existing is not None
                        )
                    except ApiError as error:
                        if error.status != 500:
                            raise
                        # The service can answer 500 even when it stored the registration.
                        _LOGGER.debug(
                            "Alert registration returned HTTP 500 for %s",
                            coordinator.device["name"],
                        )
                        try:
                            confirmed = await self.client.alert_registration(psid, device_id)
                        except ApiError:
                            confirmed = None
                        if confirmed is None or confirmed.get("fcmToken") != token:
                            raise error
                current = await self.client.alert_types(psid, device_id)
                changed = {
                    code
                    for code in current & desired.keys()
                    if previous is not None and previous[1].get(code) != desired[code]
                }
                if obsolete := (current - desired.keys()) | changed:
                    await self.client.unsubscribe_alerts(psid, device_id, obsolete)
                if missing := (desired.keys() - current) | changed:
                    await self.client.subscribe_alerts(
                        psid, device_id, {code: desired[code] for code in missing}
                    )
                self._subscribed[device_id] = (token, desired)
            except ApiError as error:
                _LOGGER.warning(
                    "Could not subscribe to alerts for %s: %s", coordinator.device["name"], error
                )
                succeeded = False
        return succeeded

    async def _remove_subscriptions(self, psid: str, retain: set[str]) -> bool:
        """Unsubscribe appliances removed from this entry or all when turned off."""
        ids = dict(self.entry.data.get("fcm_registration_ids", {}))

        def removed(device_id: str) -> None:
            del ids[device_id]
            self._subscribed.pop(device_id, None)
            self._save(fcm_registration_ids=ids)

        return await async_remove_alert_subscriptions(
            self.client, psid, ids.keys() - retain, removed
        )
