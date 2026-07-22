import asyncio
import os
import tempfile

import structlog

from app.core.config import get_settings
from app.core.errors import InvalidMediaError

logger = structlog.get_logger(__name__)


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
                raise InvalidMediaError(
                    f"ffmpeg exited {proc.returncode}: {stderr.decode()[:300]}"
                )

            with open(out_path, "rb") as f:
                return f.read()
        finally:
            for p in (in_path, out_path):
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        logger.warning("Failed to remove temp file %s", p, exc_info=True)
