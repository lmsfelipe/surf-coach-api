"""VideoTranscoder: ffmpeg argument construction, failure classification, cleanup.

ffmpeg itself is stubbed — the subprocess is the boundary, and CI images do not
all carry an encoder. What matters is that the flags follow the settings, that a
signal death is told apart from a bad file, and that temp files never leak.
"""

from __future__ import annotations

import asyncio
import os
import signal

import pytest

from app.core.config import get_settings
from app.core.errors import InvalidMediaError, MediaProcessingKilledError
from app.core.video_transcoder import VideoTranscoder, get_optimize_gate


class _StubProcess:
    def __init__(self, returncode: int, stderr: bytes) -> None:
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self):
        return b"", self._stderr


@pytest.fixture
def run_ffmpeg(monkeypatch):
    """Stub create_subprocess_exec; return a recorder of the argv it was given.

    ``output`` is what the stubbed ffmpeg "writes" to its output path.
    """
    recorded: dict = {}

    def _install(*, returncode: int = 0, stderr: bytes = b"", output: bytes = b"transcoded"):
        ffmpeg_stderr = stderr  # the inner signature rebinds `stderr` to the PIPE kwarg

        async def _fake_exec(*cmd, stdout=None, stderr=None):
            recorded["cmd"] = list(cmd)
            if returncode == 0:
                with open(cmd[-1], "wb") as fh:  # ffmpeg's output path is the last arg
                    fh.write(output)
            return _StubProcess(returncode, ffmpeg_stderr)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        return recorded

    return _install


def _flag_value(cmd: list[str], flag: str) -> str:
    return cmd[cmd.index(flag) + 1]


async def test_transcode_returns_the_encoded_bytes(run_ffmpeg):
    run_ffmpeg(output=b"smaller-mp4")
    assert await VideoTranscoder().transcode(b"raw-video") == b"smaller-mp4"


async def test_transcode_writes_the_input_where_ffmpeg_reads_it(run_ffmpeg):
    recorded = run_ffmpeg()
    await VideoTranscoder().transcode(b"raw-video-bytes")
    # The -i argument is the spooled source file, and the output is a distinct path.
    assert _flag_value(recorded["cmd"], "-i") != recorded["cmd"][-1]
    assert recorded["cmd"][0] == "ffmpeg"


async def test_encoder_flags_follow_the_settings(run_ffmpeg, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "VIDEO_CRF", 31, raising=False)
    monkeypatch.setattr(settings, "VIDEO_TARGET_HEIGHT", 480, raising=False)
    recorded = run_ffmpeg()

    await VideoTranscoder().transcode(b"raw")

    cmd = recorded["cmd"]
    assert _flag_value(cmd, "-crf") == "31"
    assert "min(480,ih)" in _flag_value(cmd, "-vf")
    assert _flag_value(cmd, "-c:v") == "libx264"
    # faststart keeps the moov atom up front so the browser can begin playback early.
    assert _flag_value(cmd, "-movflags") == "+faststart"


async def test_scale_filter_never_upscales(run_ffmpeg):
    """min(target, ih) means a 480p source stays 480p rather than being blown up."""
    recorded = run_ffmpeg()
    await VideoTranscoder().transcode(b"raw")
    vf = _flag_value(recorded["cmd"], "-vf")
    assert vf.startswith("scale=-2:")
    assert "min(" in vf


async def test_audio_is_re_encoded_when_kept(run_ffmpeg, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "VIDEO_KEEP_AUDIO", True, raising=False)
    monkeypatch.setattr(settings, "VIDEO_AUDIO_BITRATE_KBPS", 64, raising=False)
    recorded = run_ffmpeg()

    await VideoTranscoder().transcode(b"raw")

    assert _flag_value(recorded["cmd"], "-c:a") == "aac"
    assert _flag_value(recorded["cmd"], "-b:a") == "64k"
    assert "-an" not in recorded["cmd"]


async def test_audio_is_stripped_when_disabled(run_ffmpeg, monkeypatch):
    monkeypatch.setattr(get_settings(), "VIDEO_KEEP_AUDIO", False, raising=False)
    recorded = run_ffmpeg()

    await VideoTranscoder().transcode(b"raw")

    assert "-an" in recorded["cmd"]
    assert "-c:a" not in recorded["cmd"]


async def test_nonzero_exit_is_reported_as_invalid_media(run_ffmpeg):
    run_ffmpeg(returncode=1, stderr=b"moov atom not found")
    with pytest.raises(InvalidMediaError) as exc:
        await VideoTranscoder().transcode(b"corrupt")
    assert "moov atom not found" in str(exc.value)


async def test_stderr_is_truncated_in_the_error_message(run_ffmpeg):
    run_ffmpeg(returncode=1, stderr=b"x" * 5000)
    with pytest.raises(InvalidMediaError) as exc:
        await VideoTranscoder().transcode(b"corrupt")
    assert len(str(exc.value)) < 500


async def test_sigkill_is_reported_as_an_infra_failure_not_a_bad_file(run_ffmpeg):
    """-9 is almost always the OOM killer; misclassifying it would blame the video."""
    run_ffmpeg(returncode=-int(signal.SIGKILL), stderr=b"")
    with pytest.raises(MediaProcessingKilledError) as exc:
        await VideoTranscoder().transcode(b"huge")
    assert "SIGKILL" in str(exc.value)
    assert "out of memory" in str(exc.value)


async def test_other_signals_are_named_in_the_error(run_ffmpeg):
    run_ffmpeg(returncode=-int(signal.SIGTERM))
    with pytest.raises(MediaProcessingKilledError) as exc:
        await VideoTranscoder().transcode(b"raw")
    assert "SIGTERM" in str(exc.value)


async def test_an_unrecognised_signal_number_still_produces_a_killed_error(run_ffmpeg):
    run_ffmpeg(returncode=-99)
    with pytest.raises(MediaProcessingKilledError) as exc:
        await VideoTranscoder().transcode(b"raw")
    assert "signal 99" in str(exc.value)


def test_killed_error_is_catchable_as_invalid_media_but_carries_its_own_code():
    """It subclasses InvalidMediaError so existing handlers still catch it, while
    the distinct code keeps the infra cause (OOM) visible in logs and responses."""
    assert issubclass(MediaProcessingKilledError, InvalidMediaError)
    assert MediaProcessingKilledError.code != InvalidMediaError.code


async def test_temp_files_are_removed_after_a_successful_transcode(run_ffmpeg):
    recorded = run_ffmpeg()
    await VideoTranscoder().transcode(b"raw")
    in_path = _flag_value(recorded["cmd"], "-i")
    assert not os.path.exists(in_path)
    assert not os.path.exists(recorded["cmd"][-1])


async def test_temp_files_are_removed_after_a_failed_transcode(run_ffmpeg):
    recorded = run_ffmpeg(returncode=1, stderr=b"bad")
    with pytest.raises(InvalidMediaError):
        await VideoTranscoder().transcode(b"raw")
    assert not os.path.exists(_flag_value(recorded["cmd"], "-i"))


async def test_temp_files_are_removed_when_the_subprocess_cannot_start(monkeypatch):
    created: list[str] = []
    real_exec = asyncio.create_subprocess_exec

    async def _boom(*cmd, stdout=None, stderr=None):
        created.append(cmd[cmd.index("-i") + 1])
        raise FileNotFoundError("ffmpeg not installed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
    with pytest.raises(FileNotFoundError):
        await VideoTranscoder().transcode(b"raw")

    assert created and not os.path.exists(created[0])
    assert real_exec is not None


# ---------------------------------------------------------------------------
# Concurrency gate
# ---------------------------------------------------------------------------


def test_optimize_gate_is_a_process_wide_singleton():
    assert get_optimize_gate() is get_optimize_gate()


def test_optimize_gate_is_sized_from_the_setting(monkeypatch):
    import app.core.video_transcoder as vt

    monkeypatch.setattr(vt, "_optimize_gate", None)
    monkeypatch.setattr(get_settings(), "VIDEO_OPTIMIZE_CONCURRENCY", 3, raising=False)
    assert get_optimize_gate()._value == 3


async def test_optimize_gate_serialises_transcodes_at_concurrency_one(monkeypatch):
    """One slot means the second caller waits — the whole point of the gate."""
    import app.core.video_transcoder as vt

    monkeypatch.setattr(vt, "_optimize_gate", None)
    monkeypatch.setattr(get_settings(), "VIDEO_OPTIMIZE_CONCURRENCY", 1, raising=False)

    gate = get_optimize_gate()
    order: list[str] = []

    async def _work(name: str) -> None:
        async with gate:
            order.append(f"{name}-start")
            await asyncio.sleep(0.01)
            order.append(f"{name}-end")

    await asyncio.gather(_work("a"), _work("b"))

    # No interleaving: each transcode finishes before the next begins.
    assert order == ["a-start", "a-end", "b-start", "b-end"]
