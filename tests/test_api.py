"""Cloud failures, token rotation and both observed SignalR envelope versions."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp import web
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.subzero import api
from custom_components.subzero.auth import InvalidAuth

from .conftest import make_tokens


def notification(properties, *, full=False, device="test-fridge", user=None):
    envelope = {
        "DeviceId": device,
        "Payload": json.dumps(
            {
                "api.async_channel": json.dumps(
                    {
                        "type": 1 if full else 2,
                        "pload": properties if full else {"props": properties},
                    }
                )
            }
        ),
    }
    arguments = [json.dumps(envelope)]
    if user is not None:
        arguments.insert(0, user)
    return {"type": 1, "target": "ConnectedApplianceMessage", "arguments": arguments}


@pytest.mark.parametrize("legacy", [False, True])
def test_notifications_preserve_false_and_zero(legacy):
    properties = {"ref_door_ajar": False, "frz_set_temp": 0}
    event = notification(properties, user="TEST-OWNER" if legacy else None)
    assert api.parse_notification(event, "test-fridge", "test-owner") == api.StateUpdate(
        properties, full=False
    )


def test_notifications_do_not_mix_devices_or_accounts():
    assert api.parse_notification(notification({}, device="other"), "test-fridge", "owner") is None
    assert api.parse_notification(notification({}, user="other"), "test-fridge", "owner") is None


def test_any_model_snapshot_is_accepted():
    state = {"appliance_model": "ANOTHER-MODEL", "ref_door_ajar": True}
    assert api.parse_notification(notification(state, full=True), "test-fridge", "owner") == (
        api.StateUpdate(state, full=True)
    )


@pytest.mark.parametrize("value", [None, "bad", "nan", "inf"])
def test_invalid_retry_after_uses_finite_fallback(value):
    assert api.retry_delay(value) == 600


def test_retry_after_http_date():
    deadline = datetime.now(UTC) + timedelta(seconds=300)
    assert 298 <= api.retry_delay(format_datetime(deadline, usegmt=True)) <= 300


@pytest.fixture
async def api_server(aiohttp_server, monkeypatch, socket_enabled):
    behavior = {"requests": [], "status": 200, "refreshes": 0, "user_id": "test-owner"}

    async def token(request):
        assert request.headers.getall("User-Agent") == [
            "Dalvik/2.1.0 (Linux; U; Android 16; Pixel 9 Build/BP2A.250605.031.A2)"
        ]
        assert request.headers["Accept"] == "application/json"
        assert request.headers["Accept-Encoding"] == "gzip"
        assert request.content_type == "application/x-www-form-urlencoded"
        assert not {"Authorization", "Userid", "Ocp-Apim-Subscription-Key"} & request.headers.keys()
        behavior["refreshes"] += 1
        data = await request.post()
        assert data["grant_type"] == "refresh_token"
        return web.json_response(
            make_tokens(behavior["user_id"], refresh_token="rotated-refresh", expires_in=7200)
        )

    async def devices(request):
        assert request.headers.getall("User-Agent") == ["Dart/3.11 (dart:io)"]
        assert request.headers["Accept-Encoding"] == "gzip"
        assert request.headers["Userid"] == "test-owner"
        assert request.headers["Ocp-Apim-Subscription-Key"] == "test-key"
        behavior["requests"].append(request.headers.get("Authorization"))
        if "response" in behavior:
            return web.json_response(behavior["response"])
        status = behavior["status"]
        if status == 429:
            return web.json_response({}, status=429, headers={"Retry-After": "300"})
        if status == 401 and behavior["refreshes"] == 0:
            return web.json_response({}, status=401)
        return web.json_response(
            {
                "devices": [
                    {
                        "id": "test-fridge",
                        "name": "Kitchen",
                        "applianceId": "99.1.2.3",
                        "temperatureUnitForAppliance": "F",
                        "pin": "never-persist-this",
                    }
                ]
            }
        )

    app = web.Application()
    app.router.add_post("/token", token)
    app.router.add_get("/consumerapp/user/devices", devices)
    server = await aiohttp_server(app)
    origin = str(server.make_url("/")).rstrip("/")
    monkeypatch.setattr(api, "API_BASE", origin)
    monkeypatch.setattr(api, "TOKEN_URL", origin + "/token")
    return behavior


async def test_concurrent_refresh_rotates_once_and_persists(api_server):
    save = AsyncMock()
    async with aiohttp.ClientSession() as session:
        client = api.SubZeroClient(session, "test-key", make_tokens(expires_in=-1), save)
        await asyncio.gather(client.refresh(), client.refresh(), client.refresh())
    assert api_server["refreshes"] == 1
    save.assert_awaited_once_with(client.tokens)
    assert client.tokens["refresh_token"] == "rotated-refresh"


async def test_refresh_rejects_changed_account(api_server):
    api_server["user_id"] = "another-owner"
    save = AsyncMock()
    async with aiohttp.ClientSession() as session:
        client = api.SubZeroClient(session, "test-key", make_tokens(expires_in=-1), save)
        with pytest.raises(InvalidAuth):
            await client.refresh()
    save.assert_not_awaited()


async def test_unauthorized_request_refreshes_then_retries_once(api_server, tokens):
    api_server["status"] = 401
    async with aiohttp.ClientSession() as session:
        client = api.SubZeroClient(session, "test-key", tokens)
        appliances = await client.appliances()
    assert api_server["refreshes"] == 1
    assert len(api_server["requests"]) == 2
    assert api_server["requests"][0] != api_server["requests"][1]
    assert appliances == [api.Appliance("test-fridge", "Kitchen", "F", "99.1.2.3")]
    assert not hasattr(appliances[0], "pin")


async def test_late_unauthorized_response_reuses_already_refreshed_token(api_server, tokens):
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = []
    async with aiohttp.ClientSession() as session:
        client = api.SubZeroClient(session, "test-key", tokens)
        request = client._json

        async def delayed_request(method, url, **kwargs):
            if url == api.API_BASE + "/consumerapp/user/devices":
                access = kwargs["headers"]["Authorization"]
                calls.append(access)
                if access == "Bearer " + tokens["access_token"]:
                    if len(calls) == 1:
                        first_started.set()
                        await release_first.wait()
                    raise InvalidAuth("Expired")
            return await request(method, url, **kwargs)

        client._json = delayed_request
        first = asyncio.create_task(client.appliances())
        await first_started.wait()
        try:
            assert await client.appliances()
        finally:
            release_first.set()
            assert await first
    assert api_server["refreshes"] == 1
    assert len(calls) == 4
    assert calls[2] == calls[3]


@pytest.mark.parametrize("access", [None, 123, {}, "not-a-token"])
def test_invalid_access_tokens_report_authentication_failure(tokens, access):
    with pytest.raises(InvalidAuth):
        api.token_state({**tokens, "access_token": access})


@pytest.mark.parametrize(
    "device", [None, {}, {"id": None}, {"id": 123}, {"id": ""}, {"id": "test", "name": 123}]
)
async def test_invalid_appliance_list_reports_connection_failure(api_server, tokens, device):
    api_server["response"] = {"devices": [device]}
    async with aiohttp.ClientSession() as session:
        with pytest.raises(api.ApiError, match="invalid appliance list"):
            await api.SubZeroClient(session, "test-key", tokens).appliances()


async def test_requests_override_shared_home_assistant_headers(hass, api_server, tokens):
    api_server["status"] = 401
    session = async_get_clientsession(hass)
    original_headers = dict(session.headers)
    client = api.SubZeroClient(session, "test-key", tokens)
    assert await client.appliances()
    assert api_server["refreshes"] == 1
    assert len(api_server["requests"]) == 2
    assert dict(session.headers) == original_headers


async def test_rate_limit_blocks_followup_requests(api_server, tokens):
    api_server["status"] = 429
    async with aiohttp.ClientSession() as session:
        client = api.SubZeroClient(session, "test-key", tokens)
        for _ in range(3):
            with pytest.raises(api.RateLimited) as error:
                await client.appliances()
            assert 298 <= error.value.retry_after <= 300
    assert len(api_server["requests"]) == 1
    assert api_server["refreshes"] == 0


@pytest.mark.parametrize("offline", [False, True, "recover"])
@pytest.mark.parametrize("slow_open", [False, True])
async def test_single_signalr_connection_routes_multiple_appliances(
    hass, aiohttp_server, monkeypatch, socket_enabled, tokens, offline, slow_open
):
    sockets = []
    opened = []
    recovered = asyncio.Event()
    release_open = asyncio.Event()
    first_update = asyncio.Event()
    heartbeat = asyncio.Event()

    async def negotiate_user(request):
        assert request.headers.getall("User-Agent") == ["Dart/3.11 (dart:io)"]
        assert request.headers["Accept-Encoding"] == "gzip"
        assert request.headers["Ocp-Apim-Subscription-Key"] == "test-key"
        assert request.query["userId"] == "test-owner"
        return web.json_response(
            {
                "url": origin + "/client/?hub=connectedappliances",
                "accessToken": "test-signalr-token",
            }
        )

    async def negotiate_transport(request):
        assert request.headers.getall("User-Agent") == ["Dart/3.11 (dart:io)"]
        assert request.headers["Accept-Encoding"] == "gzip"
        assert request.headers["Authorization"] == "Bearer test-signalr-token"
        assert request.headers["X-Requested-With"] == "FlutterHttpClient"
        assert request.headers["Content-Type"] == "text/plain;charset=UTF-8"
        assert not {"Userid", "Ocp-Apim-Subscription-Key"} & request.headers.keys()
        assert request.query == {"hub": "connectedappliances", "negotiateVersion": "1"}
        return web.json_response({"connectionToken": "connection"})

    async def websocket(request):
        assert request.headers.getall("User-Agent") == ["Dart/3.11 (dart:io)"]
        assert request.headers["Accept-Encoding"] == "gzip"
        assert request.headers["Authorization"] == "Bearer test-signalr-token"
        assert "X-Requested-With" not in request.headers
        assert not {"Userid", "Ocp-Apim-Subscription-Key"} & request.headers.keys()
        assert request.query == {"hub": "connectedappliances", "id": "connection"}
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        sockets.append(socket)
        assert json.loads((await socket.receive_str()).rstrip(api.SEPARATOR)) == {
            "protocol": "json",
            "version": 1,
        }
        await socket.send_str("{}" + api.SEPARATOR)
        async for message in socket:
            if message.type == aiohttp.WSMsgType.TEXT:
                assert json.loads(message.data.rstrip(api.SEPARATOR)) == {"type": 6}
                heartbeat.set()
        return socket

    async def command(request):
        assert request.headers.getall("User-Agent") == ["Dart/3.11 (dart:io)"]
        device_id = request.match_info["device_id"]
        assert (await request.json())["pload"]["cmd"] == "open_cloud_async"
        opened.append(device_id)
        if slow_open and device_id == "test-oven":
            await release_open.wait()
        if (
            offline
            and device_id == "test-oven"
            and (offline != "recover" or opened.count(device_id) == 1)
        ):
            return web.json_response({}, status=503)
        event = notification(
            {"appliance_model": "ANY-MODEL", "service_required": False}, full=True, device=device_id
        )
        unrelated = notification({"unit_on": True}, device="other-owner-device")
        await sockets[0].send_str(
            json.dumps(unrelated) + api.SEPARATOR + json.dumps(event) + api.SEPARATOR
        )
        return web.json_response({})

    app = web.Application()
    app.router.add_post("/signal-r/negotiateUser", negotiate_user)
    app.router.add_post("/client/negotiate", negotiate_transport)
    app.router.add_get("/client/", websocket)
    app.router.add_post("/consumerapp/device/{device_id}/directmethod/executeAPICmd", command)
    server = await aiohttp_server(app)
    origin = str(server.make_url("/")).rstrip("/")
    monkeypatch.setattr(api, "API_BASE", origin)
    monkeypatch.setattr(api, "SIGNALR_ORIGIN", origin)
    monkeypatch.setattr(api, "PING_INTERVAL", 0.01)
    monkeypatch.setattr(api, "RECONNECT_DELAY", 0.2)
    monkeypatch.setattr("random.uniform", lambda *_: 0)
    received = {}
    session = async_get_clientsession(hass)
    original_headers = dict(session.headers)
    stream = api.SubZeroClient(session, "test-key", tokens).watch(["test-fridge", "test-oven"])

    async def collect():
        try:
            async for device_id, update in stream:
                received[device_id] = update
                first_update.set()
                if device_id == "test-oven" and isinstance(update, api.StateUpdate):
                    recovered.set()
                if len(received) == 2 and (offline != "recover" or recovered.is_set()):
                    break
        finally:
            await stream.aclose()

    collector = asyncio.create_task(collect())
    try:
        async with asyncio.timeout(5):
            if slow_open:
                await first_update.wait()
                await heartbeat.wait()
                release_open.set()
            await collector
    finally:
        release_open.set()
        collector.cancel()
        await asyncio.gather(collector, return_exceptions=True)
    assert dict(session.headers) == original_headers
    assert len(sockets) == 1
    assert opened == ["test-fridge", "test-oven"] + (["test-oven"] if offline == "recover" else [])
    assert isinstance(received["test-fridge"], api.StateUpdate)
    assert isinstance(received["test-oven"], api.ApiError if offline is True else api.StateUpdate)


@pytest.mark.parametrize("error", [None, InvalidAuth("Expired"), api.RateLimited(300)])
async def test_signalr_cleans_up_pending_tasks_and_preserves_channel_errors(tokens, error):
    opening = asyncio.Event()
    receiving = asyncio.Event()
    channel_cancelled = asyncio.Event()
    receive_cancelled = asyncio.Event()
    release = asyncio.Event()
    frames = iter(
        [
            "{}" + api.SEPARATOR,
            json.dumps(notification({"ref_door_ajar": True})) + api.SEPARATOR,
        ]
    )

    async def receive(**kwargs):
        if (frame := next(frames, None)) is not None:
            return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, frame, "")
        receiving.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            receive_cancelled.set()
            raise
        return aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, None, "")

    async def open_channel(device_id):
        opening.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            channel_cancelled.set()
            raise
        if error is not None:
            raise error

    socket = AsyncMock()
    socket.receive.side_effect = receive
    session = MagicMock()
    session.ws_connect.return_value.__aenter__.return_value = socket
    client = api.SubZeroClient(session, "test-key", tokens)
    client._request = AsyncMock(
        return_value={"url": api.SIGNALR_ORIGIN + "/client/", "accessToken": "test-signalr"}
    )
    client._json = AsyncMock(return_value={"connectionToken": "test-connection"})
    client.open_channel = open_channel
    stream = client.watch(["test-fridge"])
    try:
        assert await anext(stream) == (
            "test-fridge",
            api.StateUpdate({"ref_door_ajar": True}, full=False),
        )
        await opening.wait()
        await receiving.wait()
        if error is not None:
            release.set()
            with pytest.raises(type(error)) as caught:
                await anext(stream)
            assert caught.value is error
    finally:
        await stream.aclose()
    if error is None:
        assert channel_cancelled.is_set()
        assert receive_cancelled.is_set()
    session.ws_connect.return_value.__aexit__.assert_awaited_once()


@pytest.fixture
async def control_server(aiohttp_server, monkeypatch, socket_enabled):
    behavior = {"requests": [], "status": 200, "response": {}}

    async def command(request):
        assert request.headers.getall("User-Agent") == ["Dart/3.11 (dart:io)"]
        assert request.headers["Accept-Encoding"] == "gzip"
        assert request.match_info["device_id"] == "test-fridge"
        assert request.headers["Userid"] == "test-owner"
        assert request.headers["Ocp-Apim-Subscription-Key"] == "test-key"
        body = await request.json()
        behavior["requests"].append(body)
        return web.json_response(
            behavior["response"], status=behavior["status"], headers={"Retry-After": "300"}
        )

    app = web.Application()
    app.router.add_post("/consumerapp/device/{device_id}/directmethod/executeAPICmd", command)
    server = await aiohttp_server(app)
    monkeypatch.setattr(api, "API_BASE", str(server.make_url("/")).rstrip("/"))
    return behavior


async def test_control_writes_use_the_authenticated_app_envelope(hass, control_server, tokens):
    session = async_get_clientsession(hass)
    original_headers = dict(session.headers)
    client = api.SubZeroClient(session, "test-key", tokens)
    await client.set_property("test-fridge", "night_ice_on", True)
    await client.set_property("test-fridge", "ref_set_temp", 39)
    assert dict(session.headers) == original_headers
    first, second = control_server["requests"]
    assert first["pload"] == {"cmd": "set", "params": {"night_ice_on": True}}
    assert second["pload"] == {"cmd": "set", "params": {"ref_set_temp": 39}}
    assert first["req_id"] != second["req_id"]
    assert set(first) == {"req_id", "pload"}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("", False),
        ("unit_on", False),
        ("cav_light_on", True),
        ("remote_svc_reg_token", "private"),
        ("air_filter_on", 1),
        ("ref_set_temp", True),
        ("ref_set_temp", "38"),
        ("ref_set_temp", float("nan")),
    ],
)
async def test_invalid_control_fields_never_send_a_request(control_server, tokens, key, value):
    async with aiohttp.ClientSession() as session:
        client = api.SubZeroClient(session, "test-key", tokens)
        with pytest.raises(ValueError):
            await client.set_property("test-fridge", key, value)
    assert not control_server["requests"]


@pytest.mark.parametrize(
    "response", [{"status": 1}, {"status": False}, {"error": "private detail"}]
)
async def test_control_rejection_in_successful_http_response_is_an_error(
    control_server, tokens, response
):
    control_server["response"] = response
    async with aiohttp.ClientSession() as session:
        client = api.SubZeroClient(session, "test-key", tokens)
        with pytest.raises(api.ApiError, match="rejected the setting") as error:
            await client.set_property("test-fridge", "air_filter_on", False)
    assert "private detail" not in str(error.value)
    assert len(control_server["requests"]) == 1


async def test_control_rate_limit_blocks_subsequent_commands(control_server, tokens):
    control_server["status"] = 429
    async with aiohttp.ClientSession() as session:
        client = api.SubZeroClient(session, "test-key", tokens)
        for _ in range(2):
            with pytest.raises(api.RateLimited):
                await client.set_property("test-fridge", "air_filter_on", False)
    assert len(control_server["requests"]) == 1


async def test_failed_control_request_is_not_automatically_retried(control_server, tokens):
    control_server["status"] = 503
    async with aiohttp.ClientSession() as session:
        client = api.SubZeroClient(session, "test-key", tokens)
        with pytest.raises(api.ApiError):
            await client.set_property("test-fridge", "air_filter_on", False)
    assert len(control_server["requests"]) == 1
