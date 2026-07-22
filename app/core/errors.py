from typing import Any

import sentry_sdk
import structlog
from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = structlog.get_logger(__name__)


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


class TooFewPhotosError(AppError):
    code = "TOO_FEW_PHOTOS"
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    message = "At least 3 photos are required when uploading photos."


class TooManyPhotosError(AppError):
    code = "TOO_MANY_PHOTOS"
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    message = "A maximum of 10 photos can be uploaded at once."


class TooManyVideosError(AppError):
    code = "TOO_MANY_VIDEOS"
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    message = "A maximum of 3 videos can be uploaded at once."


class NoMediaForSessionError(AppError):
    code = "NO_MEDIA_FOR_SESSION"
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    message = "No media found for this session. Upload media before generating a review."


class ReviewAlreadyExistsError(AppError):
    code = "REVIEW_ALREADY_EXISTS"
    status_code = status.HTTP_409_CONFLICT
    message = "A review already exists for this session."


class TrainingPlanAlreadyExistsError(AppError):
    code = "TRAINING_PLAN_ALREADY_EXISTS"
    status_code = status.HTTP_409_CONFLICT
    message = "A training plan already exists for this review."


class ReviewNotRetryableError(AppError):
    code = "REVIEW_NOT_RETRYABLE"
    status_code = status.HTTP_409_CONFLICT
    message = "Only failed reviews can be retried."


class TrainingPlanNotRetryableError(AppError):
    code = "TRAINING_PLAN_NOT_RETRYABLE"
    status_code = status.HTTP_409_CONFLICT
    message = "Only failed training plans can be retried."


class ReviewNotFoundError(AppError):
    code = "REVIEW_NOT_FOUND"
    status_code = status.HTTP_404_NOT_FOUND
    message = "Review not found."


class SurfboardNotFoundError(AppError):
    code = "SURFBOARD_NOT_FOUND"
    status_code = status.HTTP_404_NOT_FOUND
    message = "Surfboard not found."


class SurfboardForbiddenError(AppError):
    code = "SURFBOARD_FORBIDDEN"
    status_code = status.HTTP_403_FORBIDDEN
    message = "This surfboard does not belong to you."


class TokenExpiredError(AppError):
    code = "TOKEN_EXPIRED"
    status_code = status.HTTP_401_UNAUTHORIZED
    message = "The access token has expired."


class RangeNotSatisfiableError(AppError):
    code = "RANGE_NOT_SATISFIABLE"
    status_code = 416
    message = "The requested byte range is not satisfiable."


class StorageUploadFailedError(AppError):
    code = "STORAGE_UPLOAD_FAILED"
    status_code = status.HTTP_502_BAD_GATEWAY
    message = "Media storage upload failed. Please try again."


class StorageDownloadError(AppError):
    code = "STORAGE_ERROR"
    status_code = status.HTTP_502_BAD_GATEWAY
    message = "Failed to retrieve media from storage."


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


class MediaNotSurfRelatedError(AppError):
    code = "MEDIA_NOT_SURF_RELATED"
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    message = "Uploaded media does not appear to be surf or water sports related."


class ExplicitContentError(AppError):
    code = "EXPLICIT_CONTENT"
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    message = "Uploaded media contains explicit or offensive content."


def _envelope(code: str, message: str, details: Any | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.exception("app_error_5xx", code=exc.code)
            sentry_sdk.capture_exception(exc)
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
        logger.exception("unhandled_server_error")
        sentry_sdk.capture_exception(exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope("INTERNAL_ERROR", "An unexpected error occurred."),
        )
