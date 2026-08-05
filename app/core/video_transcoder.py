import asyncio
import os
import signal
import tempfile

import structlog

from app.core.config import get_settings
from app.core.errors import InvalidMediaError, MediaProcessingKilledError

logger = structlog.get_logger(__name__)

_optimize_gate: asyncio.Semaphore | None = None


def get_optimize_gate() -> asyncio.Semaphore:
    """Process-wide cap on concurrent transcodes.

    ffmpeg holds whole decoded frames in memory; running several at once exhausts
    the container's RAM and the OOM killer sends SIGKILL (surfacing as the classic
    ``ffmpeg exited -9``). Sized by VIDEO_OPTIMIZE_CONCURRENCY (default 1) and kept
    separate from WORKER_MAX_JOBS so it throttles only optimization, never reviews.
    Created lazily so it binds to the worker's running event loop.
    """
    global _optimize_gate
    if _optimize_gate is None:
        _optimize_gate = asyncio.Semaphore(get_settings().VIDEO_OPTIMIZE_CONCURRENCY)
    return _optimize_gate


class VideoTranscoder:
    async def transcode(self, video_bytes: bytes) -> bytes:
        """Re-encode to a web-optimized H.264/AAC MP4. Raises InvalidMediaError."""
        s = get_settings()
        in_path = out_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".src", delete=False) as f:
                f.write(video_bytes)
                in_path = f.name
            out_path = in_path + ".mp4"

            vf = f"scale=-2:'min({s.VIDEO_TARGET_HEIGHT},ih)'"
            audio = (
                ["-c:a", "aac", "-b:a", f"{s.VIDEO_AUDIO_BITRATE_KBPS}k"]
                if s.VIDEO_KEEP_AUDIO
                else ["-an"]
            )
            cmd = [
                "ffmpeg",
                "-y",
                "-nostdin",
                "-i",
                in_path,
                "-vf",
                vf,
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                str(s.VIDEO_CRF),
                "-pix_fmt",
                "yuv420p",
                *audio,
                "-movflags",
                "+faststart",
                out_path,
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                detail = stderr.decode()[:300]
                if proc.returncode < 0:
                    # Negative return code = killed by a signal; SIGKILL (-9) is
                    # almost always the OOM killer, an infra problem, not a bad file.
                    try:
                        sig = signal.Signals(-proc.returncode).name
                    except ValueError:
                        sig = f"signal {-proc.returncode}"
                    raise MediaProcessingKilledError(
                        f"ffmpeg killed by {sig} (likely out of memory): {detail}"
                    )
                raise InvalidMediaError(f"ffmpeg exited {proc.returncode}: {detail}")

            with open(out_path, "rb") as f:
                return f.read()
        finally:
            for p in (in_path, out_path):
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        logger.warning("Failed to remove temp file %s", p, exc_info=True)
