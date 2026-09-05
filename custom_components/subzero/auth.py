"""Sub-Zero's Azure B2C login, using the website's form requests."""

import hashlib
import json
import re
import secrets
from base64 import urlsafe_b64encode
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import aiohttp
import jwt

from .app_config import AUTH_HEADERS, LOGIN_HEADERS

LOGIN_ORIGIN = "https://login.subzero-wolf.com"
POLICY_BASE = LOGIN_ORIGIN + "/SubZeroB2CPrd.onmicrosoft.com/B2C_1A_SIGNUP_SIGNIN"
AUTHORIZE_URL = POLICY_BASE + "/oauth2/v2.0/authorize"
TOKEN_URL = POLICY_BASE + "/oauth2/v2.0/token"
METADATA_URL = POLICY_BASE + "/v2.0/.well-known/openid-configuration"
CLIENT_ID = "6eefabd0-49a3-4b92-b329-81b9f638e940"
REDIRECT_URI = "com.szg.szgdigitalproductexperience://oauth/redirect"
SCOPES = "openid offline_access " + CLIENT_ID


class LoginError(Exception):
    """The sign-in service could not complete the request."""


class InvalidAuth(LoginError):
    """The sign-in service rejected the credentials or verification code."""


class LoginChallenge(LoginError):
    """The sign-in journey requires another form step."""


def page_variable(page: str, name: str) -> dict:
    match = re.search(r"\b" + re.escape(name) + r"\s*=\s*", page)
    if not match:
        raise LoginError("The Sub-Zero sign-in page has changed.")
    try:
        value, _ = json.JSONDecoder().raw_decode(page[match.end() :])
    except ValueError:
        raise LoginError("The Sub-Zero sign-in page has changed.") from None
    if not isinstance(value, dict):
        raise LoginError("The Sub-Zero sign-in page has changed.")
    return value


class SubZeroLogin:
    """Keep login state in a session using CookieJar(quote_cookie=False).

    B2C rejects quoted cookie values that browsers send without quotes.
    Token requests use a separate session without the browser's cookies.
    """

    def __init__(self, session: aiohttp.ClientSession, token_session: aiohttp.ClientSession):
        self.session = session
        self.token_session = token_session
        self.settings: dict = {}
        self.last_page = ""
        self.last_url = ""
        self.state = secrets.token_urlsafe(32)
        self.nonce = secrets.token_urlsafe(32)
        self.verifier = secrets.token_urlsafe(48)

    async def login(self, username: str, password: str) -> dict:
        challenge = (
            urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip("=")
        )
        params = {
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPES,
            "state": self.state,
            "nonce": self.nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        try:
            await self._navigate(AUTHORIZE_URL + "?" + urlencode(params))
            if self.settings.get("api") != "CombinedSigninAndSignup":
                raise LoginError("Sub-Zero returned an unexpected sign-in step.")
            await self._post_form(
                {"request_type": "RESPONSE", "signInName": username, "password": password}
            )
            return await self._confirm()
        except KeyError, TypeError, ValueError:
            raise LoginError("Sub-Zero returned an invalid sign-in response.") from None

    async def _post_form(self, data: dict) -> None:
        settings = self.settings
        path = settings["hosts"]["tenant"] + "/SelfAsserted"
        params = {"tx": settings["transId"], "p": settings["hosts"]["policy"]}
        headers = {
            **LOGIN_HEADERS,
            "X-CSRF-TOKEN": settings["csrf"],
            "Origin": LOGIN_ORIGIN,
            "Referer": self.last_url,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        if settings.get("isPageViewIdSentWithHeader"):
            headers["x-ms-cpim-pageviewid"] = settings["pageViewId"]
        async with self.session.post(
            LOGIN_ORIGIN + path, params=params, headers=headers, data=data, allow_redirects=False
        ) as response:
            if response.status != 200:
                raise LoginError(f"Sub-Zero's sign-in form returned HTTP {response.status}.")
            try:
                result = await response.json(content_type=None)
            except ValueError:
                raise LoginError("Sub-Zero returned an invalid sign-in response.") from None
        if not isinstance(result, dict):
            raise LoginError("Sub-Zero returned an invalid sign-in response.")
        if str(result.get("status")) != "200":
            raise InvalidAuth("Sub-Zero did not accept the sign-in details.")

    async def _confirm(self) -> dict:
        settings = self.settings
        path = settings["hosts"]["tenant"] + "/api/" + settings["api"] + "/confirmed"
        params = {
            "rememberMe": "false",
            "csrf_token": settings["csrf"],
            "tx": settings["transId"],
            "p": settings["hosts"]["policy"],
        }
        code = await self._navigate(LOGIN_ORIGIN + path + "?" + urlencode(params))
        if code is None:
            raise LoginChallenge("Sub-Zero requires another verification step.")
        return await self._exchange_code(code)

    async def _navigate(self, url: str) -> str | None:
        expected = urlsplit(REDIRECT_URI)
        for _ in range(10):
            parsed = urlsplit(url)
            if (parsed.scheme, parsed.netloc, parsed.path) == (
                expected.scheme,
                expected.netloc,
                expected.path,
            ):
                params = parse_qs(parsed.query)
                if not secrets.compare_digest(params.get("state", [""])[0], self.state):
                    raise LoginError("Sub-Zero returned an unexpected OAuth state.")
                if params.get("error") or not params.get("code"):
                    raise InvalidAuth("Sub-Zero did not authorize this sign-in.")
                return params["code"][0]
            origin = urlsplit(LOGIN_ORIGIN)
            if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
                raise LoginError("Sub-Zero requested an unsupported sign-in provider.")
            async with self.session.get(
                url, headers=LOGIN_HEADERS, allow_redirects=False
            ) as response:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        raise LoginError("Sub-Zero returned an invalid sign-in redirect.")
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise LoginError(f"Sub-Zero's sign-in page returned HTTP {response.status}.")
                self.last_page = await response.text()
                self.last_url = str(response.url)
                self.settings = page_variable(self.last_page, "SETTINGS")
                return None
        raise LoginError("Sub-Zero returned too many sign-in redirects.")

    async def _exchange_code(self, code: str) -> dict:
        data = {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "code": code,
            "code_verifier": self.verifier,
            "scope": SCOPES,
        }
        async with self.token_session.post(
            TOKEN_URL,
            headers=AUTH_HEADERS,
            data=data,
            timeout=self.session.timeout,
            allow_redirects=False,
        ) as response:
            if response.status in (400, 401, 403):
                raise InvalidAuth("Sub-Zero did not accept the authorization code.")
            if response.status != 200:
                raise LoginError(f"Sub-Zero's token service returned HTTP {response.status}.")
            tokens = await response.json(content_type=None)
        if not isinstance(tokens, dict) or not all(
            tokens.get(key) for key in ("access_token", "id_token", "refresh_token")
        ):
            raise LoginError("Sub-Zero did not return the required login tokens.")
        await self._validate_identity(tokens["id_token"])
        return tokens

    async def _validate_identity(self, token: str) -> None:
        async with self.token_session.get(
            METADATA_URL,
            headers=AUTH_HEADERS,
            timeout=self.session.timeout,
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise LoginError("Sub-Zero's signing metadata is unavailable.")
            metadata = await response.json(content_type=None)
        if not isinstance(metadata, dict) or not all(
            isinstance(metadata.get(key), str) for key in ("jwks_uri", "issuer")
        ):
            raise LoginError("Sub-Zero returned invalid signing metadata.")
        key_url = urlsplit(metadata["jwks_uri"])
        origin = urlsplit(LOGIN_ORIGIN)
        if (key_url.scheme, key_url.netloc) not in {
            (origin.scheme, origin.netloc),
            ("https", "subzerob2cprd.b2clogin.com"),
        }:
            raise LoginError("Sub-Zero returned an unexpected signing-key location.")
        async with self.token_session.get(
            metadata["jwks_uri"],
            headers=AUTH_HEADERS,
            timeout=self.session.timeout,
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise LoginError("Sub-Zero's signing keys are unavailable.")
            keys = await response.json(content_type=None)
        try:
            kid = jwt.get_unverified_header(token)["kid"]
            key = next(jwt.PyJWK.from_dict(key) for key in keys["keys"] if key["kid"] == kid)
            identity = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=CLIENT_ID,
                issuer=metadata["issuer"],
                options={"require": ["exp", "aud", "iss", "nonce"]},
            )
            if not secrets.compare_digest(identity.get("nonce", ""), self.nonce):
                raise LoginError("Sub-Zero returned an unexpected login nonce.")
        except jwt.PyJWTError, KeyError, StopIteration, TypeError, ValueError:
            raise LoginError("Sub-Zero's login token could not be verified.") from None
