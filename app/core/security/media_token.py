"""Short-lived, signed access tokens for media streaming.

Each token is scoped to a single media_id + profile_id and expires after
MEDIA_TOKEN_TTL_SECONDS (default 15 min).  Tokens use HS256 with the
application's SUPABASE_JWT_SECRET and audience="media" so they cannot be
confused with Supabase auth JWTs (audience="authenticated").
"""

import time
from uuid import UUID

from jose import ExpiredSignatureError, JWTError, jwt

from app.core.config import get_settings
from app.core.errors import ForbiddenError, InvalidTokenError, TokenExpiredError

_ALG = "HS256"
_AUD = "media"
MEDIA_TOKEN_TTL_SECONDS = 15 * 60  # 15 minutes


def mint_media_token(media_id: UUID, profile_id: UUID) -> str:
    """Create a short-lived token granting read access to one media object."""
    settings = get_settings()
    now = int(time.time())
    payload = {
        "media_id": str(media_id),
        "sub": str(profile_id),
        "aud": _AUD,
        "iat": now,
        "exp": now + MEDIA_TOKEN_TTL_SECONDS,
    }
    return jwt.encode(payload, settings.SUPABASE_JWT_SECRET, algorithm=_ALG)


def verify_media_token(token: str, media_id: UUID) -> UUID:
    """Validate a media access token and return the profile_id.

    Raises:
        TokenExpiredError: token past its expiry.
        InvalidTokenError: signature invalid or claims malformed.
        ForbiddenError: token was issued for a different media_id.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.SUPABASE_JWT_SECRET,
            algorithms=[_ALG],
            audience=_AUD,
        )
    except ExpiredSignatureError as e:
        raise TokenExpiredError() from e
    except JWTError as e:
        raise InvalidTokenError() from e

    if payload.get("media_id") != str(media_id):
        raise ForbiddenError("Token is not valid for this media.")

    sub = payload.get("sub")
    if not sub:
        raise InvalidTokenError("Token is missing profile claim.")

    try:
        return UUID(sub)
    except (ValueError, TypeError) as e:
        raise InvalidTokenError("Token profile claim is not a valid UUID.") from e
