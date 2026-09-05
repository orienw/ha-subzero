"""Authenticated appliance reads and SignalR notifications."""

import asyncio
import base64
import json
import math
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import aiohttp
from yarl import URL

from .auth import CLIENT_ID, SCOPES, TOKEN_URL, InvalidAuth

API_BASE = "https://prod.iot.subzero.com"
SIGNALR_ORIGIN = "https://sznacasigprod.service.signalr.net"
SEPARATOR = "\x1e"


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
    appliance_type: str


@dataclass(frozen=True)
class StateUpdate:
    properties: dict
    full: bool


def token_state(tokens: dict, previous: dict | None = None) -> dict:
    previous = previous or {}
    try:
        access = tokens["access_token"]
        claims = json.loads(base64.urlsafe_b64decode(access.split(".")[1] + "=="))
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
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": expires_at,
        "user_id": user_id,
    }


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
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ApiError("Sub-Zero sent an invalid notification.") from None
    if not isinstance(value, dict):
        raise ApiError("Sub-Zero sent an invalid notification.")
    return value


def parse_notification(event: dict, device_id: str, user_id: str) -> StateUpdate | None:
    if event.get("type") != 1 or event.get("target") != "ConnectedApplianceMessage":
        return None
    arguments = event.get("arguments", [])
    if not isinstance(arguments, list):
        raise ApiError("Sub-Zero sent invalid notification arguments.")
    if len(arguments) == 2:
        if not isinstance(arguments[0], str) or arguments[0].lower() != user_id.lower():
            return None
        envelope = _object(arguments[1])
    elif len(arguments) == 1:
        envelope = _object(arguments[0])
    else:
        return None
    if envelope.get("DeviceId") != device_id:
        return None
    payload = _object(envelope.get("Payload"))
    if "api.async_channel" not in payload:
        return None
    message = _object(payload["api.async_channel"])
    if message.get("device_id", device_id) != device_id:
        return None
    properties = _object(message.get("pload"))
    if message.get("type") == 1:
        if not properties.get("appliance_model"):
            raise ApiError("Sub-Zero sent an incomplete appliance snapshot.")
        return StateUpdate(properties, full=True)
    if message.get("type") == 2:
        return StateUpdate(_object(properties.get("props")), full=False)
    return None


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
        self.on_tokens = on_tokens
        self._refresh_lock = asyncio.Lock()
        self._retry_at = 0.0

    async def _json(self, method: str, url: str | URL, *, token_request=False, **kwargs) -> dict:
        if self._retry_at > time.monotonic():
            raise RateLimited(self._retry_at - time.monotonic())
        try:
            async with self.session.request(
                method,
                url,
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
                if response.status != 200:
                    raise ApiError(f"Sub-Zero returned HTTP {response.status}.")
                try:
                    return _object(await response.json(content_type=None))
                except ValueError:
                    raise ApiError("Sub-Zero returned an invalid API response.") from None
        except aiohttp.ClientError, TimeoutError:
            raise ApiError("Could not connect to Sub-Zero.") from None

    async def refresh(self, *, force=False) -> None:
        previous_access = self.tokens["access_token"]
        async with self._refresh_lock:
            if self.tokens["expires_at"] > time.time() + 120:
                if not force or self.tokens["access_token"] != previous_access:
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
        for attempt in range(2):
            headers = {
                "Authorization": "Bearer " + self.tokens["access_token"],
                "Ocp-Apim-Subscription-Key": self.subscription_key,
                "Userid": self.tokens["user_id"],
                "Accept": "application/json",
            }
            try:
                return await self._json(method, API_BASE + path, headers=headers, **kwargs)
            except InvalidAuth:
                if attempt:
                    raise
                await self.refresh(force=True)
        raise InvalidAuth("Sub-Zero requires a new sign-in.")

    async def appliances(self) -> list[Appliance]:
        data = await self._request("GET", "/consumerapp/user/devices")
        if not isinstance(data.get("devices"), list):
            raise ApiError("Sub-Zero did not return the appliance list.")
        try:
            return [
                Appliance(
                    d["id"],
                    d.get("name") or "Sub-Zero",
                    d.get("temperatureUnitForAppliance"),
                    d.get("applianceId", ""),
                )
                for d in data["devices"]
            ]
        except KeyError, TypeError:
            raise ApiError("Sub-Zero returned an invalid appliance list.") from None

    async def _read_command(self, device_id: str, command: str) -> dict:
        if command not in {"get", "open_cloud_async"}:
            raise ValueError("Unsupported read command")
        path = "/consumerapp/device/" + quote(device_id, safe="") + "/directmethod/executeAPICmd"
        return await self._request(
            "POST", path, json={"req_id": str(uuid.uuid4()), "pload": {"cmd": command}}
        )

    async def state(self, device_id: str) -> dict:
        data = await self._read_command(device_id, "get")
        if not isinstance(data.get("appliance_model"), str):
            raise ApiError("The appliance did not return a status snapshot.")
        return data

    async def open_channel(self, device_id: str) -> None:
        await self._read_command(device_id, "open_cloud_async")

    async def watch(
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
        negotiate = endpoint.with_path(
            endpoint.path.rstrip("/") + "/negotiate", keep_query=True
        ).update_query(negotiateVersion=1)
        transport = await self._json("POST", negotiate, headers=headers)
        connection = transport.get("connectionToken") or transport.get("connectionId")
        if not connection:
            raise ApiError("Sub-Zero did not open a notification connection.")
        ws_url = endpoint.with_scheme("wss" if endpoint.scheme == "https" else "ws").update_query(
            id=connection
        )
        try:
            async with self.session.ws_connect(
                ws_url, headers=headers, max_msg_size=262144
            ) as websocket:
                await websocket.send_str('{"protocol":"json","version":1}' + SEPARATOR)
                first = await websocket.receive(timeout=20)
                if first.type != aiohttp.WSMsgType.TEXT or SEPARATOR not in first.data:
                    raise ApiError("Sub-Zero did not complete the notification handshake.")
                handshake, pending = first.data.split(SEPARATOR, 1)
                if _object(handshake) != {}:
                    raise ApiError("Sub-Zero rejected the notification handshake.")
                for device_id in device_ids:
                    try:
                        await self.open_channel(device_id)
                    except RateLimited:
                        raise
                    except ApiError as error:
                        yield device_id, error
                next_ping = time.monotonic() + 15
                last_received = time.monotonic()
                while True:
                    while SEPARATOR in pending:
                        frame, pending = pending.split(SEPARATOR, 1)
                        if not frame:
                            continue
                        event = _object(frame)
                        if event.get("type") == 7:
                            raise ApiError("Sub-Zero closed the notification connection.")
                        for device_id in device_ids:
                            update = parse_notification(event, device_id, self.tokens["user_id"])
                            if update:
                                yield device_id, update
                                break
                    now = time.monotonic()
                    if now - last_received > 60:
                        raise ApiError("Sub-Zero's notification connection stopped responding.")
                    if now >= next_ping:
                        await websocket.send_str('{"type":6}' + SEPARATOR)
                        next_ping = now + 15
                    try:
                        message = await websocket.receive(
                            timeout=max(0.1, next_ping - time.monotonic())
                        )
                    except TimeoutError:
                        continue
                    if message.type != aiohttp.WSMsgType.TEXT:
                        raise ApiError("Sub-Zero's notification connection disconnected.")
                    last_received = time.monotonic()
                    pending += message.data
                    if len(pending) > 262144:
                        raise ApiError("Sub-Zero sent an oversized notification.")
        except aiohttp.ClientError, TimeoutError:
            raise ApiError("Could not maintain Sub-Zero's notification connection.") from None
