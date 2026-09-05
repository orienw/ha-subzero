"""Cloud failures, token rotation and both observed SignalR envelope versions."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web

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
        behavior["refreshes"] += 1
        data = await request.post()
        assert data["grant_type"] == "refresh_token"
        return web.json_response(
            make_tokens(behavior["user_id"], refresh_token="rotated-refresh", expires_in=7200)
        )

    async def devices(request):
        behavior["requests"].append(request.headers.get("Authorization"))
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


@pytest.mark.parametrize("offline", [False, True])
async def test_single_signalr_connection_routes_multiple_appliances(
    aiohttp_server, monkeypatch, socket_enabled, tokens, offline
):
    sockets = []
    opened = []

    async def negotiate_user(request):
        assert request.query["userId"] == "test-owner"
        return web.json_response(
            {
                "url": origin + "/client/?hub=connectedappliances",
                "accessToken": "test-signalr-token",
            }
        )

    async def negotiate_transport(request):
        assert request.query == {"hub": "connectedappliances", "negotiateVersion": "1"}
        return web.json_response({"connectionToken": "connection"})

    async def websocket(request):
        assert request.query == {"hub": "connectedappliances", "id": "connection"}
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        sockets.append(socket)
        assert json.loads((await socket.receive_str()).rstrip(api.SEPARATOR)) == {
            "protocol": "json",
            "version": 1,
        }
        await socket.send_str("{}" + api.SEPARATOR)
        async for _ in socket:
            pass
        return socket

    async def command(request):
        device_id = request.match_info["device_id"]
        assert (await request.json())["pload"]["cmd"] == "open_cloud_async"
        opened.append(device_id)
        if offline and device_id == "test-oven":
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
    received = {}
    async with aiohttp.ClientSession() as session:
        stream = api.SubZeroClient(session, "test-key", tokens).watch(["test-fridge", "test-oven"])
        async with asyncio.timeout(5):
            try:
                async for device_id, update in stream:
                    received[device_id] = update
                    if len(received) == 2:
                        break
            finally:
                await stream.aclose()
    assert len(sockets) == 1
    assert opened == ["test-fridge", "test-oven"]
    assert isinstance(received["test-fridge"], api.StateUpdate)
    assert isinstance(received["test-oven"], api.ApiError if offline else api.StateUpdate)
