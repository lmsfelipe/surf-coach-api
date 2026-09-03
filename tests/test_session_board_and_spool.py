"""Two small surfaces the other suites leave uncovered: linking a surfboard to a
session (an ownership check that runs before the row is written), and the
SpooledUpload handle's read/cleanup behaviour.
"""

from __future__ import annotations

import os
from datetime import date
from uuid import uuid4

import pytest

from app.core.errors import SurfboardForbiddenError, SurfboardNotFoundError
from app.core.security.jwt import AuthUser
from app.core.upload import SNIFF_BYTES, SpooledUpload
from app.schemas.sessions import SessionCreate
from app.services.sessions import SessionsService
from tests.fake_deps import FakeSessionsRepo, FakeSurfboardRepo

# ---------------------------------------------------------------------------
# Linking a surfboard to a session
# ---------------------------------------------------------------------------


@pytest.fixture
def sessions_repo() -> FakeSessionsRepo:
    return FakeSessionsRepo()


@pytest.fixture
def boards_repo() -> FakeSurfboardRepo:
    return FakeSurfboardRepo()


def _payload(surfboard_id=None) -> SessionCreate:
    return SessionCreate(
        session_date=date(2026, 4, 17),
        location="Maresias",
        wave_size=1.5,
        surfboard_id=surfboard_id,
        notes=None,
    )


async def test_a_session_can_be_created_without_a_board(sessions_repo, boards_repo):
    user = AuthUser(id=uuid4(), email="a@example.com")
    service = SessionsService(sessions_repo, boards_repo)  # type: ignore[arg-type]

    session = await service.create_session(_payload(), user)

    assert session.surfboard_id is None


async def test_a_session_links_the_callers_own_board(sessions_repo, boards_repo):
    user = AuthUser(id=uuid4(), email="a@example.com")
    board = await boards_repo.create(
        profile_id=user.id, board_type="shortboard", board_size=5.9, volume=27.0, label=None
    )
    service = SessionsService(sessions_repo, boards_repo)  # type: ignore[arg-type]

    session = await service.create_session(_payload(board.id), user)

    assert session.surfboard_id == board.id


async def test_an_unknown_board_is_rejected_before_the_session_is_written(
    sessions_repo, boards_repo
):
    user = AuthUser(id=uuid4(), email="a@example.com")
    service = SessionsService(sessions_repo, boards_repo)  # type: ignore[arg-type]

    with pytest.raises(SurfboardNotFoundError):
        await service.create_session(_payload(uuid4()), user)

    assert await sessions_repo.list_for_profile(user.id) == []


async def test_another_users_board_cannot_be_attached(sessions_repo, boards_repo):
    """Otherwise a session could reference equipment the caller does not own."""
    owner = AuthUser(id=uuid4(), email="owner@example.com")
    intruder = AuthUser(id=uuid4(), email="intruder@example.com")
    board = await boards_repo.create(
        profile_id=owner.id, board_type="longboard", board_size=9.0, volume=70.0, label=None
    )
    service = SessionsService(sessions_repo, boards_repo)  # type: ignore[arg-type]

    with pytest.raises(SurfboardForbiddenError):
        await service.create_session(_payload(board.id), intruder)

    assert await sessions_repo.list_for_profile(intruder.id) == []


async def test_the_board_check_is_skipped_when_no_board_repo_is_wired(sessions_repo):
    """The repo is optional; without it the id is stored unvalidated rather than crashing."""
    user = AuthUser(id=uuid4(), email="a@example.com")
    service = SessionsService(sessions_repo, None)  # type: ignore[arg-type]

    session = await service.create_session(_payload(uuid4()), user)

    assert session.surfboard_id is not None


# ---------------------------------------------------------------------------
# SpooledUpload
# ---------------------------------------------------------------------------


@pytest.fixture
def spooled(tmp_path):
    def _make(content: bytes, name: str = "clip.mp4") -> SpooledUpload:
        path = tmp_path / name
        path.write_bytes(content)
        return SpooledUpload(path=str(path), size=len(content), file_name=name)

    return _make


def test_head_reads_only_the_sniff_prefix(spooled):
    upload = spooled(b"A" * (SNIFF_BYTES * 4))
    assert len(upload.head()) == SNIFF_BYTES


def test_head_accepts_a_custom_length(spooled):
    assert spooled(b"0123456789").head(4) == b"0123"


def test_head_of_a_short_file_returns_what_there_is(spooled):
    assert spooled(b"tiny").head() == b"tiny"


def test_read_all_returns_the_whole_part(spooled):
    content = b"x" * 5000
    assert spooled(content).read_all() == content


def test_open_yields_a_readable_handle_the_caller_owns(spooled):
    upload = spooled(b"streamed bytes")
    with upload.open() as fh:
        assert fh.read() == b"streamed bytes"


def test_head_does_not_consume_the_part(spooled):
    """Each accessor opens its own handle, so sniffing cannot starve the upload."""
    upload = spooled(b"0123456789")
    upload.head(4)
    assert upload.read_all() == b"0123456789"


def test_close_removes_the_spool_file(spooled):
    upload = spooled(b"bytes")
    upload.close()
    assert not os.path.exists(upload.path)


def test_close_is_idempotent(spooled):
    """Cleanup runs in a finally block that may execute after an earlier close."""
    upload = spooled(b"bytes")
    upload.close()
    upload.close()  # must not raise


def test_close_survives_an_unremovable_spool(spooled, monkeypatch):
    """A permission error during cleanup is logged, never propagated."""
    upload = spooled(b"bytes")

    def _deny(path):
        raise PermissionError("read-only filesystem")

    monkeypatch.setattr(os, "remove", _deny)
    upload.close()  # must not raise
