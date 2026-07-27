import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager

import cv2
import structlog

from app.core.errors import InvalidMediaError

logger = structlog.get_logger(__name__)


@contextmanager
def _temp_video(video_bytes: bytes) -> Iterator[str]:
    """Materialise in-memory video bytes as a file, since OpenCV opens by path."""
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp.write(video_bytes)
            tmp_path = tmp.name
        yield tmp_path
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                logger.warning("Failed to remove temp file %s", tmp_path, exc_info=True)


class FrameExtractor:
    """Video probing and frame sampling.

    The ``*_path`` variants are the real implementations; callers that already
    have the video on disk (uploads spooled by ``app.core.upload``) should use
    them and skip the copy. The bytes-taking variants exist for the worker and
    review pipeline, which pull objects out of storage as bytes.
    """

    def extract(self, video_bytes: bytes, frame_count: int = 6) -> list[bytes]:
        with _temp_video(video_bytes) as path:
            return self.extract_path(path, frame_count)

    def probe_duration(self, video_bytes: bytes) -> float:
        with _temp_video(video_bytes) as path:
            return self.probe_duration_path(path)

    def extract_path(self, video_path: str, frame_count: int = 6) -> list[bytes]:
        """Sample `frame_count` evenly-spaced frames from a video and return them as JPEGs."""
        cap = None
        try:
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                raise InvalidMediaError("Could not open video file.")

            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if total_frames <= 0:
                raise InvalidMediaError("Video has no readable frames.")

            count = max(1, frame_count)
            indices = [int(i * total_frames / count) for i in range(count)]

            frames: list[bytes] = []
            for idx in indices:
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ok, frame = cap.read()
                if not ok or frame is None:
                    continue
                ok, buf = cv2.imencode(".jpg", frame)
                if ok:
                    frames.append(buf.tobytes())

            if not frames:
                raise InvalidMediaError("No frames could be decoded from video.")

            return frames
        finally:
            if cap is not None:
                cap.release()

    def probe_duration_path(self, video_path: str) -> float:
        """Return video duration in seconds. Raises InvalidMediaError on failure."""
        cap = None
        try:
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                raise InvalidMediaError("Could not open video file.")

            fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
            total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
            if fps <= 0 or total_frames <= 0:
                raise InvalidMediaError("Could not determine video duration.")
            return total_frames / fps
        finally:
            if cap is not None:
                cap.release()
