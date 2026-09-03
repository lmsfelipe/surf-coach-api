"""Contract tests: the in-memory fakes must match the real classes they stand in for.

Nearly every test in this suite injects a fake instead of a real repository, so a
fake that drifts from its real counterpart makes the whole suite pass while
production breaks — a renamed repository method, a new required keyword, a
changed default. These tests compare the two surfaces directly, without a
database, so that drift fails here instead of in production.
"""

from __future__ import annotations

import inspect

import pytest

from app.core.frame_extractor import FrameExtractor
from app.core.storage import StorageClient
from app.core.video_transcoder import VideoTranscoder
from app.repositories.ai import ReviewRepository, TrainingPlanRepository
from app.repositories.auth import AuthRepository
from app.repositories.media import MediaRepository
from app.repositories.sessions import SessionsRepository
from app.repositories.surfboards import SurfboardRepository
from app.services.ai import GeminiService
from tests import fake_deps

# (fake, real) pairs. The fake must implement everything the real class exposes.
REPO_PAIRS = [
    (fake_deps.FakeAuthRepo, AuthRepository),
    (fake_deps.FakeSessionsRepo, SessionsRepository),
    (fake_deps.FakeMediaRepo, MediaRepository),
    (fake_deps.FakeReviewRepo, ReviewRepository),
    (fake_deps.FakeTrainingPlanRepo, TrainingPlanRepository),
    (fake_deps.FakeSurfboardRepo, SurfboardRepository),
]

# Collaborators outside the repository layer that are also faked.
COLLABORATOR_PAIRS = [
    (fake_deps.FakeFrameExtractor, FrameExtractor),
    (fake_deps.FakeVideoTranscoder, VideoTranscoder),
]

ALL_PAIRS = REPO_PAIRS + COLLABORATOR_PAIRS


def _public_methods(cls) -> set[str]:
    return {
        name
        for name, member in inspect.getmembers(cls, callable)
        if not name.startswith("_") and not inspect.isclass(member)
    }


def _params(cls, name: str) -> list[inspect.Parameter]:
    sig = inspect.signature(getattr(cls, name))
    return [p for n, p in sig.parameters.items() if n != "self"]


def _ids(pairs):
    return [real.__name__ for _, real in pairs]


@pytest.mark.parametrize(("fake", "real"), ALL_PAIRS, ids=_ids(ALL_PAIRS))
def test_the_fake_implements_every_public_method(fake, real):
    missing = _public_methods(real) - _public_methods(fake)
    assert not missing, (
        f"{fake.__name__} is missing {sorted(missing)} from {real.__name__}. "
        "Tests injecting this fake would not exercise the new method."
    )


@pytest.mark.parametrize(("fake", "real"), ALL_PAIRS, ids=_ids(ALL_PAIRS))
def test_the_fake_has_no_methods_the_real_class_lacks(fake, real):
    """An extra method usually means the real one was renamed or removed."""
    extra = _public_methods(fake) - _public_methods(real)
    assert not extra, (
        f"{fake.__name__} defines {sorted(extra)}, absent from {real.__name__}. "
        "Either the real method was renamed, or the fake grew a method nothing calls."
    )


@pytest.mark.parametrize(("fake", "real"), ALL_PAIRS, ids=_ids(ALL_PAIRS))
def test_method_parameter_names_line_up(fake, real):
    """Keyword-argument call sites must work against both classes."""
    mismatches = []
    for name in sorted(_public_methods(real) & _public_methods(fake)):
        real_params = _params(real, name)
        fake_params = _params(fake, name)

        # A **kwargs-style fake deliberately accepts anything.
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in fake_params):
            continue

        real_names = [p.name for p in real_params]
        fake_names = [p.name for p in fake_params]
        if real_names != fake_names:
            mismatches.append(f"{name}: real{real_names} != fake{fake_names}")

    assert not mismatches, f"{fake.__name__} vs {real.__name__}: " + "; ".join(mismatches)


@pytest.mark.parametrize(("fake", "real"), ALL_PAIRS, ids=_ids(ALL_PAIRS))
def test_keyword_only_arguments_stay_keyword_only(fake, real):
    """Passing positionally against the real class would be a TypeError."""
    mismatches = []
    for name in sorted(_public_methods(real) & _public_methods(fake)):
        fake_params = _params(fake, name)
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in fake_params):
            continue

        real_kwonly = {p.name for p in _params(real, name) if p.kind is p.KEYWORD_ONLY}
        fake_kwonly = {p.name for p in fake_params if p.kind is p.KEYWORD_ONLY}
        if real_kwonly != fake_kwonly:
            mismatches.append(f"{name}: real{sorted(real_kwonly)} != fake{sorted(fake_kwonly)}")

    assert not mismatches, f"{fake.__name__} vs {real.__name__}: " + "; ".join(mismatches)


@pytest.mark.parametrize(("fake", "real"), ALL_PAIRS, ids=_ids(ALL_PAIRS))
def test_async_methods_stay_async_in_the_fake(fake, real):
    """A sync fake of an async method silently returns a value instead of a coroutine."""
    mismatches = [
        name
        for name in sorted(_public_methods(real) & _public_methods(fake))
        if inspect.iscoroutinefunction(getattr(real, name))
        != inspect.iscoroutinefunction(getattr(fake, name))
    ]
    assert not mismatches, f"{fake.__name__} vs {real.__name__} differ in async-ness: {mismatches}"


# ---------------------------------------------------------------------------
# Classes the fakes stand in for only partially
# ---------------------------------------------------------------------------


def test_the_fake_gemini_covers_every_call_the_services_make():
    """FakeGeminiService is a partial stand-in, so assert on the methods used."""
    used = {
        "analyze_surf_media",
        "refine_review_with_description",
        "moderate_media_content",
        "generate_training_plan",
        "parse_response",
    }
    assert used <= _public_methods(GeminiService)
    assert used <= _public_methods(fake_deps.FakeGeminiService)


@pytest.mark.parametrize(
    "name", ["analyze_surf_media", "refine_review_with_description", "generate_training_plan"]
)
def test_fake_gemini_accepts_the_real_positional_arguments(name):
    real_required = [
        p.name
        for p in _params(GeminiService, name)
        if p.default is inspect.Parameter.empty and p.kind is not p.VAR_KEYWORD
    ]
    fake_names = [p.name for p in _params(fake_deps.FakeGeminiService, name)]
    assert real_required[: len(fake_names)] == fake_names[: len(real_required)]


def test_the_fake_storage_client_covers_every_call_the_services_make():
    used = {"upload", "upload_file", "download", "delete", "download_range", "stream_range"}
    assert used <= _public_methods(StorageClient)
    assert used <= _public_methods(fake_deps.FakeStorageClient)


@pytest.mark.parametrize("name", ["upload", "upload_file", "download", "delete"])
def test_fake_storage_signatures_match_the_real_client(name):
    real = [p.name for p in _params(StorageClient, name)]
    fake = [p.name for p in _params(fake_deps.FakeStorageClient, name)]
    assert real == fake


def test_the_fake_arq_pool_matches_the_enqueue_call_shape():
    """Routes call enqueue_job(name, *args); the fake must accept the same shape."""
    params = _params(fake_deps.FakeArqPool, "enqueue_job")
    assert params[0].name == "name"
    assert any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params)
    assert inspect.iscoroutinefunction(fake_deps.FakeArqPool.enqueue_job)
