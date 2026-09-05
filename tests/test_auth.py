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

from custom_components.subzero import auth


@pytest.fixture
async def login_server(aiohttp_server, monkeypatch, socket_enabled):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_key["kid"] = "test-key"
    journey = {"failure": None, "form_accepted": False, "exchanged": False}

    async def authorize(request):
        journey.update(request.query)
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

    app = web.Application()
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


async def test_password_login_with_b2c_cookies_and_text_json(login_server):
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(quote_cookie=False, unsafe=True)
    ) as session:
        tokens = await auth.SubZeroLogin(session).login("owner@example.test", "test-only-password")
    assert tokens["refresh_token"] == "test-refresh"
    assert login_server["exchanged"]
    assert "password" not in tokens


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
async def test_rejects_failed_or_unverified_login(login_server, failure, exception):
    login_server["failure"] = failure
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(quote_cookie=False, unsafe=True)
    ) as session:
        with pytest.raises(exception):
            await auth.SubZeroLogin(session).login("owner@example.test", "test-only-password")


async def test_rejects_external_redirect_without_requesting_it():
    async with aiohttp.ClientSession() as session:
        with pytest.raises(auth.LoginError, match="unsupported sign-in provider"):
            await auth.SubZeroLogin(session)._navigate("https://unexpected.example.test/login")
