"""FrameExtractor against real (synthesised) video files.

OpenCV is exercised for real here rather than faked: the failure modes worth
guarding — an unopenable file, a zero-frame container — come from OpenCV itself,
so stubbing it would test nothing.
"""

from __future__ import annotations

import pytest

from app.core.errors import InvalidMediaError
from app.core.frame_extractor import FrameExtractor, _temp_video

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

FPS = 10.0
TOTAL_FRAMES = 30
WIDTH, HEIGHT = 64, 48


@pytest.fixture(scope="module")
def video_path(tmp_path_factory) -> str:
    """A short, valid MP4 — 30 frames at 10 fps, so exactly 3 seconds."""
    path = str(tmp_path_factory.mktemp("video") / "clip.mp4")
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), FPS, (WIDTH, HEIGHT))
    if not writer.isOpened():
        pytest.skip("No MP4 encoder available in this OpenCV build")
    for i in range(TOTAL_FRAMES):
        frame = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
        frame[:, :, i % 3] = 255  # a different channel each frame
        writer.write(frame)
    writer.release()
    return path


@pytest.fixture(scope="module")
def video_bytes(video_path) -> bytes:
    with open(video_path, "rb") as fh:
        return fh.read()


@pytest.fixture
def extractor() -> FrameExtractor:
    return FrameExtractor()


# ---------------------------------------------------------------------------
# extract_path / extract
# ---------------------------------------------------------------------------


def test_extract_path_returns_the_requested_number_of_jpegs(extractor, video_path):
    frames = extractor.extract_path(video_path, frame_count=6)
    assert len(frames) == 6
    for frame in frames:
        assert frame.startswith(b"\xff\xd8")  # JPEG SOI marker
        assert frame.endswith(b"\xff\xd9")  # JPEG EOI marker


def test_extracted_frames_are_sampled_across_the_whole_video(extractor, video_path):
    """Evenly-spaced sampling, not the same frame six times."""
    frames = extractor.extract_path(video_path, frame_count=6)
    assert len(set(frames)) > 1


def test_extract_accepts_bytes_and_matches_the_path_variant(extractor, video_path, video_bytes):
    from_bytes = extractor.extract(video_bytes, frame_count=4)
    from_path = extractor.extract_path(video_path, frame_count=4)
    assert len(from_bytes) == len(from_path) == 4


@pytest.mark.parametrize("count", [1, 2, 5, 12])
def test_frame_count_is_honoured(extractor, video_path, count):
    assert len(extractor.extract_path(video_path, frame_count=count)) == count


@pytest.mark.parametrize("count", [0, -1])
def test_non_positive_frame_count_still_yields_one_frame(extractor, video_path, count):
    """max(1, frame_count) — a bad config must not produce an empty frame list."""
    assert len(extractor.extract_path(video_path, frame_count=count)) == 1


def test_requesting_more_frames_than_the_video_has_does_not_crash(extractor, video_path):
    frames = extractor.extract_path(video_path, frame_count=TOTAL_FRAMES * 2)
    assert len(frames) > 0


def test_extract_rejects_a_file_that_is_not_a_video(extractor, tmp_path):
    junk = tmp_path / "not-a-video.mp4"
    junk.write_bytes(b"this is definitely not an mp4 container")
    with pytest.raises(InvalidMediaError):
        extractor.extract_path(str(junk))


def test_extract_rejects_a_missing_file(extractor, tmp_path):
    with pytest.raises(InvalidMediaError):
        extractor.extract_path(str(tmp_path / "nope.mp4"))


def test_extract_rejects_empty_bytes(extractor):
    with pytest.raises(InvalidMediaError):
        extractor.extract(b"")


# ---------------------------------------------------------------------------
# probe_duration
# ---------------------------------------------------------------------------


def test_probe_duration_matches_frames_over_fps(extractor, video_path):
    assert extractor.probe_duration_path(video_path) == pytest.approx(TOTAL_FRAMES / FPS, abs=0.2)


def test_probe_duration_accepts_bytes(extractor, video_bytes):
    assert extractor.probe_duration(video_bytes) == pytest.approx(TOTAL_FRAMES / FPS, abs=0.2)


def test_probe_duration_rejects_a_non_video(extractor, tmp_path):
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"nope")
    with pytest.raises(InvalidMediaError):
        extractor.probe_duration_path(str(junk))


def test_probe_duration_rejects_a_missing_file(extractor, tmp_path):
    with pytest.raises(InvalidMediaError):
        extractor.probe_duration_path(str(tmp_path / "absent.mp4"))


def test_probe_duration_rejects_a_zero_fps_container(extractor, video_path, monkeypatch):
    """A container OpenCV opens but cannot time must not divide by zero."""

    class _ZeroFpsCapture:
        def __init__(self, path):
            pass

        def isOpened(self):
            return True

        def get(self, prop):
            return 0.0

        def release(self):
            pass

    monkeypatch.setattr(cv2, "VideoCapture", _ZeroFpsCapture)
    with pytest.raises(InvalidMediaError):
        extractor.probe_duration_path(video_path)


def test_extract_rejects_a_container_reporting_no_frames(extractor, video_path, monkeypatch):
    class _EmptyCapture:
        def __init__(self, path):
            pass

        def isOpened(self):
            return True

        def get(self, prop):
            return 0

        def release(self):
            pass

    monkeypatch.setattr(cv2, "VideoCapture", _EmptyCapture)
    with pytest.raises(InvalidMediaError):
        extractor.extract_path(video_path)


def test_extract_rejects_a_video_whose_frames_all_fail_to_decode(
    extractor, video_path, monkeypatch
):
    """Frame count is positive but every read fails — no frames, so no review."""

    class _UnreadableCapture:
        def __init__(self, path):
            pass

        def isOpened(self):
            return True

        def get(self, prop):
            return 30

        def set(self, prop, value):
            return True

        def read(self):
            return False, None

        def release(self):
            pass

    monkeypatch.setattr(cv2, "VideoCapture", _UnreadableCapture)
    with pytest.raises(InvalidMediaError):
        extractor.extract_path(video_path)


# ---------------------------------------------------------------------------
# _temp_video
# ---------------------------------------------------------------------------


def test_temp_video_removes_the_file_afterwards():
    import os

    with _temp_video(b"some bytes") as path:
        assert os.path.exists(path)
        held = path
    assert not os.path.exists(held)


def test_temp_video_cleans_up_even_when_the_body_raises():
    import os

    held = None
    with pytest.raises(RuntimeError):
        with _temp_video(b"bytes") as path:
            held = path
            raise RuntimeError("boom")
    assert held is not None
    assert not os.path.exists(held)


def test_temp_video_tolerates_a_file_already_removed():
    import os

    with _temp_video(b"bytes") as path:
        os.remove(path)  # simulate a consumer that moved the file away
    # Exiting the context must not raise.
