"""Authenticated appliance commands and SignalR notifications."""

import asyncio
import base64
import json
import logging
import math
import random
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import aiohttp
from yarl import URL

from .app_config import APP_HEADERS, AUTH_HEADERS
from .auth import CLIENT_ID, SCOPES, TOKEN_URL, InvalidAuth
from .const import (
    MAX_RECONNECT_DELAY,
    RECONNECT_DELAY,
    STATE_KEYS,
    WRITABLE_BOOLEAN_KEYS,
    WRITABLE_INTEGER_KEYS,
)

API_BASE = "https://prod.iot.subzero.com"
SIGNALR_ORIGIN = "https://sznacasigprod.service.signalr.net"
SEPARATOR = "\x1e"
PING_INTERVAL = 15

_LOGGER = logging.getLogger(__name__)


def notification_lifetime(token: str) -> float:
    """Renew independently of the account token, even while pings keep arriving."""
    try:
        claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
        expiry = float(claims["exp"])
        if math.isfinite(expiry):
            return max(1, min(3000, expiry - time.time() - 60))
    except ValueError, TypeError, KeyError, IndexError:
        pass
    return 3000


class ApiError(Exception):
    """The cloud service or appliance could not complete a request."""


class RateLimited(ApiError):
    """Wait before making another request to the service."""

    def __init__(self, retry_after: float):
        self.retry_after = max(1, retry_after)
        super().__init__("Sub-Zero temporarily limited requests.")


@dataclass(frozen=True)
class Appliance:
    id: str
    name: str
    temperature_unit: str | None


@dataclass(frozen=True)
class StateUpdate:
    properties: dict
    full: bool


def token_state(tokens: dict, previous: dict | None = None) -> dict:
    previous = previous or {}
    try:
        access = tokens["access_token"]
        if not isinstance(access, str) or not access:
            raise ValueError
        identity = tokens.get("id_token")
        if identity is not None and (not isinstance(identity, str) or not identity):
            raise ValueError
        bearer = identity or access
        claims = json.loads(base64.urlsafe_b64decode(bearer.split(".")[1] + "=="))
        if not isinstance(claims, dict):
            raise ValueError
        user_id = claims.get("mergedId") or claims["sub"]
        refresh = tokens.get("refresh_token") or previous["refresh_token"]
        expires_at = float(tokens.get("expires_at") or claims["exp"])
        if (
            not isinstance(user_id, str)
            or not user_id
            or not isinstance(refresh, str)
            or not refresh
            or not math.isfinite(expires_at)
        ):
            raise ValueError
    except KeyError, ValueError, IndexError, TypeError:
        raise InvalidAuth("Sub-Zero returned incomplete login tokens.") from None
    if user_id != user_id.lower():
        _LOGGER.debug("Normalizing account ID casing for cloud notifications")
    state = {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": expires_at,
        "user_id": user_id.lower(),
    }
    if identity is not None:
        state["id_token"] = identity
    return state


def retry_delay(value: str | None) -> float:
    if value:
        try:
            delay = float(value)
            return max(1, delay) if math.isfinite(delay) else 600
        except ValueError:
            try:
                return max(1, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
            except TypeError, ValueError, OverflowError:
                pass
    return 600


def _object(value) -> dict:
    layers = 0
    while isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError, RecursionError:
            raise ApiError("Sub-Zero sent an invalid notification.") from None
        layers += 1
    if not isinstance(value, dict):
        raise ApiError("Sub-Zero sent an invalid notification.")
    if layers > 1:
        _LOGGER.debug("Decoded %d JSON string layers", layers)
    return value


def _rejected(response: dict) -> bool:
    """Whether an appliance command result carries a non-zero status."""
    return "status" in response and (type(response["status"]) is not int or response["status"] != 0)


def parse_notification(
    event: dict, user_id: str, device_ids: list[str]
) -> tuple[str, StateUpdate | None] | None:
    """Read state from response, property-change, or root payloads."""
    if event.get("type") != 1 or event.get("target") != "ConnectedApplianceMessage":
        return None
    arguments = event.get("arguments", [])
    if not isinstance(arguments, list):
        raise ApiError("Sub-Zero sent invalid notification arguments.")
    if len(arguments) == 2:
        if not isinstance(arguments[0], str) or arguments[0].lower() != user_id.lower():
            _LOGGER.debug("Ignoring a notification addressed to another account")
            return None
        envelope = _object(arguments[1])
    elif len(arguments) == 1:
        envelope = _object(arguments[0])
    else:
        return None
    device_id = envelope.get("DeviceId")
    if not isinstance(device_id, str) or not device_id:
        raise ApiError("Sub-Zero sent a notification without an appliance.")
    if device_id not in device_ids:
        _LOGGER.debug("Ignoring a notification for an unselected appliance")
        return None
    payload = _object(envelope.get("Payload"))
    if "api.async_channel" not in payload:
        return None
    message = _object(payload["api.async_channel"])
    if message.get("device_id", device_id) != device_id:
        _LOGGER.debug("Ignoring a notification with conflicting appliance IDs")
        return None
    properties = _object(message.get("pload"))
    message_type = message.get("type")
    _LOGGER.debug(
        "Appliance %d notification: type=%s, payload keys=%s",
        device_ids.index(device_id) + 1,
        message_type if isinstance(message_type, int) else "unknown",
        sorted(properties),
    )
    wrapper = "root"
    for key in ("resp", "props"):
        if properties.get(key) is not None:
            properties = _object(properties[key])
            wrapper = key
            break
    _LOGGER.debug(
        "Appliance %d notification: wrapper=%s, state keys=%s",
        device_ids.index(device_id) + 1,
        wrapper,
        sorted(properties),
    )
    if not properties or (wrapper == "root" and properties.keys().isdisjoint(STATE_KEYS)):
        return device_id, None
    model = properties.get("appliance_model")
    full = wrapper != "props" and isinstance(model, str) and bool(model)
    return device_id, StateUpdate(properties, full=full)


class SubZeroClient:
    """One account's API token and connection; passwords are not needed."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        subscription_key: str,
        tokens: dict,
        on_tokens: Callable[[dict], Awaitable[None]] | None = None,
    ):
        self.session = session
        self.subscription_key = subscription_key
        self.tokens = token_state(tokens)
        if "id_token" not in self.tokens:
            # Refresh existing logins once to obtain an ID token.
            self.tokens["expires_at"] = 0
        self.on_tokens = on_tokens
        self._refresh_lock = asyncio.Lock()
        self._retry_at = 0.0
        self._state_commands: dict[str, str] = {}
        self.push_connected = False
        self.notification_stats: dict[str, int | str | None] = {
            "received": 0,
            "ignored": 0,
            "invalid": 0,
            "last_received": None,
        }

    @property
    def _api_token(self) -> str:
        return self.tokens.get("id_token", self.tokens["access_token"])

    async def _json(
        self,
        method: str,
        url: str | URL,
        *,
        token_request=False,
        appliance_command=False,
        headers: dict[str, str] | None = None,
        **kwargs,
    ) -> dict:
        if self._retry_at > time.monotonic():
            raise RateLimited(self._retry_at - time.monotonic())
        try:
            async with self.session.request(
                method,
                url,
                headers={**(AUTH_HEADERS if token_request else APP_HEADERS), **(headers or {})},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=40),
                **kwargs,
            ) as response:
                if response.status == 429:
                    delay = retry_delay(response.headers.get("Retry-After"))
                    self._retry_at = time.monotonic() + delay
                    raise RateLimited(delay)
                if response.status in (401, 403) or (token_request and response.status == 400):
                    raise InvalidAuth("Sub-Zero requires a new sign-in.")
                if appliance_command and response.status in (200, 500):
                    body = (await response.text()).strip()
                    if response.status == 200 and body in ("", "OK", '"OK"'):
                        return {}
                    try:
                        result = json.loads(body)
                    except ValueError:
                        result = None
                    if (
                        response.status == 500
                        and isinstance(result, dict)
                        and result.get("Message", result.get("message")) == "OK"
                    ):
                        return {}
                    if response.status == 200:
                        return _object(result)
                if response.status != 200:
                    raise ApiError(f"Sub-Zero returned HTTP {response.status}.")
                try:
                    return _object(await response.json(content_type=None))
                except ValueError:
                    raise ApiError("Sub-Zero returned an invalid API response.") from None
        except aiohttp.ClientError, TimeoutError:
            raise ApiError("Could not connect to Sub-Zero.") from None

    async def refresh(self, *, rejected_token: str | None = None) -> None:
        async with self._refresh_lock:
            if self.tokens["expires_at"] > time.time() + 300 and self._api_token != rejected_token:
                return
            data = await self._json(
                "POST",
                TOKEN_URL,
                token_request=True,
                data={
                    "grant_type": "refresh_token",
                    "client_id": CLIENT_ID,
                    "refresh_token": self.tokens["refresh_token"],
                    "scope": SCOPES,
                },
            )
            updated = token_state(data, self.tokens)
            if updated["user_id"].lower() != self.tokens["user_id"].lower():
                raise InvalidAuth("Sub-Zero refreshed a different account.")
            self.tokens = updated
            if self.on_tokens:
                await self.on_tokens(dict(updated))

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        await self.refresh()
        token = self._api_token
        headers = {
            "Authorization": "Bearer " + token,
            "Ocp-Apim-Subscription-Key": self.subscription_key,
            "Userid": self.tokens["user_id"],
            "Accept": "application/json",
        }
        try:
            return await self._json(method, API_BASE + path, headers=headers, **kwargs)
        except InvalidAuth:
            await self.refresh(rejected_token=token)
        headers["Authorization"] = "Bearer " + self._api_token
        headers["Userid"] = self.tokens["user_id"]
        return await self._json(method, API_BASE + path, headers=headers, **kwargs)

    async def appliances(self) -> list[Appliance]:
        data = await self._request("GET", "/consumerapp/user/devices")
        if not isinstance(data.get("devices"), list):
            raise ApiError("Sub-Zero did not return the appliance list.")
        account_id = data.get("mySubZeroUniqueUserId")
        if isinstance(account_id, str) and account_id.lower() != self.tokens["user_id"].lower():
            _LOGGER.debug("The appliance list names a different account id than the sign-in")
        appliances = []
        for device in data["devices"]:
            if (
                not isinstance(device, dict)
                or not isinstance(device.get("id"), str)
                or not device["id"]
                or not isinstance(device.get("name") or "Sub-Zero", str)
            ):
                raise ApiError("Sub-Zero returned an invalid appliance list.")
            appliances.append(
                Appliance(
                    device["id"],
                    device.get("name") or "Sub-Zero",
                    device.get("temperatureUnitForAppliance"),
                )
            )
        return appliances

    async def _command(self, device_id: str, command: str, params: dict | None = None) -> dict:
        if command not in {"get", "get_async", "open_cloud_async", "set"}:
            raise ValueError("Unsupported appliance command")
        payload = {"cmd": command}
        if params is not None:
            payload["params"] = params
        path = "/consumerapp/device/" + quote(device_id, safe="") + "/directmethod/executeAPICmd"
        return await self._request(
            "POST",
            path,
            json={"req_id": str(uuid.uuid4()), "pload": payload},
            appliance_command=True,
        )

    async def state(self, device_id: str) -> dict:
        command = self._state_commands.get(device_id, "get")
        data = await self._command(device_id, command)
        if not data and command == "get":
            command = "get_async"
            data = await self._command(device_id, command)
        if _rejected(data):
            raise ApiError("The appliance rejected the status request.")
        data = _object(data.get("resp", data))
        if not isinstance(data.get("appliance_model"), str):
            raise ApiError("The appliance did not return a status snapshot.")
        self._state_commands[device_id] = command
        return data

    async def open_channel(self, device_id: str) -> None:
        response = await self._command(device_id, "open_cloud_async")
        if _rejected(response) or _rejected(_object(response.get("resp", {}))):
            raise ApiError("The appliance rejected opening its update channel.")

    async def set_property(self, device_id: str, key: str, value: bool | int) -> None:
        if not (
            key in WRITABLE_BOOLEAN_KEYS
            and type(value) is bool
            or key in WRITABLE_INTEGER_KEYS
            and type(value) is int
        ):
            raise ValueError("Unsupported setting or value type")
        response = await self._command(device_id, "set", {key: value})
        if _rejected(response):
            raise ApiError("Sub-Zero rejected the setting.")
        response = _object(response.get("resp", response))
        if response and (type(response.get("status")) is not int or response["status"] != 0):
            raise ApiError("Sub-Zero rejected the setting.")

    async def watch(
        self, device_ids: list[str]
    ) -> AsyncIterator[tuple[str, StateUpdate | ApiError]]:
        while True:
            try:
                async with aclosing(self._watch_connection(device_ids)) as updates:
                    async for event in updates:
                        yield event
            finally:
                self.push_connected = False

    async def _watch_connection(
        self, device_ids: list[str]
    ) -> AsyncIterator[tuple[str, StateUpdate | ApiError]]:
        info = await self._request(
            "POST", "/signal-r/negotiateUser", params={"userId": self.tokens["user_id"]}
        )
        try:
            endpoint = URL(info["url"])
            access_token = info["accessToken"]
            if endpoint.origin() != URL(SIGNALR_ORIGIN) or not isinstance(access_token, str):
                raise ValueError
        except KeyError, TypeError, ValueError:
            raise ApiError("Sub-Zero returned an unexpected notification endpoint.") from None
        headers = {"Authorization": "Bearer " + access_token}
        renew_at = time.monotonic() + notification_lifetime(access_token)
        negotiate = endpoint.with_path(
            endpoint.path.rstrip("/") + "/negotiate", keep_query=True
        ).update_query(negotiateVersion=1)
        transport = await self._json(
            "POST",
            negotiate,
            headers={
                **headers,
                "X-Requested-With": "FlutterHttpClient",
                "Content-Type": "text/plain;charset=UTF-8",
            },
        )
        connection = transport.get("connectionToken") or transport.get("connectionId")
        if not connection:
            raise ApiError("Sub-Zero did not open a notification connection.")
        ws_url = endpoint.with_scheme("wss" if endpoint.scheme == "https" else "ws").update_query(
            id=connection
        )
        try:
            async with self.session.ws_connect(
                ws_url, headers={**APP_HEADERS, **headers}, max_msg_size=262144
            ) as websocket:
                await websocket.send_str('{"protocol":"json","version":1}' + SEPARATOR)
                first = await websocket.receive(timeout=20)
                if first.type != aiohttp.WSMsgType.TEXT or SEPARATOR not in first.data:
                    raise ApiError("Sub-Zero did not complete the notification handshake.")
                handshake, pending = first.data.split(SEPARATOR, 1)
                if _object(handshake) != {}:
                    raise ApiError("Sub-Zero rejected the notification handshake.")
                self.push_connected = True
                errors: deque[tuple[str, ApiError]] = deque()
                pending_channels = set(device_ids)

                async def open_channels() -> None:
                    backoff = RECONNECT_DELAY
                    while pending_channels:
                        for index, device_id in enumerate(device_ids, 1):
                            if device_id not in pending_channels:
                                continue
                            _LOGGER.debug("Opening update channel for appliance %d", index)
                            try:
                                await self.open_channel(device_id)
                            except RateLimited:
                                raise
                            except ApiError as error:
                                if device_id in pending_channels:
                                    errors.append((device_id, error))
                            else:
                                _LOGGER.debug("Update channel accepted for appliance %d", index)
                                pending_channels.discard(device_id)
                        if pending_channels:
                            await asyncio.sleep(backoff + random.uniform(0, 5))
                            backoff = min(backoff * 2, MAX_RECONNECT_DELAY)

                opening = asyncio.create_task(open_channels())
                receiving = asyncio.create_task(websocket.receive())
                tasks = {opening, receiving}
                try:
                    next_ping = time.monotonic() + PING_INTERVAL
                    last_received = time.monotonic()
                    while True:
                        while errors:
                            yield errors.popleft()
                        while SEPARATOR in pending:
                            frame, pending = pending.split(SEPARATOR, 1)
                            if not frame:
                                continue
                            try:
                                event = _object(frame)
                            except ApiError as error:
                                self.notification_stats["invalid"] += 1
                                _LOGGER.debug("Skipping invalid notification frame: %s", error)
                                continue
                            if event.get("type") == 7:
                                raise ApiError("Sub-Zero closed the notification connection.")
                            if (
                                event.get("type") != 1
                                or event.get("target") != "ConnectedApplianceMessage"
                            ):
                                continue
                            self.notification_stats["received"] += 1
                            self.notification_stats["last_received"] = datetime.now(UTC).isoformat()
                            try:
                                parsed = parse_notification(
                                    event, self.tokens["user_id"], device_ids
                                )
                            except ApiError as error:
                                self.notification_stats["invalid"] += 1
                                _LOGGER.debug("Skipping invalid appliance notification: %s", error)
                                continue
                            if parsed is None or parsed[1] is None:
                                self.notification_stats["ignored"] += 1
                                continue
                            device_id, update = parsed
                            if update.full:
                                pending_channels.discard(device_id)
                            yield device_id, update
                        now = time.monotonic()
                        if now >= renew_at:
                            return
                        if now - last_received > 60:
                            raise ApiError("Sub-Zero's notification connection stopped responding.")
                        if now >= next_ping:
                            await websocket.send_str('{"type":6}' + SEPARATOR)
                            next_ping = now + PING_INTERVAL
                        await asyncio.wait(
                            tasks,
                            timeout=max(0.1, min(next_ping, renew_at) - time.monotonic()),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if opening.done():
                            opening.result()
                            tasks.discard(opening)
                        if not receiving.done():
                            continue
                        message = receiving.result()
                        if message.type != aiohttp.WSMsgType.TEXT:
                            raise ApiError("Sub-Zero's notification connection disconnected.")
                        tasks.remove(receiving)
                        receiving = asyncio.create_task(websocket.receive())
                        tasks.add(receiving)
                        last_received = time.monotonic()
                        pending += message.data
                        if len(pending) > 262144:
                            raise ApiError("Sub-Zero sent an oversized notification.")
                finally:
                    opening.cancel()
                    receiving.cancel()
                    await asyncio.gather(opening, receiving, return_exceptions=True)
        except aiohttp.ClientError, TimeoutError:
            raise ApiError("Could not maintain Sub-Zero's notification connection.") from None
