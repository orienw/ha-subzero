"""Synthetic fixtures; tests never use the owner's saved account data."""

import time

import jwt
import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


def make_tokens(user_id="test-owner", *, expires_in=3600, refresh_token="test-refresh"):
    return {
        **{
            name: jwt.encode(
                {"mergedId": user_id, "exp": int(time.time()) + expires_in, "token_use": name},
                "synthetic-key-for-tests-only-32-bytes",
                algorithm="HS256",
            )
            for name in ("access_token", "id_token")
        },
        "refresh_token": refresh_token,
    }


@pytest.fixture
def tokens():
    return make_tokens()
