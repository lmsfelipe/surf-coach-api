import logging
import os
import tempfile

import cv2

from app.core.errors import InvalidMediaError

logger = logging.getLogger(__name__)


class FrameExtractor:
    def extract(self, video_bytes: bytes, frame_count: int = 6) -> list[bytes]:
        """Sample `frame_count` evenly-spaced frames from a video and return them as JPEGs."""
        tmp_path: str | None = None
        cap = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                tmp.write(video_bytes)
                tmp_path = tmp.name

            cap = cv2.VideoCapture(tmp_path)
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
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    logger.warning("Failed to remove temp file %s", tmp_path, exc_info=True)

    def probe_duration(self, video_bytes: bytes) -> float:
        """Return video duration in seconds. Raises InvalidMediaError on failure."""
        tmp_path: str | None = None
        cap = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                tmp.write(video_bytes)
                tmp_path = tmp.name

            cap = cv2.VideoCapture(tmp_path)
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
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
