"""Signed media-access tokens.

These grant read access to one object without a Supabase session, so the
scoping rules — one media_id, one profile, short expiry, own audience — are
the whole security boundary.
"""

from __future__ import annotations

import time
from uuid import uuid4

import pytest
from jose import jwt

from app.core.config import get_settings
from app.core.errors import ForbiddenError, InvalidTokenError, TokenExpiredError
from app.core.security.media_token import (
    MEDIA_TOKEN_TTL_SECONDS,
    mint_media_token,
    verify_media_token,
)
from tests.conftest import make_token as make_auth_token


def _decode(token: str) -> dict:
    return jwt.decode(
        token, get_settings().SUPABASE_JWT_SECRET, algorithms=["HS256"], audience="media"
    )


def _sign(payload: dict) -> str:
    return jwt.encode(payload, get_settings().SUPABASE_JWT_SECRET, algorithm="HS256")


def test_a_minted_token_verifies_and_returns_the_profile():
    media_id, profile_id = uuid4(), uuid4()
    assert verify_media_token(mint_media_token(media_id, profile_id), media_id) == profile_id


def test_the_token_carries_the_media_audience():
    """A distinct audience stops an auth JWT and a media token being interchangeable."""
    claims = _decode(mint_media_token(uuid4(), uuid4()))
    assert claims["aud"] == "media"


def test_the_token_expires_after_the_configured_ttl():
    claims = _decode(mint_media_token(uuid4(), uuid4()))
    assert claims["exp"] - claims["iat"] == MEDIA_TOKEN_TTL_SECONDS


def test_a_token_for_another_object_is_refused():
    token = mint_media_token(uuid4(), uuid4())
    with pytest.raises(ForbiddenError):
        verify_media_token(token, uuid4())


def test_an_expired_token_is_refused():
    media_id, profile_id = uuid4(), uuid4()
    past = int(time.time()) - 10
    token = _sign(
        {
            "media_id": str(media_id),
            "sub": str(profile_id),
            "aud": "media",
            "iat": past - MEDIA_TOKEN_TTL_SECONDS,
            "exp": past,
        }
    )
    with pytest.raises(TokenExpiredError):
        verify_media_token(token, media_id)


def test_a_token_signed_with_another_secret_is_refused():
    media_id = uuid4()
    token = jwt.encode(
        {
            "media_id": str(media_id),
            "sub": str(uuid4()),
            "aud": "media",
            "exp": int(time.time()) + 600,
        },
        "an-entirely-different-secret",
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        verify_media_token(token, media_id)


def test_a_supabase_auth_jwt_is_not_accepted_as_a_media_token():
    """Same secret, different audience — the audience check is what separates them."""
    with pytest.raises(InvalidTokenError):
        verify_media_token(make_auth_token(uuid4()), uuid4())


def test_a_token_without_a_profile_claim_is_refused():
    media_id = uuid4()
    token = _sign({"media_id": str(media_id), "aud": "media", "exp": int(time.time()) + 600})
    with pytest.raises(InvalidTokenError):
        verify_media_token(token, media_id)


def test_a_token_with_an_empty_profile_claim_is_refused():
    media_id = uuid4()
    token = _sign(
        {"media_id": str(media_id), "sub": "", "aud": "media", "exp": int(time.time()) + 600}
    )
    with pytest.raises(InvalidTokenError):
        verify_media_token(token, media_id)


def test_a_non_uuid_profile_claim_is_refused():
    media_id = uuid4()
    token = _sign(
        {
            "media_id": str(media_id),
            "sub": "not-a-uuid",
            "aud": "media",
            "exp": int(time.time()) + 600,
        }
    )
    with pytest.raises(InvalidTokenError):
        verify_media_token(token, media_id)


def test_garbage_is_refused():
    with pytest.raises(InvalidTokenError):
        verify_media_token("not.a.token", uuid4())


def test_tokens_for_different_objects_are_distinct():
    profile_id = uuid4()
    assert mint_media_token(uuid4(), profile_id) != mint_media_token(uuid4(), profile_id)
