import json
import logging
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError

from app.core.config import get_settings
from app.core.errors import (
    AIGenerationFailedError,
    AIParseFailedError,
    ForbiddenError,
    NoMediaForSessionError,
    NotFoundError,
    ReviewAlreadyExistsError,
)
from app.core.frame_extractor import FrameExtractor
from app.core.security.jwt import AuthUser
from app.core.storage import StorageClient
from app.models.review import Review
from app.repositories.ai import ReviewRepository
from app.repositories.auth import AuthRepository
from app.repositories.media import MediaRepository
from app.repositories.sessions import SessionsRepository

logger = logging.getLogger(__name__)


class SurferContext(BaseModel):
    skill_level: str
    location: str
    wave_conditions: str | None = None
    board_type: str | None = None
    notes: str | None = None


class ScoreRubric(BaseModel):
    flow: float | None = Field(default=None, ge=0, le=10)
    drop: float | None = Field(default=None, ge=0, le=10)
    balance: float | None = Field(default=None, ge=0, le=10)
    wave_selection: float | None = Field(default=None, ge=0, le=10)
    maneuvers: float | None = Field(default=None, ge=0, le=10)
    arms: float | None = Field(default=None, ge=0, le=10)


class ReviewOutput(BaseModel):
    narrative: str
    improvement_tips: list[str]
    scores: ScoreRubric


SYSTEM_PERSONA = (
    "Você é um treinador de surf experiente com 20 anos de experiência analisando vídeos e fotos de surfe. "
    "Forneça feedback estruturado e acionável calibrado ao nível de habilidade do surfista. "
    "IMPORTANTE: avalie SOMENTE o que está visível nas mídias fornecidas. "
    "Se um aspecto técnico (ex: descida, manobra, uso dos braços) não aparecer nas imagens, "
    "não invente uma avaliação — retorne null na pontuação correspondente."
)

OUTPUT_SCHEMA_INSTRUCTION = (
    "Retorne SOMENTE JSON válido (sem markdown, sem preâmbulo, sem comentários extras) "
    "seguindo este schema:\n"
    "{\n"
    '  "narrative": string,                  // análise completa em prosa da performance do surfista\n'
    '  "improvement_tips": [string, string, string],  // exatamente 3 dicas acionáveis\n'
    '  "scores": {\n'
    '    "flow": number | null,              // 0.0 – 10.0; null se o fluxo geral não for visível\n'
    '    "drop": number | null,              // 0.0 – 10.0; null se a descida não aparecer na mídia\n'
    '    "balance": number | null,           // 0.0 – 10.0; null se o equilíbrio não for visível\n'
    '    "wave_selection": number | null,    // 0.0 – 10.0; null se a escolha da onda não for visível\n'
    '    "maneuvers": number | null,         // 0.0 – 10.0; null se nenhuma manobra for visível\n'
    '    "arms": number | null               // 0.0 – 10.0; null se o uso dos braços não for visível\n'
    "  }\n"
    "}"
)


def build_prompt(context: SurferContext) -> str:
    context_block = json.dumps(context.model_dump(), ensure_ascii=False, indent=2)
    return (
        f"{SYSTEM_PERSONA}\n\n"
        f"Surfer context:\n{context_block}\n\n"
        f"{OUTPUT_SCHEMA_INSTRUCTION}"
    )


def _strip_json_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = [ln for ln in stripped.splitlines() if not ln.startswith("```")]
        return "\n".join(lines).strip()
    return stripped


class GeminiService:
    def __init__(self, api_key: str | None = None, model_name: str | None = None) -> None:
        self.settings = get_settings()
        self.api_key = api_key or self.settings.GEMINI_API_KEY
        self.model_name = model_name or self.settings.GEMINI_MODEL

    def analyze_surf_media(
        self,
        images: list[bytes],
        context: SurferContext,
    ) -> ReviewOutput:
        import google.generativeai as genai
        from google.generativeai import protos

        genai.configure(api_key=self.api_key)
        model = genai.GenerativeModel(self.model_name)

        prompt = build_prompt(context)
        parts: list = [prompt]
        for img in images:
            parts.append(protos.Part(inline_data=protos.Blob(mime_type="image/jpeg", data=img)))

        try:
            response = model.generate_content(parts)
            text = getattr(response, "text", None) or ""
        except Exception as e:
            logger.exception("Gemini API call failed")
            raise AIGenerationFailedError() from e

        return self.parse_response(text)

    @staticmethod
    def parse_response(raw_text: str) -> ReviewOutput:
        cleaned = _strip_json_fences(raw_text)
        try:
            return ReviewOutput.model_validate_json(cleaned)
        except ValidationError as e:
            logger.exception("Gemini response failed Pydantic validation")
            raise AIParseFailedError() from e
        except ValueError as e:
            logger.exception("Gemini response was not valid JSON")
            raise AIParseFailedError() from e


def _clamp_round(value: float, ndigits: int = 1) -> float:
    v = max(0.0, min(10.0, float(value)))
    return round(v, ndigits)


def normalise_scores(scores: ScoreRubric) -> dict[str, Decimal | None]:
    def _process(v: float | None) -> Decimal | None:
        if v is None:
            return None
        return Decimal(str(_clamp_round(v)))

    keys = ("flow", "drop", "balance", "wave_selection", "maneuvers", "arms")
    values: dict[str, Decimal | None] = {k: _process(getattr(scores, k)) for k in keys}
    non_null = [float(v) for v in values.values() if v is not None]
    values["overall"] = Decimal(str(round(sum(non_null) / len(non_null), 1))) if non_null else None
    return values


class ReviewService:
    def __init__(
        self,
        sessions_repo: SessionsRepository,
        media_repo: MediaRepository,
        review_repo: ReviewRepository,
        auth_repo: AuthRepository,
        gemini: GeminiService,
        frame_extractor: FrameExtractor,
        storage: StorageClient,
    ) -> None:
        self.sessions_repo = sessions_repo
        self.media_repo = media_repo
        self.review_repo = review_repo
        self.auth_repo = auth_repo
        self.gemini = gemini
        self.frame_extractor = frame_extractor
        self.storage = storage
        self.settings = get_settings()

    async def generate_review(self, session_id: UUID, user: AuthUser) -> Review:
        session = await self.sessions_repo.get(session_id)
        if session is None:
            raise NotFoundError("Session not found.")
        if session.profile_id != user.id:
            raise ForbiddenError()

        existing = await self.review_repo.get_for_session(session_id)
        if existing is not None:
            raise ReviewAlreadyExistsError()

        media_items = await self.media_repo.list_for_session(session_id)
        if not media_items:
            raise NoMediaForSessionError()

        profile = await self.auth_repo.get_profile(user.id)
        skill_level = profile.surf_level if profile else "beginner"

        all_frames: list[bytes] = []
        for item in media_items:
            key = self._extract_key(item.storage_url, user.id, session_id, item.id)
            if not key:
                logger.warning("Could not derive storage key for media %s", item.id)
                continue
            raw = self.storage.download(key)
            if item.media_type == "video":
                frames = self.frame_extractor.extract(
                    raw, frame_count=self.settings.FRAME_EXTRACT_COUNT
                )
                all_frames.extend(frames)
            else:
                all_frames.append(raw)

        if not all_frames:
            raise NoMediaForSessionError("No decodable media found for this session.")

        context = SurferContext(
            skill_level=skill_level,
            location=session.location,
            wave_conditions=session.wave_conditions,
            board_type=session.board_type,
            notes=session.notes,
        )

        review_output = self.gemini.analyze_surf_media(all_frames, context)

        if len(review_output.improvement_tips) != 3:
            tips = list(review_output.improvement_tips)
            if len(tips) > 3:
                tips = tips[:3]
            while len(tips) < 3:
                tips.append("Continue praticando e grave mais sessões para um feedback mais detalhado.")
            review_output = ReviewOutput(
                narrative=review_output.narrative,
                improvement_tips=tips,
                scores=review_output.scores,
            )

        normalised = normalise_scores(review_output.scores)

        review = await self.review_repo.create(
            session_id=session_id,
            profile_id=user.id,
            narrative=review_output.narrative,
            improvement_tips=review_output.improvement_tips,
            score_flow=normalised["flow"],
            score_drop=normalised["drop"],
            score_balance=normalised["balance"],
            score_wave_selection=normalised["wave_selection"],
            score_maneuvers=normalised["maneuvers"],
            score_arms=normalised["arms"],
            overall_score=normalised["overall"],
            ai_model_version=self.settings.GEMINI_MODEL,
        )
        return review

    async def get_review(self, review_id: UUID, user: AuthUser) -> Review:
        review = await self.review_repo.get(review_id)
        if review is None:
            raise NotFoundError("Review not found.")
        if review.profile_id != user.id:
            raise ForbiddenError()
        return review

    async def get_review_for_session(self, session_id: UUID, user: AuthUser) -> Review:
        session = await self.sessions_repo.get(session_id)
        if session is None:
            raise NotFoundError("Session not found.")
        if session.profile_id != user.id:
            raise ForbiddenError()
        review = await self.review_repo.get_for_session(session_id)
        if review is None:
            raise NotFoundError("Review not found for this session.")
        return review

    @staticmethod
    def _extract_key(url: str, user_id, session_id, media_id) -> str | None:
        marker = f"{user_id}/{session_id}/"
        idx = url.find(marker)
        if idx < 0:
            return None
        return url[idx:].split("?", 1)[0]
