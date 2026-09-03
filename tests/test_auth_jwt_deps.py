"""JWT verification beyond the HS256 happy path, plus dependency wiring.

Supabase issues either legacy HS256 tokens or ES256 tokens signed with a
rotating JWT signing key, so the asymmetric branch is a live production path,
not a fallback.
"""

from __future__ import annotations

import base64
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from jose import jwt

from app.core.deps import close_arq_pool, get_arq_pool, get_current_user
from app.core.errors import InvalidTokenError, MissingTokenError
from app.core.security.jwt import AuthUser, _fetch_jwks, _peek_algorithm, verify_supabase_jwt
from tests.conftest import make_token


@pytest.fixture(autouse=True)
def _clear_jwks_cache():
    """_fetch_jwks is lru_cached process-wide; a stale entry would leak across tests."""
    _fetch_jwks.cache_clear()
    yield
    _fetch_jwks.cache_clear()


# ---------------------------------------------------------------------------
# ES256 / JWKS
# ---------------------------------------------------------------------------


def _b64u(value: int, length: int) -> str:
    return base64.urlsafe_b64encode(value.to_bytes(length, "big")).rstrip(b"=").decode()


@pytest.fixture
def es256_key():
    """An EC P-256 keypair plus its public JWK, as Supabase would publish it."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    numbers = private_key.public_key().public_numbers()
    jwk = {
        "kty": "EC",
        "crv": "P-256",
        "kid": "test-signing-key",
        "alg": "ES256",
        "use": "sig",
        "x": _b64u(numbers.x, 32),
        "y": _b64u(numbers.y, 32),
    }
    from cryptography.hazmat.primitives import serialization

    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return {"pem": pem, "jwk": jwk}


def _es256_token(es256_key, user_id, *, kid="test-signing-key", audience="authenticated"):
    from datetime import UTC, datetime, timedelta

    return jwt.encode(
        {
            "sub": str(user_id),
            "email": "surfer@example.com",
            "aud": audience,
            "exp": datetime.now(tz=UTC) + timedelta(hours=1),
        },
        es256_key["pem"],
        algorithm="ES256",
        headers={"kid": kid},
    )


@pytest.fixture
def jwks_served(monkeypatch):
    """Serve a JWKS document over the mocked HTTP transport jwt.py uses."""

    def _install(keys: list[dict]):
        real_cls = httpx.Client

        class _MockedClient(real_cls):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = httpx.MockTransport(
                    lambda request: httpx.Response(200, json={"keys": keys})
                )
                super().__init__(*args, **kwargs)

        monkeypatch.setattr("app.core.security.jwt.httpx.Client", _MockedClient)

    return _install


def test_an_es256_token_is_verified_against_the_published_jwks(es256_key, jwks_served):
    jwks_served([es256_key["jwk"]])
    user_id = uuid4()

    user = verify_supabase_jwt(_es256_token(es256_key, user_id))

    assert user == AuthUser(id=user_id, email="surfer@example.com")


def test_the_matching_key_is_selected_by_kid(es256_key, jwks_served):
    """A decoy key listed first must not be picked over the one named by kid."""
    decoy = dict(es256_key["jwk"], kid="some-other-key", x=_b64u(1, 32), y=_b64u(2, 32))
    jwks_served([decoy, es256_key["jwk"]])

    assert verify_supabase_jwt(_es256_token(es256_key, uuid4())).email == "surfer@example.com"


def test_the_jwks_is_fetched_once_and_cached(es256_key, jwks_served, monkeypatch):
    jwks_served([es256_key["jwk"]])
    calls: list[int] = []
    real = httpx.Client.get

    def _counting_get(self, *args, **kwargs):
        calls.append(1)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "get", _counting_get)

    verify_supabase_jwt(_es256_token(es256_key, uuid4()))
    verify_supabase_jwt(_es256_token(es256_key, uuid4()))

    assert len(calls) == 1


def test_an_es256_token_with_the_wrong_audience_is_rejected(es256_key, jwks_served):
    jwks_served([es256_key["jwk"]])
    with pytest.raises(InvalidTokenError):
        verify_supabase_jwt(_es256_token(es256_key, uuid4(), audience="anon"))


def test_an_es256_token_signed_by_an_unpublished_key_is_rejected(es256_key, jwks_served):
    """The JWKS lists a different key, so the signature cannot validate."""
    other = ec.generate_private_key(ec.SECP256R1())
    from cryptography.hazmat.primitives import serialization

    other_pem = other.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    jwks_served([es256_key["jwk"]])

    from datetime import UTC, datetime, timedelta

    forged = jwt.encode(
        {
            "sub": str(uuid4()),
            "email": "a@example.com",
            "aud": "authenticated",
            "exp": datetime.now(tz=UTC) + timedelta(hours=1),
        },
        other_pem,
        algorithm="ES256",
        headers={"kid": "test-signing-key"},
    )

    with pytest.raises(InvalidTokenError):
        verify_supabase_jwt(forged)


def test_an_hs256_token_never_triggers_a_jwks_fetch(monkeypatch):
    """The symmetric path must not reach out to the network."""

    def _boom(*args, **kwargs):
        raise AssertionError("HS256 verification must not fetch the JWKS")

    monkeypatch.setattr("app.core.security.jwt._fetch_jwks", _boom)
    assert verify_supabase_jwt(make_token(uuid4())).email == "surfer@example.com"


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------


def test_the_algorithm_is_read_from_the_token_header():
    assert _peek_algorithm(make_token(uuid4())) == "HS256"


def test_a_malformed_token_header_is_rejected():
    with pytest.raises(InvalidTokenError):
        _peek_algorithm("garbage")


def test_a_token_missing_the_email_claim_is_rejected():
    from datetime import UTC, datetime, timedelta

    from app.core.config import get_settings

    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "aud": "authenticated",
            "exp": datetime.now(tz=UTC) + timedelta(hours=1),
        },
        get_settings().SUPABASE_JWT_SECRET,
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        verify_supabase_jwt(token)


# ---------------------------------------------------------------------------
# get_current_user
# ---------------------------------------------------------------------------


async def test_a_missing_authorization_header_is_a_missing_token():
    with pytest.raises(MissingTokenError):
        await get_current_user(authorization=None)


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Bearer",  # scheme with no credentials
        "Bearer ",
        "Basic dXNlcjpwYXNz",  # wrong scheme
        "token abc",
    ],
)
async def test_malformed_authorization_headers_are_rejected(header):
    with pytest.raises(MissingTokenError):
        await get_current_user(authorization=header)


async def test_the_bearer_scheme_is_matched_case_insensitively():
    user_id = uuid4()
    user = await get_current_user(authorization=f"bearer {make_token(user_id)}")
    assert user.id == user_id


async def test_a_bearer_header_with_a_bad_token_is_an_invalid_token():
    with pytest.raises(InvalidTokenError):
        await get_current_user(authorization="Bearer not-a-jwt")


# ---------------------------------------------------------------------------
# arq pool lifecycle
# ---------------------------------------------------------------------------


async def test_the_arq_pool_is_created_once_and_reused(monkeypatch):
    created: list[int] = []

    class _Pool:
        async def aclose(self):
            pass

    async def _create_pool(settings):
        created.append(1)
        return _Pool()

    monkeypatch.setattr("app.core.deps.create_pool", _create_pool)
    monkeypatch.setattr("app.core.deps._arq_pool", None)

    first = await get_arq_pool()
    second = await get_arq_pool()

    assert first is second
    assert len(created) == 1
    await close_arq_pool()


async def test_closing_the_pool_releases_it_for_recreation(monkeypatch):
    closed: list[int] = []

    class _Pool:
        async def aclose(self):
            closed.append(1)

    async def _create_pool(settings):
        return _Pool()

    monkeypatch.setattr("app.core.deps.create_pool", _create_pool)
    monkeypatch.setattr("app.core.deps._arq_pool", None)

    await get_arq_pool()
    await close_arq_pool()

    assert closed == [1]
    import app.core.deps as deps

    assert deps._arq_pool is None


async def test_closing_an_unopened_pool_is_a_no_op(monkeypatch):
    monkeypatch.setattr("app.core.deps._arq_pool", None)
    await close_arq_pool()  # must not raise
