"""Exercise B2C's form journey, browser-compatible cookies and ID validation."""

import hashlib
import json
import time
from base64 import urlsafe_b64encode
from urllib.parse import urlencode

import aiohttp
import jwt
import pytest
from aiohttp import web
from cryptography.hazmat.primitives.asymmetric import rsa
from homeassistant.helpers.aiohttp_client import async_create_clientsession, async_get_clientsession

from custom_components.subzero import auth


@pytest.fixture
async def login_client():
    async with (
        aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(quote_cookie=False, unsafe=True)
        ) as session,
        aiohttp.ClientSession() as token_session,
    ):
        yield auth.SubZeroLogin(session, token_session)


@pytest.fixture
async def login_server(aiohttp_server, monkeypatch, socket_enabled):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_key["kid"] = "test-key"
    journey = {"failure": None, "form_accepted": False, "exchanged": False, "requests": []}

    @web.middleware
    async def record_headers(request, handler):
        journey["requests"].append((request.path, request.headers.copy()))
        if journey.get("response_path") == request.path:
            return web.Response(text=journey["response_body"], status=journey["response_status"])
        return await handler(request)

    async def start(request):
        raise web.HTTPFound("/authorize?" + request.query_string)

    async def authorize(request):
        journey.update(request.query)
        journey["authorize_url"] = str(request.url)
        settings = {
            "api": "CombinedSigninAndSignup",
            "hosts": {"tenant": "/policy", "policy": "test-policy"},
            "csrf": "test-csrf",
            "transId": "test-transaction",
        }
        response = web.Response(text="SETTINGS = " + json.dumps(settings) + ";")
        response.headers.add("Set-Cookie", "x-ms-cpim-csrf=abc==; Path=/")
        return response

    async def form(request):
        assert request.headers["X-CSRF-TOKEN"] == "test-csrf"
        assert request.headers["Referer"] == journey["authorize_url"]
        assert "x-ms-cpim-csrf=abc==" in request.headers["Cookie"]
        assert 'x-ms-cpim-csrf="' not in request.headers["Cookie"]
        assert request.query == {"tx": "test-transaction", "p": "test-policy"}
        data = await request.post()
        assert data["signInName"] == "owner@example.test"
        assert data["password"] == "test-only-password"
        journey["form_accepted"] = True
        status = "400" if journey["failure"] == "password" else "200"
        return web.Response(text=json.dumps({"status": status}), content_type="text/json")

    async def confirmed(request):
        assert journey["form_accepted"]
        if journey["failure"] == "mfa":
            return web.Response(text='SETTINGS = {"api":"SelfAsserted"};')
        state = "wrong-state" if journey["failure"] == "state" else journey["state"]
        raise web.HTTPFound(auth.REDIRECT_URI + "?" + urlencode({"state": state, "code": "code"}))

    async def token(request):
        data = await request.post()
        challenge = (
            urlsafe_b64encode(hashlib.sha256(data["code_verifier"].encode()).digest())
            .decode()
            .rstrip("=")
        )
        assert journey["code_challenge"] == challenge
        assert journey["code_challenge_method"] == "S256"
        assert data["redirect_uri"] == auth.REDIRECT_URI
        claims = {
            "sub": "test-owner",
            "iss": origin + "/issuer",
            "aud": auth.CLIENT_ID,
            "exp": int(time.time()) + 3600,
            "nonce": journey["nonce"],
        }
        failure = journey["failure"]
        if failure in {"nonce", "aud", "iss"}:
            claims[failure] = "unexpected-value"
        if failure == "expired":
            claims["exp"] = int(time.time()) - 60
        if failure == "missing_exp":
            del claims["exp"]
        key = private_key
        if failure == "signature":
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        identity = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})
        journey["exchanged"] = True
        return web.json_response(
            {"id_token": identity, "access_token": "test-access", "refresh_token": "test-refresh"}
        )

    async def metadata(request):
        return web.json_response({"issuer": origin + "/issuer", "jwks_uri": origin + "/keys"})

    async def keys(request):
        return web.json_response({"keys": [public_key]})

    app = web.Application(middlewares=[record_headers])
    app.router.add_get("/start", start)
    app.router.add_get("/authorize", authorize)
    app.router.add_post("/policy/SelfAsserted", form)
    app.router.add_get("/policy/api/CombinedSigninAndSignup/confirmed", confirmed)
    app.router.add_post("/token", token)
    app.router.add_get("/metadata", metadata)
    app.router.add_get("/keys", keys)
    server = await aiohttp_server(app)
    origin = str(server.make_url("/")).rstrip("/")
    monkeypatch.setattr(auth, "LOGIN_ORIGIN", origin)
    monkeypatch.setattr(auth, "AUTHORIZE_URL", origin + "/authorize")
    monkeypatch.setattr(auth, "TOKEN_URL", origin + "/token")
    monkeypatch.setattr(auth, "METADATA_URL", origin + "/metadata")
    return journey


async def test_password_login_with_b2c_cookies_and_text_json(login_server, login_client):
    tokens = await login_client.login("owner@example.test", "test-only-password")
    assert tokens["refresh_token"] == "test-refresh"
    assert login_server["exchanged"]
    assert "password" not in tokens


async def test_login_headers_override_home_assistant_through_redirects(
    hass, login_server, monkeypatch
):
    monkeypatch.setattr(auth, "AUTHORIZE_URL", auth.LOGIN_ORIGIN + "/start")
    session = async_create_clientsession(
        hass, cookie_jar=aiohttp.CookieJar(quote_cookie=False, unsafe=True)
    )
    token_session = async_get_clientsession(hass)
    original_headers = dict(session.headers)
    original_token_headers = dict(token_session.headers)
    await auth.SubZeroLogin(session, token_session).login(
        "owner@example.test", "test-only-password"
    )
    assert dict(session.headers) == original_headers
    assert dict(token_session.headers) == original_token_headers
    browser_paths = [
        "/start",
        "/authorize",
        "/policy/SelfAsserted",
        "/policy/api/CombinedSigninAndSignup/confirmed",
    ]
    assert [path for path, _ in login_server["requests"]] == [
        *browser_paths,
        "/token",
        "/metadata",
        "/keys",
    ]
    for path, headers in login_server["requests"]:
        if path in browser_paths:
            assert headers.getall("User-Agent") == [
                "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/153.0.0.0 Mobile Safari/537.36"
            ]
            assert headers["Accept-Language"] == "en-US,en;q=0.9"
        else:
            assert headers.getall("User-Agent") == [
                "Dalvik/2.1.0 (Linux; U; Android 16; Pixel 9 Build/BP2A.250605.031.A2)"
            ]
            assert headers["Accept"] == "application/json"
            assert headers["Accept-Encoding"] == "gzip"
            assert "Cookie" not in headers
            assert "X-CSRF-TOKEN" not in headers
        assert not {"Authorization", "Userid", "Ocp-Apim-Subscription-Key"} & headers.keys()


@pytest.mark.parametrize(
    ("failure", "exception"),
    [
        ("password", auth.InvalidAuth),
        ("mfa", auth.LoginChallenge),
        ("state", auth.LoginError),
        ("nonce", auth.LoginError),
        ("aud", auth.LoginError),
        ("iss", auth.LoginError),
        ("expired", auth.LoginError),
        ("missing_exp", auth.LoginError),
        ("signature", auth.LoginError),
    ],
)
async def test_rejects_failed_or_unverified_login(login_server, login_client, failure, exception):
    login_server["failure"] = failure
    with pytest.raises(exception):
        await login_client.login("owner@example.test", "test-only-password")
    assert login_server["form_accepted"]
    if failure not in {"password", "mfa", "state"}:
        assert login_server["exchanged"]


async def test_rejects_external_redirect_without_requesting_it(login_client):
    with pytest.raises(auth.LoginError, match="unsupported sign-in provider"):
        await login_client._navigate("https://unexpected.example.test/login")


@pytest.mark.parametrize(
    ("path", "body", "status"),
    [
        ("/authorize", 'SETTINGS = {"api":"CombinedSigninAndSignup"};', 200),
        ("/token", "<html>Temporarily unavailable</html>", 200),
        ("/metadata", "<html>Temporarily unavailable</html>", 200),
        ("/keys", "<html>Temporarily unavailable</html>", 200),
        ("/token", "Service unavailable", 503),
    ],
)
async def test_bad_login_responses_report_connection_failure(
    login_server, login_client, path, body, status
):
    login_server.update(response_path=path, response_body=body, response_status=status)
    with pytest.raises(auth.LoginError) as error:
        await login_client.login("owner@example.test", "test-only-password")
    assert type(error.value) is auth.LoginError
