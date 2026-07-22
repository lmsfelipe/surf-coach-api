from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import db_session, get_current_user
from app.core.rate_limit import limiter
from app.core.errors import NotFoundError
from app.core.frame_extractor import FrameExtractor
from app.core.security.jwt import AuthUser
from app.core.security.media_token import mint_media_token, verify_media_token
from app.core.storage import StorageClient, get_storage_client
from app.models.media import Media
from app.repositories.media import MediaRepository
from app.repositories.sessions import SessionsRepository
from app.schemas.media import MediaOut
from app.services.ai import GeminiService
from app.services.media import MediaService

router = APIRouter(prefix="/api/v1", tags=["media"])


def get_frame_extractor() -> FrameExtractor:
    return FrameExtractor()


def get_media_service(
    db: AsyncSession = Depends(db_session),
    storage: StorageClient = Depends(get_storage_client),
    frame_extractor: FrameExtractor = Depends(get_frame_extractor),
) -> MediaService:
    return MediaService(
        media_repo=MediaRepository(db),
        sessions_repo=SessionsRepository(db),
        storage=storage,
        frame_extractor=frame_extractor,
        gemini=GeminiService(),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _media_to_out(media: Media, profile_id: UUID) -> MediaOut:
    """Convert a Media ORM model to MediaOut with a signed contentUrl."""
    token = mint_media_token(media.id, profile_id)
    return MediaOut(
        id=media.id,
        session_id=media.session_id,
        media_type=media.media_type,
        content_url=f"/api/v1/media/{media.id}/content?token={token}",
        file_name=media.file_name,
        file_size_bytes=media.file_size_bytes,
        duration_seconds=float(media.duration_seconds) if media.duration_seconds else None,
        created_at=media.created_at,
    )


def _storage_key_from_url(url: str) -> str:
    """Extract the Supabase Storage object key from a full storage URL."""
    bucket = get_settings().SUPABASE_BUCKET
    marker = f"/{bucket}/"
    idx = url.find(marker)
    if idx < 0:
        raise NotFoundError("Cannot resolve media storage location.")
    return url[idx + len(marker) :].split("?", 1)[0]


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/sessions/{session_id}/media/",
    response_model=list[MediaOut],
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(lambda: get_settings().RATE_LIMIT_UPLOAD)
async def upload_media(
    request: Request,
    session_id: UUID,
    file: list[UploadFile] = File(...),
    user: AuthUser = Depends(get_current_user),
    service: MediaService = Depends(get_media_service),
) -> list[MediaOut]:
    files_data = [(await f.read(), f.filename or "upload") for f in file]

    service.validate_upload_counts([data for data, _ in files_data])

    results = []
    for data, filename in files_data:
        media = await service.upload(
            session_id=session_id,
            file_bytes=data,
            file_name=filename,
            user=user,
        )
        results.append(_media_to_out(media, user.id))
    return results


@router.get(
    "/sessions/{session_id}/media/",
    response_model=list[MediaOut],
    response_model_by_alias=True,
)
async def list_media(
    session_id: UUID,
    user: AuthUser = Depends(get_current_user),
    service: MediaService = Depends(get_media_service),
) -> list[MediaOut]:
    items = await service.list_media(session_id, user)
    return [_media_to_out(m, user.id) for m in items]


@router.get(
    "/media/{media_id}",
    response_model=MediaOut,
    response_model_by_alias=True,
)
async def get_media(
    media_id: UUID,
    user: AuthUser = Depends(get_current_user),
    service: MediaService = Depends(get_media_service),
) -> MediaOut:
    media = await service.get_media(media_id, user)
    return _media_to_out(media, user.id)


@router.delete("/media/{media_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_media(
    media_id: UUID,
    user: AuthUser = Depends(get_current_user),
    service: MediaService = Depends(get_media_service),
) -> None:
    await service.delete_media(media_id, user)


# ---------------------------------------------------------------------------
# Streaming / proxy endpoint
# ---------------------------------------------------------------------------


@router.get("/media/{media_id}/content", tags=["media-stream"])
async def stream_media_content(
    request: Request,
    media_id: UUID,
    token: str = Query(...),
    service: MediaService = Depends(get_media_service),
    storage: StorageClient = Depends(get_storage_client),
) -> Response:
    """Stream media bytes from private storage.

    Authorised via a short-lived signed token (see §4.1 of the media proxy spec).
    Supports HTTP Range requests for video seeking.
    """
    # 1. Validate token → get profile_id
    profile_id = verify_media_token(token, media_id)

    # 2. Look up media and verify ownership
    media = await service.get_media_for_profile(media_id, profile_id)

    # 3. Extract storage key and download (with Range support)
    key = _storage_key_from_url(media.storage_url)
    range_header = request.headers.get("range")
    result = await storage.download_range(key, range_header)

    # 5. Build response
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(result.content_length),
        "Cache-Control": "private, max-age=3600",
    }
    if result.content_range:
        headers["Content-Range"] = result.content_range

    return Response(
        content=result.content,
        status_code=result.status_code,
        media_type=result.content_type,
        headers=headers,
    )
