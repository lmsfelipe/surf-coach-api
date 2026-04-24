from dataclasses import dataclass
from functools import lru_cache
from uuid import UUID

import httpx
from jose import JWTError, jwt

from app.core.config import get_settings
from app.core.errors import InvalidTokenError


@dataclass(frozen=True)
class AuthUser:
    id: UUID
    email: str


@lru_cache(maxsize=1)
def _fetch_jwks() -> dict:
    """Fetch and cache the JWKS from Supabase (used for ES256 / asymmetric tokens)."""
    settings = get_settings()
    url = f"{settings.SUPABASE_URL}/auth/v1/.well-known/jwks.json"
    with httpx.Client(timeout=10) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.json()


def _peek_algorithm(token: str) -> str:
    try:
        return jwt.get_unverified_header(token).get("alg", "HS256")
    except JWTError as e:
        raise InvalidTokenError() from e


def verify_supabase_jwt(token: str) -> AuthUser:
    """Verify a Supabase-issued JWT (HS256 legacy or ES256 signing key).

    Enforces signature, expiry, and that aud == "authenticated".
    """
    settings = get_settings()
    algorithm = _peek_algorithm(token)

    try:
        if algorithm == "HS256":
            key = settings.SUPABASE_JWT_SECRET
        else:
            # ES256 (JWT Signing Keys) — resolve the matching JWK by kid.
            jwks = _fetch_jwks()
            kid = jwt.get_unverified_header(token).get("kid")
            key = next(
                (k for k in jwks.get("keys", []) if k.get("kid") == kid),
                jwks,  # fallback: pass the full JWKS dict and let jose pick
            )

        payload = jwt.decode(
            token,
            key,
            algorithms=[algorithm],
            audience="authenticated",
        )
    except JWTError as e:
        raise InvalidTokenError() from e

    sub = payload.get("sub")
    email = payload.get("email")
    if not sub or not email:
        raise InvalidTokenError("Token is missing required claims.")

    try:
        user_id = UUID(sub)
    except (ValueError, TypeError) as e:
        raise InvalidTokenError("Token `sub` claim is not a valid UUID.") from e

    return AuthUser(id=user_id, email=email)
