from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import db_session, get_current_user
from app.core.errors import NotFoundError
from app.core.frame_extractor import FrameExtractor
from app.core.rate_limit import limiter
from app.core.security.jwt import AuthUser
from app.core.security.media_token import mint_media_token, verify_media_token
from app.core.storage import StorageClient, get_storage_client
from app.core.upload import SpooledUpload, spool_upload
from app.models.media import Media
from app.repositories.media import MediaRepository
from app.repositories.sessions import SessionsRepository
from app.schemas.media import BatchUploadResult, MediaOut
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
    response_model=list[MediaOut],  # the 201 shape — unchanged
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_207_MULTI_STATUS: {"model": BatchUploadResult}},  # additive
)
@limiter.limit(lambda: get_settings().RATE_LIMIT_UPLOAD)
async def upload_media(
    request: Request,
    session_id: UUID,
    file: list[UploadFile] = File(...),
    user: AuthUser = Depends(get_current_user),
    service: MediaService = Depends(get_media_service),
) -> Any:
    # Part count first: it is the only check that costs nothing. The total body
    # size is already capped by BodySizeLimitMiddleware, which runs before the
    # multipart parser touches the stream.
    service.validate_file_count(len(file))

    max_bytes = get_settings().MAX_UPLOAD_SIZE_MB * 1024 * 1024
    uploads: list[SpooledUpload] = []
    try:
        # Spooling stays sequential — it reads one multipart body stream; only
        # the store leg inside upload_batch is parallelized.
        for part in file:
            uploads.append(await spool_upload(part, max_bytes))

        service.validate_upload_counts([u.head() for u in uploads])

        result = await service.upload_batch(session_id=session_id, uploads=uploads, user=user)
        succeeded = [_media_to_out(m, user.id) for m in result.succeeded]
        if not result.failed:
            return succeeded  # 201, list[MediaOut] — byte-for-byte as today

        # Some files stored, some hit STORAGE_UPLOAD_FAILED. Return a JSONResponse
        # so the 207 body bypasses response_model=list[MediaOut] (which would
        # otherwise try to coerce the envelope into a bare list).
        body = BatchUploadResult(succeeded=succeeded, failed=result.failed)
        return JSONResponse(
            status_code=status.HTTP_207_MULTI_STATUS,
            content=jsonable_encoder(body, by_alias=True),
        )
    finally:
        for upload in uploads:
            upload.close()


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
) -> StreamingResponse:
    """Stream media bytes from private storage.

    Authorised via a short-lived signed token (see §4.1 of the media proxy spec).
    Supports HTTP Range requests for video seeking.

    Bytes are relayed straight from storage to the client, so a range-less
    request for a full video costs a chunk of memory rather than the whole file.
    """
    # 1. Validate token → get profile_id
    profile_id = verify_media_token(token, media_id)

    # 2. Look up media and verify ownership
    media = await service.get_media_for_profile(media_id, profile_id)

    # 3. Extract storage key and open the upstream body (with Range support)
    key = _storage_key_from_url(media.storage_url)
    range_header = request.headers.get("range")
    stream = await storage.stream_range(key, range_header)

    # 4. Build response
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=3600",
    }
    if stream.content_length is not None:
        headers["Content-Length"] = str(stream.content_length)
    if stream.content_range:
        headers["Content-Range"] = stream.content_range

    async def _body():
        try:
            async for chunk in stream.aiter_bytes():
                yield chunk
        finally:
            await stream.aclose()

    return StreamingResponse(
        _body(),
        status_code=stream.status_code,
        media_type=stream.content_type,
        headers=headers,
    )
