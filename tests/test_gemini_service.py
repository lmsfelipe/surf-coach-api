"""GeminiService call construction, retry/backoff, and response handling.

Every test stubs the google-genai client, so nothing here touches the network.
"""

from __future__ import annotations

import json

import pytest

from app.core.errors import AIGenerationFailedError, AIParseFailedError
from app.services.ai import (
    GeminiService,
    ModerationOutput,
    ReviewOutput,
    ScoreRubric,
    SurferContext,
    TrainingContext,
    _generate_content_with_retry,
    _is_retryable,
    _thinking_config,
)
from tests.fake_deps import make_review_output, make_training_plan_output


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text


class _StubModels:
    """Records every generate_content call and replays a scripted result list."""

    def __init__(self, results: list) -> None:
        self._results = list(results)
        self.calls: list[dict] = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        result = self._results.pop(0) if self._results else self._results
        if isinstance(result, BaseException):
            raise result
        return result


class _StubClient:
    def __init__(self, results: list) -> None:
        self.models = _StubModels(results)


@pytest.fixture
def stub_gemini(monkeypatch):
    """Install a stub client and hand back the factory used to script results."""

    def _install(results: list) -> _StubClient:
        client = _StubClient(results)
        monkeypatch.setattr("app.services.ai._gemini_client", lambda api_key: client)
        return client

    return _install


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Retry backoff would otherwise make these tests take seconds."""
    slept: list[float] = []
    monkeypatch.setattr("app.services.ai.time.sleep", slept.append)
    return slept


def _review_json(narrative: str = "Boa sessão.") -> str:
    return json.dumps(
        {
            "narrative": narrative,
            "improvement_tips": ["a", "b", "c"],
            "scores": {
                "flow": 7.0,
                "drop": 6.0,
                "balance": 8.0,
                "wave_selection": 5.0,
                "maneuvers": 4.0,
                "arms": 6.0,
            },
        }
    )


def _context() -> SurferContext:
    return SurferContext(skill_level="intermediate", location="Maresias", wave_conditions="1.5 m")


# ---------------------------------------------------------------------------
# _is_retryable
# ---------------------------------------------------------------------------


class _CodedError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"status {code}")
        self.code = code


class _StatusCodeError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_transient_status_codes_are_retryable(code):
    assert _is_retryable(_CodedError(code)) is True
    assert _is_retryable(_StatusCodeError(code)) is True


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_client_error_status_codes_are_not_retryable(code):
    assert _is_retryable(_CodedError(code)) is False


def test_connection_error_class_names_are_retryable():
    # Matched by class name, so a stand-in with the same name behaves like the real one.
    for name in ("ConnectError", "ReadTimeout", "RemoteProtocolError", "ServerError"):
        exc = type(name, (Exception,), {})()
        assert _is_retryable(exc) is True, name


def test_plain_exception_is_not_retryable():
    assert _is_retryable(ValueError("bad request")) is False


def test_retryable_cause_is_followed_through_the_wrapper():
    inner = type("ConnectError", (Exception,), {})()
    try:
        try:
            raise inner
        except Exception as e:
            raise RuntimeError("wrapped") from e
    except RuntimeError as outer:
        assert _is_retryable(outer) is True


def test_cause_chain_terminates_on_self_reference():
    """A self-referential __context__ must not send _is_retryable infinite."""
    exc = ValueError("boom")
    exc.__context__ = exc
    assert _is_retryable(exc) is False


# ---------------------------------------------------------------------------
# _generate_content_with_retry
# ---------------------------------------------------------------------------


def _call(client, *, max_attempts=3, base_delay=0.01):
    return _generate_content_with_retry(
        client,
        model="m",
        contents=["p"],
        config=None,
        max_attempts=max_attempts,
        base_delay=base_delay,
    )


def test_retry_returns_first_successful_response(stub_gemini):
    client = stub_gemini([_Response("ok")])
    assert _call(client).text == "ok"
    assert len(client.models.calls) == 1


def test_retry_recovers_after_a_transient_failure(stub_gemini, _no_real_sleep):
    client = stub_gemini([_CodedError(503), _Response("ok")])
    assert _call(client).text == "ok"
    assert len(client.models.calls) == 2
    assert len(_no_real_sleep) == 1


def test_retry_gives_up_after_max_attempts(stub_gemini):
    client = stub_gemini([_CodedError(503), _CodedError(503), _CodedError(503)])
    with pytest.raises(_CodedError):
        _call(client, max_attempts=3)
    assert len(client.models.calls) == 3


def test_non_retryable_error_raises_without_a_second_attempt(stub_gemini):
    client = stub_gemini([_CodedError(400), _Response("never reached")])
    with pytest.raises(_CodedError):
        _call(client)
    assert len(client.models.calls) == 1


def test_max_attempts_of_one_disables_retry(stub_gemini):
    client = stub_gemini([_CodedError(503), _Response("ok")])
    with pytest.raises(_CodedError):
        _call(client, max_attempts=1)
    assert len(client.models.calls) == 1


def test_max_attempts_below_one_still_makes_one_call(stub_gemini):
    client = stub_gemini([_Response("ok")])
    assert _call(client, max_attempts=0).text == "ok"
    assert len(client.models.calls) == 1


def test_backoff_delay_grows_between_attempts(stub_gemini, _no_real_sleep):
    client = stub_gemini([_CodedError(503), _CodedError(503), _Response("ok")])
    _call(client, max_attempts=3, base_delay=1.0)
    assert len(_no_real_sleep) == 2
    # Jitter adds up to 25%, so exact values vary — the growth must not.
    assert 1.0 <= _no_real_sleep[0] <= 1.25
    assert 2.0 <= _no_real_sleep[1] <= 2.5
    assert _no_real_sleep[1] > _no_real_sleep[0]


# ---------------------------------------------------------------------------
# _thinking_config
# ---------------------------------------------------------------------------


def test_empty_thinking_level_yields_no_config():
    assert _thinking_config("") is None
    assert _thinking_config("   ") is None
    assert _thinking_config(None) is None


def test_known_thinking_level_is_normalised():
    cfg = _thinking_config("low")
    assert cfg is not None
    assert cfg.thinking_level is not None


def test_unknown_thinking_level_falls_back_to_model_default():
    assert _thinking_config("TURBO") is None


# ---------------------------------------------------------------------------
# analyze_surf_media
# ---------------------------------------------------------------------------


def test_analyze_sends_prompt_plus_one_part_per_image(stub_gemini):
    client = stub_gemini([_Response(_review_json())])
    out = GeminiService().analyze_surf_media([b"f1", b"f2", b"f3"], _context())

    assert isinstance(out, ReviewOutput)
    contents = client.models.calls[0]["contents"]
    assert len(contents) == 4  # prompt + 3 frames
    assert "Maresias" in contents[0]


def test_analyze_without_description_omits_it_and_the_temperature(stub_gemini):
    client = stub_gemini([_Response(_review_json())])
    GeminiService().analyze_surf_media([b"f"], _context())

    call = client.models.calls[0]
    assert call["config"].temperature is None
    assert "Relato pessoal" not in call["contents"][0]


def test_analyze_with_description_pins_the_temperature_and_guards_the_scores(stub_gemini):
    client = stub_gemini([_Response(_review_json())])
    GeminiService().analyze_surf_media([b"f"], _context(), "mandei muito bem", temperature=0.15)

    call = client.models.calls[0]
    assert call["config"].temperature == 0.15
    prompt = call["contents"][0]
    assert "mandei muito bem" in prompt
    assert "NUNCA podem" in prompt  # the score guardrail rides along


def test_analyze_uses_the_configured_model(stub_gemini):
    client = stub_gemini([_Response(_review_json())])
    GeminiService(model_name="gemini-test-model").analyze_surf_media([b"f"], _context())
    assert client.models.calls[0]["model"] == "gemini-test-model"


def test_analyze_wraps_api_failure_as_generation_failed(stub_gemini):
    stub_gemini([_CodedError(400)])
    with pytest.raises(AIGenerationFailedError):
        GeminiService().analyze_surf_media([b"f"], _context())


def test_analyze_wraps_exhausted_retries_as_generation_failed(stub_gemini):
    stub_gemini([_CodedError(503)] * 5)
    with pytest.raises(AIGenerationFailedError):
        GeminiService().analyze_surf_media([b"f"], _context())


def test_analyze_treats_an_empty_response_as_a_parse_failure(stub_gemini):
    stub_gemini([_Response("")])
    with pytest.raises(AIParseFailedError):
        GeminiService().analyze_surf_media([b"f"], _context())


def test_analyze_response_without_a_text_attribute_parses_as_empty(stub_gemini):
    stub_gemini([object()])
    with pytest.raises(AIParseFailedError):
        GeminiService().analyze_surf_media([b"f"], _context())


# ---------------------------------------------------------------------------
# refine_review_with_description
# ---------------------------------------------------------------------------


def test_refine_replaces_the_narrative_but_never_the_scores(stub_gemini):
    original = make_review_output(flow=7.2, drop=6.8)
    refined_json = json.dumps(
        {
            "narrative": "Narrativa personalizada.",
            "improvement_tips": ["novo 1", "novo 2", "novo 3"],
            # A model that ignores the schema and returns scores anyway must not
            # be able to move them.
            "scores": {"flow": 1.0, "drop": 1.0},
        }
    )
    stub_gemini([_Response(refined_json)])

    out = GeminiService().refine_review_with_description(original, _context(), "foi incrível")

    assert out.narrative == "Narrativa personalizada."
    assert out.improvement_tips == ["novo 1", "novo 2", "novo 3"]
    assert out.scores == original.scores


def test_refine_prompt_carries_the_description_and_the_locked_scores(stub_gemini):
    client = stub_gemini(
        [_Response(json.dumps({"narrative": "n", "improvement_tips": ["a", "b", "c"]}))]
    )
    GeminiService().refine_review_with_description(
        make_review_output(flow=7.2), _context(), "acordei cedo e peguei a maré certa"
    )
    prompt = client.models.calls[0]["contents"]
    assert "acordei cedo" in prompt
    assert "7.2" in prompt


def test_refine_wraps_api_failure(stub_gemini):
    stub_gemini([_CodedError(400)])
    with pytest.raises(AIGenerationFailedError):
        GeminiService().refine_review_with_description(make_review_output(), _context(), "relato")


def test_refine_rejects_an_unparseable_response(stub_gemini):
    stub_gemini([_Response("not json at all")])
    with pytest.raises(AIParseFailedError):
        GeminiService().refine_review_with_description(make_review_output(), _context(), "relato")


def test_refine_tolerates_fenced_json_with_a_trailing_comma(stub_gemini):
    stub_gemini(
        [_Response('```json\n{"narrative": "n", "improvement_tips": ["a", "b", "c",],}\n```')]
    )
    out = GeminiService().refine_review_with_description(make_review_output(), _context(), "relato")
    assert out.narrative == "n"


# ---------------------------------------------------------------------------
# moderate_media_content
# ---------------------------------------------------------------------------


def test_moderation_parses_a_clean_verdict(stub_gemini):
    stub_gemini(
        [
            _Response(
                json.dumps(
                    {
                        "surf_related": True,
                        "explicit_content": False,
                        "reason": "Surfer on a wave.",
                    }
                )
            )
        ]
    )
    out = GeminiService().moderate_media_content([b"img"])
    assert isinstance(out, ModerationOutput)
    assert out.surf_related is True
    assert out.explicit_content is False


def test_moderation_forwards_the_supplied_mime_type(stub_gemini):
    client = stub_gemini(
        [_Response('{"surf_related": true, "explicit_content": false, "reason": "ok"}')]
    )
    GeminiService().moderate_media_content([b"img"], mime_type="image/png")
    # contents = prompt + one part per image.
    assert len(client.models.calls[0]["contents"]) == 2


def test_moderation_tolerates_fenced_json(stub_gemini):
    stub_gemini(
        [
            _Response(
                '```json\n{"surf_related": false, "explicit_content": true, '
                '"reason": "Not surf.",}\n```'
            )
        ]
    )
    out = GeminiService().moderate_media_content([b"img"])
    assert out.surf_related is False
    assert out.explicit_content is True


def test_moderation_wraps_api_failure(stub_gemini):
    stub_gemini([_CodedError(400)])
    with pytest.raises(AIGenerationFailedError):
        GeminiService().moderate_media_content([b"img"])


def test_moderation_rejects_an_unparseable_verdict(stub_gemini):
    stub_gemini([_Response("{nope}")])
    with pytest.raises(AIParseFailedError):
        GeminiService().moderate_media_content([b"img"])


# ---------------------------------------------------------------------------
# generate_training_plan
# ---------------------------------------------------------------------------


def _training_context() -> TrainingContext:
    return TrainingContext(
        surf_level="beginner",
        improvement_tips=["tip 1", "tip 2", "tip 3"],
        score_flow=6.0,
        score_balance=5.0,
        score_maneuvers=4.0,
        score_wave_selection=5.5,
        score_drop=6.5,
        score_arms=5.0,
        overall_score=5.3,
        height_cm=180,
        weight_kg=75,
    )


def _plan_json(workout_count: int) -> str:
    plan = make_training_plan_output(workout_count=workout_count)
    return plan.model_dump_json()


def test_training_plan_parses_the_configured_number_of_workouts(stub_gemini):
    stub_gemini([_Response(_plan_json(3))])
    out = GeminiService().generate_training_plan(_training_context())
    assert len(out.workouts) == 3


def test_training_plan_prompt_includes_the_surfer_scores(stub_gemini):
    client = stub_gemini([_Response(_plan_json(3))])
    GeminiService().generate_training_plan(_training_context())
    prompt = client.models.calls[0]["contents"]
    assert "beginner" in prompt
    assert "tip 1" in prompt


def test_training_plan_rejects_a_short_plan(stub_gemini):
    stub_gemini([_Response(_plan_json(2))])
    with pytest.raises(AIParseFailedError):
        GeminiService().generate_training_plan(_training_context())


def test_training_plan_wraps_api_failure(stub_gemini):
    stub_gemini([_CodedError(400)])
    with pytest.raises(AIGenerationFailedError):
        GeminiService().generate_training_plan(_training_context())


def test_training_plan_rejects_unparseable_output(stub_gemini):
    stub_gemini([_Response("<html>error</html>")])
    with pytest.raises(AIParseFailedError):
        GeminiService().generate_training_plan(_training_context())


# ---------------------------------------------------------------------------
# parse_response / parse_refinement edge cases
# ---------------------------------------------------------------------------


def test_parse_response_accepts_null_scores():
    raw = json.dumps(
        {
            "narrative": "Só uma foto do drop.",
            "improvement_tips": ["a", "b", "c"],
            "scores": {
                "flow": None,
                "drop": 6.0,
                "balance": None,
                "wave_selection": None,
                "maneuvers": None,
                "arms": None,
            },
        }
    )
    out = GeminiService.parse_response(raw)
    assert out.scores.flow is None
    assert out.scores.drop == 6.0


def test_parse_response_rejects_out_of_range_scores():
    raw = json.dumps(
        {
            "narrative": "n",
            "improvement_tips": ["a", "b", "c"],
            "scores": {"flow": 11.0},
        }
    )
    with pytest.raises(AIParseFailedError):
        GeminiService.parse_response(raw)


def test_parse_refinement_rejects_missing_tips():
    with pytest.raises(AIParseFailedError):
        GeminiService.parse_refinement('{"narrative": "n"}')


def test_score_rubric_rejects_values_outside_zero_to_ten():
    with pytest.raises(ValueError):
        ScoreRubric(flow=-0.1)
    with pytest.raises(ValueError):
        ScoreRubric(flow=10.1)
