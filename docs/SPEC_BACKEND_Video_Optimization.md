# SPEC — Video Optimization (Self-Hosted ffmpeg Transcode)

**Status:** Proposed
**Owner:** Backend
**Date:** 2026-07-21
**Related specs:** `SPEC_BACKEND_Async_Review_Processing.md`, `SPEC_BACKEND_Stuck_Job_Recovery.md`, `SPEC_BACKEND_Media_Proxy.md`

---

## 1. Problem

Session videos are uploaded and stored **raw** in Supabase Storage
(`MediaService._store_object`, `app/services/media.py`) — up to
`MAX_UPLOAD_SIZE_MB = 100` per file, `MAX_VIDEOS = 3` per session. They are
never re-encoded, so a phone clip (often 60–120 MB, frequently HEVC/`.mov`)
sits in storage at full size for its entire life.

At our target of ≤100 launch users, **storage is the only cost line that grows
monotonically** — compute, Redis, and Gemini are flat or one-time, but every
raw video uploaded keeps costing money every month thereafter. Concretely:

- The AI review is generated from **6 extracted JPEG frames** (~2 MB),
  `FRAME_EXTRACT_COUNT`, `app/services/ai.py:341`. The raw video is **~97% of
  the stored bytes** and contributes nothing to the review after frames are
  taken.
- The only post-review reason to keep the raw video is **user replay**, which
  is a hard product requirement — so we cannot simply delete it.

We can keep replay while cutting ~85–90% of the bytes by transcoding each
video to a compressed, web-optimized MP4 and **replacing the raw object in
place**. This spec covers doing that ourselves with `ffmpeg` in the existing
arq worker (no managed transcoding vendor).

### 1.1 Why this fits the current architecture

The heavy lifting already exists:

- The worker already **downloads the raw bytes and writes them to a temp file**
  for OpenCV frame extraction (`FrameExtractor`, `app/core/frame_extractor.py:18`).
- `StorageClient.upload` already **upserts** (`"upsert": "true"`,
  `app/core/storage.py:38`), so a compressed object can overwrite the raw at the
  same key with no new storage plumbing.
- Playback is served through the media proxy from the object's stored
  content-type (`StorageClient.download_range`, `app/core/storage.py:58`;
  `SPEC_BACKEND_Media_Proxy.md`), so a same-key replacement is transparent to
  the client.
- There is an established **cron-sweeper idiom** (`sweep_stuck_jobs`,
  `app/worker.py:123`) we mirror for the trigger.

## 2. Goals

- Reduce stored bytes per video by **~85–90%** while preserving smooth replay
  (progressive download + HTTP Range seeking).
- **Keep the raw only transiently** — the permanent object is the compressed
  version. Storage per video trends toward the compressed size, not the raw.
- **Self-healing and idempotent** — like the stuck-job sweeper, recovery does
  not depend on any single event firing.
- **Zero client changes** — same media id, same key, same proxy URL; only the
  bytes and content-type change.
- Normalize all videos to **H.264/AAC MP4**, which also fixes HEVC clips that
  don't play in every browser (a replay-UX win, not just a cost win).

## 3. Non-goals

- **Deleting** media or any retention/expiry policy (raw is replaced, not
  removed; replay stays available indefinitely).
- Adaptive bitrate / HLS, per-title encoding, thumbnails, or a managed
  transcoding service (Cloudflare Stream / Mux) — those are the scale-up path,
  out of scope here.
- Transcoding **images** — images are already small and are left untouched.
- Changing the upload path or the review pipeline's output.

---

## 4. Design

A single background task, `optimize_media_task(media_id)`, transcodes one video
and replaces it in storage. A **cron sweep** (§4.5) is the primary trigger and
the catch-all, exactly like `sweep_stuck_jobs`. Compression is intentionally
**decoupled from the review flow** and never blocks it.

### 4.1 Prerequisite — `optimized_at` column on `media`

Add a nullable timestamp to `public.media` marking when a video was optimized.
This is both the idempotency guard and the sweeper's work-queue anchor (mirrors
`processing_started_at` from `SPEC_BACKEND_Stuck_Job_Recovery.md`).

| State | `optimized_at` |
|---|---|
| Freshly uploaded video (or image) | `NULL` |
| Successfully transcoded (or no-gain, see §4.9) | `now()` |

A video is eligible for optimization iff `media_type = 'video'` **and**
`optimized_at IS NULL`.

### 4.2 Core — `VideoTranscoder`

New module `app/core/video_transcoder.py`, mirroring `FrameExtractor`'s
temp-file lifecycle but shelling out to `ffmpeg` asynchronously (non-blocking
in the worker event loop via `asyncio.create_subprocess_exec`):

```python
# app/core/video_transcoder.py
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

            vf = f"scale=-2:'min({s.VIDEO_TARGET_HEIGHT},ih)'"   # cap height, keep aspect, never upscale
            audio = (
                ["-c:a", "aac", "-b:a", f"{s.VIDEO_AUDIO_BITRATE_KBPS}k"]
                if s.VIDEO_KEEP_AUDIO else ["-an"]
            )
            cmd = [
                "ffmpeg", "-y", "-nostdin", "-i", in_path,
                "-vf", vf,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", str(s.VIDEO_CRF),
                "-pix_fmt", "yuv420p",
                *audio,
                "-movflags", "+faststart",
                out_path,
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise InvalidMediaError(f"ffmpeg exited {proc.returncode}: {stderr.decode()[:300]}")

            with open(out_path, "rb") as f:
                return f.read()
        finally:
            for p in (in_path, out_path):
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        logger.warning("Failed to remove temp file %s", p, exc_info=True)
```

**Why `create_subprocess_exec` (not `to_thread`):** it's the event-loop-native
way to run an external process and gives us the return code + stderr for error
reporting without occupying a thread for the full encode.

### 4.3 Storage strategy — overwrite in place

The compressed bytes are upserted to the **same storage key** the raw occupied
(`{user_id}/{session_id}/{media_id}.{ext}`, built in `MediaService._validate`,
`app/services/media.py`), with content-type forced to `video/mp4`:

```python
await asyncio.to_thread(storage.upload, key, compressed, "video/mp4")
```

- **No new key, no delete, no `storage_url` rewrite, no schema change beyond
  `optimized_at`.** The media proxy serves the object by key and streams the
  stored content-type, so replay is unaffected.
- The key's file extension may now be cosmetically stale (e.g. `…/{id}.mov`
  holding MP4 bytes). This is invisible to clients — the proxy URL is
  `/media/{id}/stream` and playback is content-type driven, not
  extension-driven. Accepted.

**Alternative (rejected for now): new `.mp4` key + swap.** Upload to
`…/{media_id}.mp4`, update `media.storage_url`, delete the old key. Cleaner
naming, but adds a row update, a delete, and a media-proxy cache concern for no
functional gain. Revisit only if the stale extension causes a concrete problem.

### 4.4 Task — `optimize_media_task`

```python
# app/worker.py
async def optimize_media_task(ctx: dict, media_id: str) -> None:
    """arq task: transcode one video and replace the raw object in storage."""
    settings = get_settings()
    if not settings.VIDEO_OPTIMIZE_ENABLED:
        return

    db = SessionLocal()
    try:
        media_repo = MediaRepository(db)
        media = await media_repo.get(UUID(media_id))
        if media is None or media.media_type != "video" or media.optimized_at is not None:
            return  # gone, not a video, or already done — idempotent no-op

        storage = get_storage_client()
        key = _key_for_media(media)   # reuse MediaService._extract_storage_key logic
        if not key:
            logger.warning("optimize: could not derive key for media %s", media_id)
            return

        raw = await asyncio.to_thread(storage.download, key)
        compressed = await VideoTranscoder().transcode(raw)

        if len(compressed) >= len(raw):
            # Already small/efficient — don't upload a bigger file; just mark done.
            await media_repo.mark_optimized(media.id, len(raw))
            return

        await asyncio.to_thread(storage.upload, key, compressed, "video/mp4")
        await media_repo.mark_optimized(media.id, len(compressed))
        logger.info(
            "optimize: media %s %d -> %d bytes (-%.0f%%)",
            media_id, len(raw), len(compressed), 100 * (1 - len(compressed) / len(raw)),
        )
    except Exception:
        logger.exception("optimize: media %s failed; raw left intact", media_id)
        # optimized_at stays NULL -> retried on a later sweep. No re-raise:
        # a failed optimize must never surface to a user or fail a review.
    finally:
        await db.close()
```

Note the ordering that makes this safe: **the raw object is overwritten only
after a successful transcode.** Any failure (download, ffmpeg, upload) leaves
the raw untouched and `optimized_at = NULL`, so the next sweep retries it.

### 4.5 Trigger — cron sweep (primary), event enqueue (optional)

**Primary: cron.** A cron job enqueues optimize tasks for eligible videos,
mirroring `sweep_stuck_jobs`:

```python
# app/worker.py
async def sweep_unoptimized_media(ctx: dict) -> None:
    settings = get_settings()
    if not settings.VIDEO_OPTIMIZE_ENABLED:
        return
    db = SessionLocal()
    try:
        repo = MediaRepository(db)
        rows = await repo.list_unoptimized_videos(
            older_than_sec=settings.VIDEO_OPTIMIZE_GRACE_SEC,
            limit=settings.VIDEO_OPTIMIZE_BATCH,
        )
        for m in rows:
            await ctx["redis"].enqueue_job("optimize_media_task", str(m.id))
        if rows:
            logger.info("optimize sweep: enqueued %d video(s)", len(rows))
    finally:
        await db.close()
```

- `ctx["redis"]` is the arq pool available inside every job — no new enqueue
  wiring needed.
- **The grace period (`VIDEO_OPTIMIZE_GRACE_SEC`) is deliberate.** It holds off
  compression until well after upload, so the review pipeline has already
  pulled frames from the **raw** (highest quality for Gemini). Reviews complete
  inside the frontend's 5-minute polling window; a 15-minute grace guarantees
  frames-from-raw in the common case. If a review ever runs *after*
  compression, 720p frames are still perfectly adequate — so this is a soft,
  quality-optimizing constraint, not a correctness one.
- `VIDEO_OPTIMIZE_BATCH` throttles each tick so a backlog (or launch spike)
  drains gradually instead of saturating the worker.

**Optional latency optimization (event enqueue).** If near-immediate
compression is ever wanted, `process_review_task` can additionally enqueue
`optimize_media_task` for the session's videos on success. Not recommended for
v1: it couples compression to the review flow and still needs the cron for
never-reviewed media. The cron alone is simpler and self-healing.

### 4.6 Worker registration

```python
# app/worker.py — WorkerSettings
class WorkerSettings:
    functions = [process_review_task, process_training_plan_task, optimize_media_task]
    cron_jobs = [
        cron(sweep_stuck_jobs, minute=set(range(60)), run_at_startup=True),
        cron(sweep_unoptimized_media, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
             run_at_startup=True),   # every 5 minutes
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL)
    max_jobs = get_settings().WORKER_MAX_JOBS
    job_timeout = get_settings().WORKER_JOB_TIMEOUT_SEC
    retry_jobs = False
```

If a 720p transcode risks approaching `WORKER_JOB_TIMEOUT_SEC` (240 s) on the
small worker, pass a dedicated per-job timeout when enqueuing
(`enqueue_job("optimize_media_task", str(m.id), _job_timeout=...)`); at
`veryfast`/720p a ≤120 s clip encodes in well under a minute, so the global
timeout is expected to be sufficient.

### 4.7 ffmpeg parameters — rationale

| Flag | Value | Why |
|---|---|---|
| `-vf scale=-2:'min(H,ih)'` | H = `VIDEO_TARGET_HEIGHT` (720) | Cap height, preserve aspect for portrait **and** landscape, never upscale |
| `-c:v libx264` | H.264 | Universal browser/mobile playback; fixes HEVC-only clips |
| `-preset veryfast` | — | Best speed/size trade-off on a small shared CPU |
| `-crf 28` | `VIDEO_CRF` | Visually fine for replay; lower = better/bigger |
| `-pix_fmt yuv420p` | — | Required for broad browser compatibility |
| `-c:a aac -b:a 96k` / `-an` | `VIDEO_KEEP_AUDIO` | Low-bitrate audio, or drop it entirely |
| `-movflags +faststart` | — | Moves the moov atom to the front → progressive play + Range seeking (critical for replay via the media proxy) |

Expected outcome: a 60–120 MB 1080p clip → **~8–15 MB at 720p** (or 4–8 MB at
480p if `VIDEO_TARGET_HEIGHT` is lowered).

### 4.8 Repository additions

```python
# app/repositories/media.py
async def mark_optimized(self, media_id: UUID, size_bytes: int) -> None:
    # UPDATE public.media SET optimized_at = now(), file_size_bytes = :size WHERE id = :id

async def list_unoptimized_videos(self, older_than_sec: int, limit: int) -> list[Media]:
    # SELECT * FROM public.media
    # WHERE media_type = 'video' AND optimized_at IS NULL
    #   AND created_at < now() - make_interval(secs => :older_than_sec)
    # ORDER BY created_at LIMIT :limit
```

`file_size_bytes` is refreshed to the compressed size so listings and any
storage accounting reflect reality.

### 4.9 Idempotency & failure handling

- **Already optimized / not a video / deleted** → task no-ops (guard in §4.4).
- **Transcode/upload failure** → raw intact, `optimized_at` stays `NULL`,
  retried next sweep. Never re-raised, so it can't fail a review or reach a user.
- **No-gain videos** (compressed ≥ raw, e.g. very short clips) → keep raw, set
  `optimized_at` so they aren't retried forever.
- **Rare double-encode window:** if `upload` succeeds but `mark_optimized` then
  fails, the next sweep re-encodes an already-720p file (mild extra quality
  loss, wasted work). Bounded and rare with `retry_jobs = False`; accepted over
  the complexity of a two-phase commit.
- **Concurrency:** `max_jobs` and `VIDEO_OPTIMIZE_BATCH` bound how many encodes
  run at once so ffmpeg can't saturate the small worker.

### 4.10 Rejected alternatives

- **Delete raw after review** (no replay): rejected — replay is a hard product
  requirement.
- **Transcode synchronously at upload** (in the API request): rejected —
  encoding is CPU-heavy and seconds-to-minutes long; it would block the web
  service and the user's upload. Belongs in the worker.
- **Managed transcoding (Cloudflare Stream / Mux):** deferred — adds a vendor
  and per-minute cost for adaptive streaming we don't need at 100 users. A
  single faststart MP4 streams fine. This is the documented scale-up path.
- **Transcode before frame extraction:** rejected — frames for Gemini should
  come from the raw (see §4.5 grace period).

---

## 5. Database migration

```python
# alembic — 00XX_add_media_optimized_at.py
def upgrade() -> None:
    op.add_column(
        "media",
        sa.Column("optimized_at", sa.DateTime(timezone=True), nullable=True),
        schema="public",
    )
    # Leave existing videos with optimized_at = NULL so the sweeper compresses
    # the backlog gradually (throttled by VIDEO_OPTIMIZE_BATCH). Mark images as
    # not-applicable is unnecessary — the task/sweeper both filter media_type.


def downgrade() -> None:
    op.drop_column("media", "optimized_at", schema="public")
```

No index needed at current scale: `list_unoptimized_videos` scans a small
`media` table. Add a partial index
(`WHERE media_type = 'video' AND optimized_at IS NULL`) only if volume grows.

## 6. Dependency / Docker change

`ffmpeg` (the CLI binary) is **not** bundled by `opencv-python-headless`, which
only ships decode libraries. Add the package to the `base` stage of the
`Dockerfile` so both `dev` and `prod` inherit it:

```dockerfile
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libmagic1 \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*
```

Adds ~50–70 MB to the image. The stock Debian `ffmpeg` package includes
`libx264` and `aac` encoders and HEVC/`.mov` demuxers — no custom build needed.

## 7. Config summary

All added to `Settings` (`app/core/config.py`), ignored in dev unless set.

| Setting | Default | Purpose |
|---|---|---|
| `VIDEO_OPTIMIZE_ENABLED` | `true` | Master switch (task + sweep both honor it) |
| `VIDEO_TARGET_HEIGHT` | `720` | Max output height; caps long-edge, never upscales |
| `VIDEO_CRF` | `28` | Quality/size knob (lower = better/larger) |
| `VIDEO_KEEP_AUDIO` | `true` | Keep low-bitrate audio vs strip it |
| `VIDEO_AUDIO_BITRATE_KBPS` | `96` | Audio bitrate when kept |
| `VIDEO_OPTIMIZE_GRACE_SEC` | `900` | Delay after upload before compressing (protects frames-from-raw) |
| `VIDEO_OPTIMIZE_BATCH` | `20` | Max videos enqueued per sweep tick |

Sweep cadence (every 5 min) is fixed in `WorkerSettings.cron_jobs`.

## 8. Observability

- Structured log per optimize with `media_id`, `raw_bytes`, `compressed_bytes`,
  and reduction % (§4.4) — lets us confirm the storage win in aggregate.
- Sweep logs the enqueue count per tick.
- Failures log via `logger.exception` and are visible in Sentry
  (`SPEC_BACKEND_Observability.md`); because the task swallows exceptions, add a
  breadcrumb/tag so repeated failures on one media id are noticeable.

## 9. Testing

Follows the in-memory-fakes convention (`tests/fake_deps.py`). Add a
`FakeVideoTranscoder` whose `transcode` returns a fixed small byte string (and a
variant that raises, and one that returns bytes ≥ input for the no-gain path).

- `optimize_media_task` on a video: downloads, transcodes, upserts to the same
  key with `video/mp4`, sets `optimized_at`, updates `file_size_bytes`.
- Idempotency: a media with `optimized_at` set is a no-op; an image is a no-op.
- Failure: transcoder raises → no upload, `optimized_at` stays `NULL`, task does
  not re-raise.
- No-gain: compressed ≥ raw → raw not overwritten, `optimized_at` still set.
- `list_unoptimized_videos` returns only `media_type='video'`,
  `optimized_at IS NULL`, older than the grace window; respects `limit`.
- `sweep_unoptimized_media` enqueues one job per returned row; honors the
  disabled flag.
- `VideoTranscoder` integration test (marked/optional, needs the `ffmpeg`
  binary): a tiny generated clip transcodes to a smaller faststart MP4 and
  preserves orientation.

## 10. Acceptance criteria

- [ ] `optimized_at` column exists on `public.media` (migration up/down).
- [ ] After the grace window, an uploaded video is transcoded and the storage
      object at its key is replaced with a smaller `video/mp4` (raw size not
      preserved).
- [ ] The media id, key, and proxy URL are unchanged; replay still streams and
      seeks (faststart) after optimization.
- [ ] An HEVC/`.mov` upload comes back as playable H.264 MP4.
- [ ] A transcode failure leaves the raw intact and `optimized_at = NULL`, and
      the next sweep retries it — no user-visible error, no failed review.
- [ ] An already-optimized video and any image are no-ops for both task and sweep.
- [ ] Reviews are unaffected: frames are still taken from the raw, and review
      timing/behavior is unchanged.
- [ ] `VIDEO_OPTIMIZE_ENABLED=false` disables both the task and the sweep.
- [ ] `ffmpeg` is present in the prod image and the worker can invoke it.

## 11. Rollout

1. Ship the migration + code with `VIDEO_OPTIMIZE_ENABLED=false`; deploy the
   worker (confirm `ffmpeg -version` runs in the container).
2. Enable on staging; upload landscape, portrait, and HEVC clips; verify replay
   and the size-reduction logs.
3. Enable in prod. The sweeper drains the **existing raw backlog** gradually
   (all pre-feature videos have `optimized_at = NULL`), throttled by
   `VIDEO_OPTIMIZE_BATCH`. Note the one-time egress: each backlog video is
   downloaded to the worker once — expected and bounded at current volume.
4. Watch aggregate reduction % and storage usage; tune `VIDEO_CRF` /
   `VIDEO_TARGET_HEIGHT` if quality or size needs adjustment.

## 12. Open questions

1. **Keep audio?** Surf clips may have useful ambient audio (wind, coaching
   callouts) or none worth 96 kbps. Default `VIDEO_KEEP_AUDIO=true`; revisit if
   audio is never used in replay.
2. **Target height 720 vs 480.** 720p is a safe default for technique replay;
   480p roughly halves storage again. Could be made per-plan later.
3. **Backlog egress.** If the pre-feature backlog is ever large, consider
   compressing it during off-peak or capping `VIDEO_OPTIMIZE_BATCH` lower for
   the first day. At launch scale this is negligible.
4. **`file_size_bytes` semantics.** This spec overwrites it with the compressed
   size. If we ever need the original size for analytics, add a separate
   `original_size_bytes` column instead of overwriting.
