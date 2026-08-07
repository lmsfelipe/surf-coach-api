import json
import random
import time
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from uuid import UUID

import structlog
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.errors import (
    AIGenerationFailedError,
    AIParseFailedError,
    ForbiddenError,
    NoMediaForSessionError,
    NotFoundError,
    ReviewAlreadyExistsError,
    ReviewNotFoundError,
    ReviewNotRetryableError,
    TrainingPlanAlreadyExistsError,
    TrainingPlanNotRetryableError,
)
from app.core.frame_extractor import FrameExtractor
from app.core.security.jwt import AuthUser
from app.core.storage import StorageClient
from app.models.review import Review
from app.models.training_plan import TrainingPlan
from app.repositories.ai import ReviewRepository, TrainingPlanRepository
from app.repositories.auth import AuthRepository
from app.repositories.media import MediaRepository
from app.repositories.sessions import SessionsRepository
from app.repositories.surfboards import SurfboardRepository

logger = structlog.get_logger(__name__)


class SurferContext(BaseModel):
    """Objective, media-independent facts used to calibrate the visual analysis.

    The surfer's own free-text description is deliberately NOT part of this
    context: it must never reach the scoring pass, or an optimistic/pessimistic
    tone in the text skews the scores. The description is applied afterwards, in
    a second pass, to personalise the narrative only. See ``ReviewService``.
    """

    skill_level: str
    location: str
    wave_conditions: str | None = None
    board_type: str | None = None


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


class NarrativeRefinement(BaseModel):
    """Second-pass output: the narrative/tips personalised with the surfer's
    description. Scores are intentionally absent — they stay locked to the
    media-only analysis and are never regenerated here."""

    narrative: str
    improvement_tips: list[str]


class ModerationOutput(BaseModel):
    surf_related: bool
    explicit_content: bool
    reason: str


SYSTEM_PERSONA = (
    "Você é um treinador de surf experiente com 20 anos de experiência analisando vídeos e fotos de surfe. "  # noqa: E501
    "Forneça feedback estruturado e acionável calibrado ao nível de habilidade do surfista. "
    "IMPORTANTE: avalie SOMENTE o que está visível nas mídias fornecidas. "
    "Se um aspecto técnico (ex: drop, manobra, uso dos braços) não aparecer nas imagens, "
    "não invente uma avaliação — retorne null na pontuação correspondente."
)

OUTPUT_SCHEMA_INSTRUCTION = (
    "Retorne SOMENTE JSON válido (sem markdown, sem preâmbulo, sem comentários extras) "
    "seguindo este schema:\n"
    "{\n"
    '  "narrative": string,                  // análise completa em prosa da performance do surfista\n'  # noqa: E501
    '  "improvement_tips": [string, string, string],  // exatamente 3 dicas acionáveis\n'
    '  "scores": {\n'
    '    "flow": number | null,              // 0.0 – 10.0; null se o fluxo geral não for visível\n'
    '    "drop": number | null,              // 0.0 – 10.0; null se a descida não aparecer na mídia\n'  # noqa: E501
    '    "balance": number | null,           // 0.0 – 10.0; null se o equilíbrio não for visível\n'
    '    "wave_selection": number | null,    // 0.0 – 10.0; null se a escolha da onda não for visível\n'  # noqa: E501
    '    "maneuvers": number | null,         // 0.0 – 10.0; null se nenhuma manobra for visível\n'
    '    "arms": number | null               // 0.0 – 10.0; null se o uso dos braços não for visível\n'  # noqa: E501
    "  }\n"
    "}"
)


MODERATION_PROMPT = (
    "You are a content moderation system for a surf coaching app. "
    "Analyse the provided image(s) and answer two questions: "
    "1) Is this content related to surfing or water sports (surfing, bodyboarding, SUP, kitesurfing, ocean swimming)? "  # noqa: E501
    "2) Does it contain explicit, adult, or offensive material?\n\n"
    "Return ONLY valid JSON (no markdown, no preamble):\n"
    '{"surf_related": boolean, "explicit_content": boolean, "reason": string}\n'
    "reason: one short sentence explaining your decision."
)


REFINE_PERSONA = (
    "Você é o mesmo treinador de surf experiente. "
    "Você JÁ analisou as mídias e produziu a avaliação técnica abaixo, "
    "baseada EXCLUSIVAMENTE no que estava visível nas imagens. "
    "Agora o surfista compartilhou um relato pessoal sobre a sessão. "
    "Use esse relato APENAS para tornar a narrativa e as dicas mais relevantes e pessoais — "
    "por exemplo, reconhecendo as sensações, dúvidas ou objetivos que o surfista mencionou. "
    "NUNCA deixe o tom do relato (otimista ou pessimista) mudar a sua avaliação técnica: "
    "ela reflete apenas o que foi observado nas mídias, e as pontuações permanecem inalteradas. "
    "Não contradiga a evidência visual. Mantenha exatamente 3 dicas acionáveis."
)

REFINE_OUTPUT_SCHEMA_INSTRUCTION = (
    "Retorne SOMENTE JSON válido (sem markdown, sem preâmbulo, sem comentários extras) "
    "seguindo este schema:\n"
    "{\n"
    '  "narrative": string,                            // narrativa refinada que incorpora o relato\n'  # noqa: E501
    '  "improvement_tips": [string, string, string]    // exatamente 3 dicas acionáveis\n'
    "}"
)


def build_prompt(context: SurferContext, description: str | None = None) -> str:
    context_block = json.dumps(context.model_dump(), ensure_ascii=False, indent=2)
    prompt = f"{SYSTEM_PERSONA}\n\nSurfer context:\n{context_block}\n\n{OUTPUT_SCHEMA_INSTRUCTION}"
    # Single-pass mode only: the surfer's free-text account is appended with an
    # explicit guardrail that it may shape the narrative/tips wording but must
    # never move the scores. Two-pass callers omit ``description``, so this path
    # is inert and the prompt is byte-identical to before.
    if description:
        prompt += (
            "\n\nRelato pessoal do surfista (use APENAS para personalizar a narrativa e as "
            "dicas; as pontuações refletem SOMENTE o que está visível na mídia e NUNCA podem "
            f"ser alteradas pelo tom do relato):\n{description}"
        )
    return prompt


def build_refinement_prompt(
    review: ReviewOutput,
    context: SurferContext,
    description: str,
) -> str:
    review_block = json.dumps(
        {
            "narrative": review.narrative,
            "improvement_tips": review.improvement_tips,
            "scores": review.scores.model_dump(),
        },
        ensure_ascii=False,
        indent=2,
    )
    context_block = json.dumps(context.model_dump(), ensure_ascii=False, indent=2)
    return (
        f"{REFINE_PERSONA}\n\n"
        f"Contexto objetivo do surfista:\n{context_block}\n\n"
        f"Avaliação técnica baseada nas mídias (pontuações fixas):\n{review_block}\n\n"
        f"Relato do surfista sobre a sessão:\n{description}\n\n"
        f"{REFINE_OUTPUT_SCHEMA_INSTRUCTION}"
    )


def _strip_json_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = [ln for ln in stripped.splitlines() if not ln.startswith("```")]
        return "\n".join(lines).strip()
    return stripped


def _strip_trailing_commas(text: str) -> str:
    """Drop commas that directly precede a closing brace/bracket.

    Schema-constrained generation should make this unnecessary, but a malformed
    trailing comma is the one syntax slip models still produce, and it costs a
    whole review to reject. Commas inside string literals are left untouched.
    """
    out: list[str] = []
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            out.append(ch)
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            continue
        if ch == ",":
            rest = text[i + 1 :]
            if rest.lstrip()[:1] in ("}", "]"):
                continue
        out.append(ch)
    return "".join(out)


@lru_cache(maxsize=1)
def _gemini_client(api_key: str):
    """Return a process-wide Gemini client, built once per API key.

    Constructing ``genai.Client`` per call spins up a fresh httpx connection
    pool every time, so each request pays TLS/connection setup with no
    keep-alive reuse. Caching one client lets every Gemini call (review,
    refine, moderation, training plan) share pooled connections. The client is
    httpx-backed and safe to share across the ``asyncio.to_thread`` workers.
    """
    from google import genai

    return genai.Client(api_key=api_key)


_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_RETRYABLE_EXC_NAMES = frozenset(
    {
        "ServerError",  # google.genai.errors.ServerError (5xx)
        "ConnectError",
        "ConnectTimeout",
        "ReadError",
        "ReadTimeout",
        "WriteError",
        "WriteTimeout",
        "PoolTimeout",
        "RemoteProtocolError",  # stale keep-alive socket closed by the peer
        "TransportError",
        "ConnectionError",
    }
)


def _is_retryable(exc: BaseException) -> bool:
    """True for transient Gemini failures worth retrying.

    Covers overload/rate-limit responses (HTTP 429 and 5xx, carried on the
    google-genai error's ``code``) and connection-level errors — including the
    stale pooled sockets that come with reusing a keep-alive client. Wrapped
    causes are followed. Non-transient errors (bad request, auth) return False.
    """
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(code, int) and code in _RETRYABLE_STATUS:
        return True
    if type(exc).__name__ in _RETRYABLE_EXC_NAMES:
        return True
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        return _is_retryable(cause)
    return False


def _generate_content_with_retry(
    client,
    *,
    model: str,
    contents,
    config,
    max_attempts: int,
    base_delay: float,
):
    """Call ``generate_content`` with exponential backoff on transient errors.

    This runs inside ``asyncio.to_thread`` (every caller is a sync method
    dispatched off the event loop), so the blocking ``time.sleep`` between
    attempts does not stall the loop. Non-retryable errors propagate immediately.
    """
    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        try:
            return client.models.generate_content(model=model, contents=contents, config=config)
        except Exception as exc:  # noqa: BLE001 — re-raised below when not retryable
            if attempt >= attempts or not _is_retryable(exc):
                raise
            delay = base_delay * (2 ** (attempt - 1))
            delay += random.uniform(0, delay * 0.25)  # jitter to avoid synchronised retries
            logger.warning(
                "Gemini call failed; retrying",
                attempt=attempt,
                max_attempts=attempts,
                retry_in_sec=round(delay, 2),
                error=str(exc),
            )
            time.sleep(delay)


def _thinking_config(level: str):
    """Build a ThinkingConfig for the configured thinking level, or None.

    Gemini 3.x models think at MEDIUM by default; lowering to LOW/MINIMAL cuts
    latency for structured tasks like scoring. ``thinking_level`` is only valid
    for 3.x models, so an empty or unknown level yields None — safe to pass
    through to any model (``thinking_config=None`` is a no-op) and correct for
    2.x models, which do not support it.
    """
    normalized = (level or "").strip().upper()
    if not normalized:
        return None
    from google.genai import types

    try:
        return types.ThinkingConfig(thinking_level=types.ThinkingLevel[normalized])
    except KeyError:
        logger.warning("Unknown GEMINI_THINKING_LEVEL; using model default", level=level)
        return None


class GeminiService:
    def __init__(self, api_key: str | None = None, model_name: str | None = None) -> None:
        self.settings = get_settings()
        self.api_key = api_key or self.settings.GEMINI_API_KEY
        self.model_name = model_name or self.settings.GEMINI_MODEL

    def analyze_surf_media(
        self,
        images: list[bytes],
        context: SurferContext,
        description: str | None = None,
        temperature: float | None = None,
    ) -> ReviewOutput:
        """Score the media and produce the narrative/tips.

        Two-pass callers pass neither ``description`` nor ``temperature``, and
        the call is byte-identical to the media-only scoring pass. Single-pass
        callers pass the surfer's description (folded into the prompt with a
        score guardrail) and a low ``temperature`` to keep the scores stable.
        """
        from google.genai import types

        client = _gemini_client(self.api_key)

        prompt = build_prompt(context, description)
        parts: list = [prompt]
        for img in images:
            parts.append(types.Part.from_bytes(data=img, mime_type="image/jpeg"))

        config_kwargs: dict = {
            "response_mime_type": "application/json",
            "response_schema": ReviewOutput,
        }
        if temperature is not None:
            config_kwargs["temperature"] = temperature
        config_kwargs["thinking_config"] = _thinking_config(self.settings.GEMINI_THINKING_LEVEL)

        try:
            response = _generate_content_with_retry(
                client,
                model=self.model_name,
                contents=parts,
                config=types.GenerateContentConfig(**config_kwargs),
                max_attempts=self.settings.GEMINI_MAX_ATTEMPTS,
                base_delay=self.settings.GEMINI_RETRY_BASE_DELAY_SEC,
            )
            text = getattr(response, "text", None) or ""
        except Exception as e:
            logger.exception("Gemini API call failed")
            raise AIGenerationFailedError() from e

        return self.parse_response(text)

    @staticmethod
    def parse_response(raw_text: str) -> ReviewOutput:
        cleaned = _strip_json_fences(raw_text)
        # ValidationError subclasses ValueError, so this catches both a schema
        # mismatch and malformed JSON; the retry only rescues the latter.
        try:
            return ReviewOutput.model_validate_json(cleaned)
        except ValueError as e:
            try:
                return ReviewOutput.model_validate_json(_strip_trailing_commas(cleaned))
            except ValueError:
                logger.exception("Gemini response could not be parsed")
                raise AIParseFailedError() from e

    def refine_review_with_description(
        self,
        review: ReviewOutput,
        context: SurferContext,
        description: str,
    ) -> ReviewOutput:
        """Second pass: personalise the narrative/tips using the surfer's
        free-text description. Scores stay locked to the media-only ``review``
        — they are copied through here and never regenerated, so the tone of
        the description cannot move them.
        """
        from google.genai import types

        client = _gemini_client(self.api_key)

        prompt = build_refinement_prompt(review, context, description)
        try:
            response = _generate_content_with_retry(
                client,
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=NarrativeRefinement,
                    thinking_config=_thinking_config(self.settings.GEMINI_THINKING_LEVEL),
                ),
                max_attempts=self.settings.GEMINI_MAX_ATTEMPTS,
                base_delay=self.settings.GEMINI_RETRY_BASE_DELAY_SEC,
            )
            text = getattr(response, "text", None) or ""
        except Exception as e:
            logger.exception("Gemini refinement call failed")
            raise AIGenerationFailedError() from e

        refinement = self.parse_refinement(text)
        return ReviewOutput(
            narrative=refinement.narrative,
            improvement_tips=refinement.improvement_tips,
            scores=review.scores,
        )

    @staticmethod
    def parse_refinement(raw_text: str) -> NarrativeRefinement:
        cleaned = _strip_json_fences(raw_text)
        try:
            return NarrativeRefinement.model_validate_json(cleaned)
        except ValueError as e:
            try:
                return NarrativeRefinement.model_validate_json(_strip_trailing_commas(cleaned))
            except ValueError:
                logger.exception("Gemini refinement response could not be parsed")
                raise AIParseFailedError() from e

    def moderate_media_content(
        self,
        images: list[bytes],
        mime_type: str = "image/jpeg",
    ) -> ModerationOutput:
        from google.genai import types

        client = _gemini_client(self.api_key)

        parts: list = [MODERATION_PROMPT]
        for img in images:
            parts.append(types.Part.from_bytes(data=img, mime_type=mime_type))

        try:
            response = _generate_content_with_retry(
                client,
                model=self.model_name,
                contents=parts,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ModerationOutput,
                    thinking_config=_thinking_config(self.settings.GEMINI_THINKING_LEVEL),
                ),
                max_attempts=self.settings.GEMINI_MAX_ATTEMPTS,
                base_delay=self.settings.GEMINI_RETRY_BASE_DELAY_SEC,
            )
            raw_text = getattr(response, "text", None) or ""
        except Exception as e:
            logger.exception("Gemini moderation API call failed")
            raise AIGenerationFailedError() from e

        cleaned = _strip_json_fences(raw_text)
        try:
            return ModerationOutput.model_validate_json(cleaned)
        except ValueError as e:
            try:
                return ModerationOutput.model_validate_json(_strip_trailing_commas(cleaned))
            except ValueError:
                logger.exception("Gemini moderation response parse failed")
                raise AIParseFailedError() from e

    def generate_training_plan(self, context: "TrainingContext") -> "TrainingPlanOutput":
        from google.genai import types

        client = _gemini_client(self.api_key)

        workout_count = get_settings().TRAINING_WORKOUTS_PER_PLAN
        system_persona = (
            "Você é um treinador especialista em preparação física para surf. "
            f"Gere um plano de treino estruturado com exatamente {workout_count} treinos, "
            "personalizado com base nos dados de performance e áreas de melhoria do surfista. "
            "Para cada exercício, o campo 'description' deve conter instruções claras e "
            "objetivas de COMO executar o movimento com boa técnica (posição do corpo, "
            "alinhamento, amplitude e controle) — como uma explicação geral e educativa da "
            "execução correta, e NÃO uma descrição do benefício do exercício. "
            "Ex.: 'Realize o agachamento mantendo o tronco o mais vertical possível, focando "
            "na flexão dos joelhos para baixar o centro de gravidade sem inclinar as costas.' "
            "— e não 'Exercício que costuma ajudar com a mobilidade da coluna torácica.'. "
            "Trate essas instruções como orientações gerais de movimento e não como uma "
            "prescrição individualizada. "
            "Foque em mobilidade, equilíbrio, estabilidade de core e mecânica do pop-up, "
            "usando movimentos de baixo risco com o peso do próprio corpo. "
            "NÃO prescreva cargas externas, pesos, halteres, barra, kettlebell, programas "
            "de força com carga, nem parâmetros específicos de resistência (ex.: '4x12 "
            "agachamentos com X kg'). "
            "Nunca presuma o histórico de lesões, condição médica ou nível de "
            "condicionamento do surfista. "
            "Mantenha os exercícios seguros para executar sem supervisão; se alguma "
            "limitação for relevante, recomende consultar um profissional qualificado. "
            "Responda sempre em português do Brasil."
        )
        context_block = json.dumps(context.model_dump(), ensure_ascii=False, indent=2)
        output_schema = (
            "Retorne SOMENTE JSON válido (sem markdown, sem preâmbulo) seguindo este schema:\n"
            "{\n"
            '  "workouts": [\n'
            "    {\n"
            '      "sequence_number": 1,\n'
            '      "title": string,\n'
            '      "focus_area": string,\n'
            '      "exercises": [\n'
            "        {\n"
            '          "name": string,\n'
            '          "description": string,  // instruções de execução do movimento, não o benefício\n'  # noqa: E501
            '          "sets": inteiro >= 1,\n'
            '          "reps": string,\n'
            '          "video_url": string | null\n'
            "        }\n"
            "      ]\n"
            "    }\n"
            "  ]\n"
            "}\n"
            f"Exatamente {workout_count} treinos. Cada treino deve ter de 4 a 6 exercícios. "
            "video_url deve ser null, a menos que uma URL pública conhecida seja adequada."
        )
        prompt = f"{system_persona}\n\nSurfer context:\n{context_block}\n\n{output_schema}"

        try:
            response = _generate_content_with_retry(
                client,
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=TrainingPlanOutput,
                    thinking_config=_thinking_config(self.settings.GEMINI_THINKING_LEVEL),
                ),
                max_attempts=self.settings.GEMINI_MAX_ATTEMPTS,
                base_delay=self.settings.GEMINI_RETRY_BASE_DELAY_SEC,
            )
            raw_text = getattr(response, "text", None) or ""
        except Exception as e:
            logger.exception("Gemini API call failed (training plan)")
            raise AIGenerationFailedError() from e

        cleaned = _strip_json_fences(raw_text)
        try:
            output = TrainingPlanOutput.model_validate_json(cleaned)
        except ValueError as e:
            try:
                output = TrainingPlanOutput.model_validate_json(_strip_trailing_commas(cleaned))
            except ValueError:
                logger.exception("Gemini training plan response failed parsing")
                raise AIParseFailedError() from e

        if len(output.workouts) != workout_count:
            raise AIParseFailedError(
                f"Expected {workout_count} workouts, got {len(output.workouts)}."
            )
        return output


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
    # Matches the worker sweeper's message (worker.SWEEP_MESSAGE).
    STUCK_PROCESSING_MESSAGE = "Processing was interrupted."

    def __init__(
        self,
        sessions_repo: SessionsRepository,
        media_repo: MediaRepository,
        review_repo: ReviewRepository,
        auth_repo: AuthRepository,
        surfboard_repo: SurfboardRepository,
        gemini: GeminiService,
        frame_extractor: FrameExtractor,
        storage: StorageClient,
    ) -> None:
        self.sessions_repo = sessions_repo
        self.media_repo = media_repo
        self.review_repo = review_repo
        self.auth_repo = auth_repo
        self.surfboard_repo = surfboard_repo
        self.gemini = gemini
        self.frame_extractor = frame_extractor
        self.storage = storage
        self.settings = get_settings()

    async def enqueue_review(self, session_id: UUID, user: AuthUser) -> Review:
        """Validate inputs, create a pending review row, and return it.

        The caller (API route) is responsible for enqueueing the worker job.
        """
        session = await self.sessions_repo.get(session_id)
        if session is None:
            raise NotFoundError("Session not found.")
        if session.profile_id != user.id:
            raise ForbiddenError()

        existing = await self.review_repo.get_for_session(session_id)
        if existing is not None and existing.status in ("completed", "processing"):
            raise ReviewAlreadyExistsError()

        media_items = await self.media_repo.list_for_session(session_id)
        if not media_items:
            raise NoMediaForSessionError()

        # If a previously failed review exists, delete it so we can create fresh
        if existing is not None and existing.status == "failed":
            await self.review_repo.delete(existing.id)

        return await self.review_repo.create_pending(session_id=session_id, profile_id=user.id)

    async def process_review(self, review_id: UUID) -> Review:
        """Heavy processing — called by the arq worker, not from an HTTP handler."""
        import asyncio

        review = await self.review_repo.get(review_id)
        if review is None:
            raise NotFoundError("Review not found.")

        session = await self.sessions_repo.get(review.session_id)
        if session is None:
            raise NotFoundError("Session not found.")

        media_items = await self.media_repo.list_for_session(review.session_id)

        profile = await self.auth_repo.get_profile(review.profile_id)
        skill_level = profile.surf_level if profile else "beginner"

        all_frames: list[bytes] = []
        for item in media_items:
            key = self._extract_key(item.storage_url, review.profile_id, review.session_id, item.id)
            if not key:
                logger.warning("Could not derive storage key for media %s", item.id)
                continue
            raw = await asyncio.to_thread(self.storage.download, key)
            if item.media_type == "video":
                frames = await asyncio.to_thread(
                    self.frame_extractor.extract,
                    raw,
                    self.settings.FRAME_EXTRACT_COUNT,
                )
                all_frames.extend(frames)
            else:
                all_frames.append(raw)

        if not all_frames:
            raise NoMediaForSessionError("No decodable media found for this session.")

        board_type: str | None = None
        if session.surfboard_id is not None:
            surfboard = await self.surfboard_repo.get_by_id(session.surfboard_id)
            if surfboard is not None:
                board_type = surfboard.board_type

        context = SurferContext(
            skill_level=skill_level,
            location=session.location,
            wave_conditions=f"{session.wave_size} m",
            board_type=board_type,
        )

        description = (session.notes or "").strip()

        if self.settings.SINGLE_PASS_REVIEW:
            # Single-pass (behind a flag, default OFF): media + description in one
            # Gemini call. build_prompt guards the scores as media-only and a low
            # temperature keeps them stable, but the description is now visible to
            # the scoring context — an instructional guarantee, not the structural
            # isolation the two-pass path below provides. Kept off in production
            # until score-bias validation confirms the description does not move
            # the scores.
            review_output = await asyncio.to_thread(
                self.gemini.analyze_surf_media,
                all_frames,
                context,
                description or None,
                self.settings.GEMINI_TEMPERATURE,
            )
        else:
            # Pass 1 — score the media in isolation. The surfer's description is
            # deliberately kept out of this call so its tone cannot bias the scores.
            review_output = await asyncio.to_thread(
                self.gemini.analyze_surf_media, all_frames, context
            )

            # Pass 2 — personalise the narrative/tips with the surfer's description.
            # Scores are locked to pass 1 (see refine_review_with_description). This
            # is best-effort: if it fails we keep the media-only review rather than
            # failing the whole job.
            if description:
                try:
                    review_output = await asyncio.to_thread(
                        self.gemini.refine_review_with_description,
                        review_output,
                        context,
                        description,
                    )
                except (AIGenerationFailedError, AIParseFailedError):
                    logger.warning(
                        "Description refinement failed; keeping media-only review",
                        review_id=str(review_id),
                    )

        if len(review_output.improvement_tips) != 3:
            tips = list(review_output.improvement_tips)
            if len(tips) > 3:
                tips = tips[:3]
            while len(tips) < 3:
                tips.append(
                    "Continue praticando e grave mais sessões para um feedback mais detalhado."
                )
            review_output = ReviewOutput(
                narrative=review_output.narrative,
                improvement_tips=tips,
                scores=review_output.scores,
            )

        normalised = normalise_scores(review_output.scores)

        return await self.review_repo.mark_completed(
            review_id,
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

    async def retry_review(self, review_id: UUID, user: AuthUser) -> Review:
        """Reset a failed review to processing. Caller enqueues the worker job."""
        review = await self.review_repo.get(review_id)
        if review is None:
            raise NotFoundError("Review not found.")
        if review.profile_id != user.id:
            raise ForbiddenError()
        if review.status != "failed":
            raise ReviewNotRetryableError()
        return await self.review_repo.reset_for_retry(review_id)

    async def _fail_if_stale(self, review: Review) -> Review:
        """Fail a review left in 'processing' past the stuck threshold.

        The worker's sweeper normally does this, but it only runs inside the
        worker process. If the worker is offline (yet Redis accepted the enqueue),
        the job never runs and the sweeper never fires, so the row would stay
        'processing' forever and the client would poll in an endless loop. This
        read-path check is the worker-independent safety net that breaks it.
        """
        if review.status != "processing":
            return review
        started = review.processing_started_at or review.created_at
        if started is None:
            return review
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        age = (datetime.now(tz=UTC) - started).total_seconds()
        if age <= self.settings.STUCK_JOB_THRESHOLD_SEC:
            return review
        logger.warning(
            "Review stuck in processing; failing on read",
            review_id=str(review.id),
            age_sec=int(age),
        )
        return await self.review_repo.mark_failed(review.id, self.STUCK_PROCESSING_MESSAGE)

    async def get_review(self, review_id: UUID, user: AuthUser) -> Review:
        review = await self.review_repo.get(review_id)
        if review is None:
            raise NotFoundError("Review not found.")
        if review.profile_id != user.id:
            raise ForbiddenError()
        return await self._fail_if_stale(review)

    async def get_review_for_session(self, session_id: UUID, user: AuthUser) -> Review:
        session = await self.sessions_repo.get(session_id)
        if session is None:
            raise NotFoundError("Session not found.")
        if session.profile_id != user.id:
            raise ForbiddenError()
        review = await self.review_repo.get_for_session(session_id)
        if review is None:
            raise NotFoundError("Review not found for this session.")
        return await self._fail_if_stale(review)

    @staticmethod
    def _extract_key(url: str, user_id, session_id, media_id) -> str | None:
        marker = f"{user_id}/{session_id}/"
        idx = url.find(marker)
        if idx < 0:
            return None
        return url[idx:].split("?", 1)[0]


# ---------------------------------------------------------------------------
# Training plan Pydantic I/O models
# ---------------------------------------------------------------------------


class TrainingContext(BaseModel):
    surf_level: str
    improvement_tips: list[str]
    score_flow: float | None
    score_balance: float | None
    score_maneuvers: float | None
    score_wave_selection: float | None
    score_drop: float | None
    score_arms: float | None
    overall_score: float | None
    height_cm: int | None
    weight_kg: int | None


class ExerciseOutput(BaseModel):
    name: str
    description: str
    sets: int
    reps: str
    video_url: str | None = None


class WorkoutOutput(BaseModel):
    sequence_number: int
    title: str
    focus_area: str
    exercises: list[ExerciseOutput]


class TrainingPlanOutput(BaseModel):
    workouts: list[WorkoutOutput]


# ---------------------------------------------------------------------------
# TrainingService
# ---------------------------------------------------------------------------


class TrainingService:
    def __init__(
        self,
        review_repo: ReviewRepository,
        auth_repo: AuthRepository,
        training_plan_repo: TrainingPlanRepository,
        gemini: GeminiService,
    ) -> None:
        self.review_repo = review_repo
        self.auth_repo = auth_repo
        self.training_plan_repo = training_plan_repo
        self.gemini = gemini
        self.settings = get_settings()

    async def enqueue_training_plan(self, review_id: UUID, user: AuthUser) -> TrainingPlan:
        """Validate inputs, create a pending plan row, and return it."""
        review = await self.review_repo.get(review_id)
        if review is None:
            raise ReviewNotFoundError()
        if review.profile_id != user.id:
            raise ForbiddenError()

        existing = await self.training_plan_repo.get_by_review_id(review_id)
        if existing is not None and existing.status in ("completed", "processing"):
            raise TrainingPlanAlreadyExistsError()

        if existing is not None and existing.status == "failed":
            await self.training_plan_repo.delete(existing.id)

        return await self.training_plan_repo.create_pending(review_id=review_id, profile_id=user.id)

    async def process_training_plan(self, plan_id: UUID) -> TrainingPlan:
        """Heavy processing — called by the arq worker."""
        import asyncio

        plan = await self.training_plan_repo.get_by_id(plan_id)
        if plan is None:
            raise NotFoundError("Training plan not found.")

        review = await self.review_repo.get(plan.review_id)
        if review is None:
            raise ReviewNotFoundError()

        profile = await self.auth_repo.get_profile(plan.profile_id)

        context = TrainingContext(
            surf_level=profile.surf_level if profile else "beginner",
            improvement_tips=list(review.improvement_tips or []),
            score_flow=float(review.score_flow) if review.score_flow is not None else None,
            score_balance=float(review.score_balance) if review.score_balance is not None else None,
            score_maneuvers=(
                float(review.score_maneuvers) if review.score_maneuvers is not None else None
            ),
            score_wave_selection=(
                float(review.score_wave_selection)
                if review.score_wave_selection is not None
                else None
            ),
            score_drop=float(review.score_drop) if review.score_drop is not None else None,
            score_arms=float(review.score_arms) if review.score_arms is not None else None,
            overall_score=float(review.overall_score) if review.overall_score is not None else None,
            height_cm=profile.height_cm if profile else None,
            weight_kg=profile.weight_kg if profile else None,
        )

        plan_output = await asyncio.to_thread(self.gemini.generate_training_plan, context)

        workouts_data = [
            {
                "sequence_number": w.sequence_number,
                "title": w.title,
                "focus_area": w.focus_area,
                "exercises": [
                    {
                        "name": ex.name,
                        "description": ex.description,
                        "sets": ex.sets,
                        "reps": ex.reps,
                        "video_url": ex.video_url,
                    }
                    for ex in w.exercises
                ],
            }
            for w in plan_output.workouts
        ]

        return await self.training_plan_repo.mark_completed(
            plan_id,
            ai_model_version=self.settings.GEMINI_MODEL,
            workouts=workouts_data,
        )

    async def retry_training_plan(self, plan_id: UUID, user: AuthUser) -> TrainingPlan:
        """Reset a failed plan to processing. Caller enqueues the worker job."""
        plan = await self.training_plan_repo.get_by_id(plan_id)
        if plan is None:
            raise NotFoundError("Training plan not found.")
        if plan.profile_id != user.id:
            raise ForbiddenError()
        if plan.status != "failed":
            raise TrainingPlanNotRetryableError()
        return await self.training_plan_repo.reset_for_retry(plan_id)

    async def list_plans(self, user: AuthUser) -> list[TrainingPlan]:
        return await self.training_plan_repo.list_for_profile(user.id)

    async def get_plan_by_id(self, plan_id: UUID, user: AuthUser) -> TrainingPlan:
        plan = await self.training_plan_repo.get_by_id(plan_id)
        if plan is None:
            raise NotFoundError("Training plan not found.")
        if plan.profile_id != user.id:
            raise ForbiddenError()
        return plan

    async def get_plan_by_review_id(self, review_id: UUID, user: AuthUser) -> TrainingPlan:
        review = await self.review_repo.get(review_id)
        if review is None:
            raise NotFoundError("Review not found.")
        if review.profile_id != user.id:
            raise ForbiddenError()
        plan = await self.training_plan_repo.get_by_review_id(review_id)
        if plan is None:
            raise NotFoundError("No training plan exists for this review yet.")
        return plan

    async def get_workout_by_id(self, workout_id: UUID, user: AuthUser):
        workout = await self.training_plan_repo.get_workout_by_id(workout_id)
        if workout is None:
            raise NotFoundError("Workout not found.")
        plan = await self.training_plan_repo.get_by_id(workout.plan_id)
        if plan is None or plan.profile_id != user.id:
            raise ForbiddenError()
        return workout
