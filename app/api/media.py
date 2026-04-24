from uuid import UUID

from fastapi import APIRouter, Depends, File, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import db_session, get_current_user
from app.core.frame_extractor import FrameExtractor
from app.core.security.jwt import AuthUser
from app.core.storage import StorageClient, get_storage_client
from app.repositories.media import MediaRepository
from app.repositories.sessions import SessionsRepository
from app.schemas.media import MediaOut
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
    )


@router.post(
    "/sessions/{session_id}/media/",
    response_model=MediaOut,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
)
async def upload_media(
    session_id: UUID,
    file: UploadFile = File(...),
    user: AuthUser = Depends(get_current_user),
    service: MediaService = Depends(get_media_service),
) -> MediaOut:
    data = await file.read()
    media = await service.upload(
        session_id=session_id,
        file_bytes=data,
        file_name=file.filename or "upload",
        user=user,
    )
    return MediaOut.model_validate(media)


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
    return MediaOut.model_validate(media)


@router.delete("/media/{media_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_media(
    media_id: UUID,
    user: AuthUser = Depends(get_current_user),
    service: MediaService = Depends(get_media_service),
) -> None:
    await service.delete_media(media_id, user)
