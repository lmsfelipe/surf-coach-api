import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base application error. Subclasses set code + status + default message."""

    code: str = "INTERNAL_ERROR"
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: Any | None = None,
    ) -> None:
        super().__init__(message or self.message)
        if message:
            self.message = message
        self.details = details


class MissingTokenError(AppError):
    code = "MISSING_TOKEN"
    status_code = status.HTTP_401_UNAUTHORIZED
    message = "Authorization header is missing."


class InvalidTokenError(AppError):
    code = "INVALID_TOKEN"
    status_code = status.HTTP_401_UNAUTHORIZED
    message = "The provided token is invalid or expired."


class ValidationError(AppError):
    code = "VALIDATION_ERROR"
    status_code = status.HTTP_400_BAD_REQUEST
    message = "Request payload failed validation."


class NotFoundError(AppError):
    code = "NOT_FOUND"
    status_code = status.HTTP_404_NOT_FOUND
    message = "Resource not found."


class ForbiddenError(AppError):
    code = "FORBIDDEN"
    status_code = status.HTTP_403_FORBIDDEN
    message = "You do not have access to this resource."


class ConflictError(AppError):
    code = "CONFLICT"
    status_code = status.HTTP_409_CONFLICT
    message = "A conflict occurred with the current state."


class InvalidMediaTypeError(AppError):
    code = "INVALID_MEDIA_TYPE"
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    message = "File type is not accepted."


class FileTooLargeError(AppError):
    code = "FILE_TOO_LARGE"
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    message = "File exceeds the maximum allowed size."


class VideoTooLongError(AppError):
    code = "VIDEO_TOO_LONG"
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    message = "Video exceeds the maximum allowed duration."


class NoMediaForSessionError(AppError):
    code = "NO_MEDIA_FOR_SESSION"
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    message = "No media found for this session. Upload media before generating a review."


class ReviewAlreadyExistsError(AppError):
    code = "REVIEW_ALREADY_EXISTS"
    status_code = status.HTTP_409_CONFLICT
    message = "A review already exists for this session."


class StorageUploadFailedError(AppError):
    code = "STORAGE_UPLOAD_FAILED"
    status_code = status.HTTP_502_BAD_GATEWAY
    message = "Media storage upload failed. Please try again."


class AIGenerationFailedError(AppError):
    code = "AI_GENERATION_FAILED"
    status_code = status.HTTP_502_BAD_GATEWAY
    message = "AI review generation failed. Please try again."


class AIParseFailedError(AppError):
    code = "AI_PARSE_FAILED"
    status_code = status.HTTP_502_BAD_GATEWAY
    message = "AI returned an unexpected response format."


class InvalidMediaError(AppError):
    code = "INVALID_MEDIA"
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    message = "Media file could not be processed."


def _envelope(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.exception("AppError 5xx: %s", exc.code)
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_envelope(
                "VALIDATION_ERROR",
                "Request payload failed validation.",
                jsonable_encoder(exc.errors()),
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "HTTP_ERROR"
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            code = "UNAUTHORIZED"
        elif exc.status_code == status.HTTP_404_NOT_FOUND:
            code = "NOT_FOUND"
        message = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(code, message),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled server error")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope("INTERNAL_ERROR", "An unexpected error occurred."),
        )
