from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from jose import jwt

from app.core.config import get_settings
from app.core.errors import InvalidTokenError
from app.core.security.jwt import verify_supabase_jwt


def _make_token(**overrides) -> str:
    settings = get_settings()
    payload = {
        "sub": str(uuid4()),
        "email": "surfer@example.com",
        "aud": "authenticated",
        "exp": datetime.now(tz=timezone.utc) + timedelta(hours=1),
    }
    payload.update(overrides)
    return jwt.encode(payload, settings.SUPABASE_JWT_SECRET, algorithm="HS256")


def test_valid_token_returns_auth_user():
    sub = str(uuid4())
    token = _make_token(sub=sub, email="a@b.com")
    user = verify_supabase_jwt(token)
    assert str(user.id) == sub
    assert user.email == "a@b.com"


def test_expired_token_rejected():
    token = _make_token(exp=datetime.now(tz=timezone.utc) - timedelta(minutes=1))
    with pytest.raises(InvalidTokenError):
        verify_supabase_jwt(token)


def test_wrong_audience_rejected():
    token = _make_token(aud="wrong-audience")
    with pytest.raises(InvalidTokenError):
        verify_supabase_jwt(token)


def test_bad_signature_rejected():
    settings = get_settings()
    payload = {
        "sub": str(uuid4()),
        "email": "x@y.com",
        "aud": "authenticated",
        "exp": datetime.now(tz=timezone.utc) + timedelta(hours=1),
    }
    token = jwt.encode(payload, settings.SUPABASE_JWT_SECRET + "-tampered", algorithm="HS256")
    with pytest.raises(InvalidTokenError):
        verify_supabase_jwt(token)


def test_missing_sub_rejected():
    token = _make_token(sub=None)
    with pytest.raises(InvalidTokenError):
        verify_supabase_jwt(token)


def test_non_uuid_sub_rejected():
    token = _make_token(sub="not-a-uuid")
    with pytest.raises(InvalidTokenError):
        verify_supabase_jwt(token)
