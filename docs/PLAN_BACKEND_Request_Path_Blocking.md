# PLAN — Request-path blocking & unbounded buffering

**Status:** Implemented (B + 3 + 1) — D4 still open
**Owner:** Backend
**Date:** 2026-07-22
**Blocks:** production release — cleared, except D4

> **Implementation notes (2026-07-22).** Built as recommended: D1 option B,
> D2 option 3, D3 option 1. D4 was left untouched — the stream endpoint still
> inherits `RATE_LIMIT_DEFAULT`, pending the frontend measurement §2/D4 calls
> for.
>
> Two deviations from §3 worth recording:
>
> - **Spool target.** Parts land in a plain temp file rather than a
>   `SpooledTemporaryFile`, because both consumers need a path or a real
>   handle: OpenCV opens videos by filename, and the Supabase upload streams
>   from an open reader. A `SpooledTemporaryFile` exposes neither before it
>   rolls over. `FrameExtractor` grew `*_path` variants for this; the
>   bytes-taking ones remain for the worker and review pipeline.
> - **Where the count cap lands.** "Returns 422 *before any part is read*" is
>   not reachable with `file: list[UploadFile] = File(...)`: FastAPI parses the
>   whole multipart body before the route — and therefore before any
>   dependency — runs. The part-count check is the first thing the route does,
>   and memory is instead protected by `BodySizeLimitMiddleware`
>   (`app/core/middleware.py`), which caps the body at
>   `MAX_UPLOAD_FILES × MAX_UPLOAD_SIZE_MB` on the `Content-Length` header and
>   again on the byte stream for chunked bodies. Getting the 422 strictly
>   before the parse would mean hand-rolling multipart parsing off
>   `request.stream()` and losing the OpenAPI schema.

---

## 1. Problem

Two related defects make the API process fall over under modest concurrency.
Both are in the HTTP request path; the worker already gets this right.

### 1.1 Blocking I/O on the event loop

`MediaService.upload` ([app/services/media.py](../app/services/media.py)) is an
`async def` called from an `async def` route, but every expensive call inside it
is **synchronous**:

| Call | Cost | Kind |
|---|---|---|
| `magic.from_buffer` | ms | CPU |
| `frame_extractor.probe_duration` | 100s of ms – seconds | OpenCV decode (CPU) |
| `_moderate()` → `frame_extractor.extract` | seconds | OpenCV decode (CPU) |
| `_moderate()` → `gemini.moderate_media_content` | **seconds** | blocking network |
| `storage.upload` | seconds (up to 100 MB) | blocking network |

None are wrapped in `asyncio.to_thread`. The worker deliberately wraps the same
calls — `app/worker.py:178`, `app/services/ai.py:401-431` — so the gap is in the
API only.

**Impact.** `railway.json` pins `--workers 1` and `numReplicas: 1` (a deliberate
choice in [DEPLOY.md](DEPLOY.md) §7 to keep Supabase session-mode connections
low). That means **one event loop serves all traffic**. A single upload freezes
every concurrent request — including the `/health` endpoint Railway polls — for
the duration of a Gemini moderation round-trip plus a 100 MB upload. With
`restartPolicyType: ON_FAILURE`, sustained uploads can push health checks past
their timeout and trigger restarts mid-upload.

### 1.2 Unbounded request buffering

`upload_media` ([app/api/media.py:91](../app/api/media.py)) reads every part into
memory before validating anything:

```python
files_data = [(await f.read(), f.filename or "upload") for f in file]
service.validate_upload_counts([data for data, _ in files_data])   # after the read
```

- The per-file `MAX_UPLOAD_SIZE_MB` cap is checked later still, inside `upload()`.
- There is no cap on the **number** of parts and no request-body limit.
- `validate_upload_counts` only counts parts whose sniffed MIME is image or
  video — parts of any other type are counted by neither branch, so they pass
  the count check entirely and are still fully buffered.

**Impact.** `POST /sessions/{id}/media/` with 100 × 50 MB parts of arbitrary type
buffers ~5 GB before a single validation runs. This is a trivial OOM on a small
Railway instance, reachable by any authenticated user.

---

## 2. Decisions needed

These are the reasons this is a plan and not a patch.

### D1 — How far to go on non-blocking

| Option | Effort | Result |
|---|---|---|
| **A. Wrap in `asyncio.to_thread`** | small | Event loop stays free. Blocking work moves to the default threadpool (40 threads); memory profile unchanged. |
| **B. A + stream parts to disk** | medium | Also fixes §1.2 memory. Requires reworking `MediaService.upload` to take a path/file object instead of `bytes`. |
| **C. Move moderation to the worker** | large | Upload returns 202 immediately; media starts `pending` and is published after moderation. Best latency and robustness, but it is an **API contract change** — the frontend currently expects 201 + `contentUrl` synchronously, and rejection today is a 422 the user sees inline. |

**Recommendation: B now, C later.** B removes the release blocker without
touching the frontend contract. C is the right end state once the async-review
polling pattern (already built for reviews) can be reused for media.

> Note: option A alone still leaves §1.2 unfixed, and 40 concurrent 100 MB
> uploads in threads is its own OOM. A is not sufficient by itself.

### D2 — Upload size enforcement point

Reading `MAX_UPLOAD_SIZE_MB` *after* buffering is the core bug. Options:

1. **Reject on `Content-Length`** before reading the body — cheap, but the header
   is advisory and absent for chunked encoding.
2. **Stream with a running byte counter**, aborting past the cap — correct, works
   for chunked, needs a small `SpooledTemporaryFile` helper.
3. **Both** — fail fast on the header when present, enforce hard during the stream.

**Recommendation: 3.** Also add an explicit `MAX_UPLOAD_FILES` setting; the
current implicit limits (3 photos min / 10 max / 3 videos) do not constrain
files of other types at all.

### D3 — Media streaming proxy

`storage.download_range` ([app/core/storage.py:58](../app/core/storage.py)) reads
`resp.content` in full, so `GET /media/{id}/content` **without** a `Range` header
pulls an entire video into the API process before responding. Options:

1. **Pass through with `httpx` streaming + `StreamingResponse`** — constant
   memory, preserves Range/206 semantics. Requires managing the client lifetime
   across the response (cannot use `async with` around the request alone).
2. **Force a Range window** — reject range-less requests for videos over N MB and
   make the client seek. Simpler, but changes client behaviour.

**Recommendation: 1.**

### D4 — Rate limiting on the stream endpoint

`GET /media/{id}/content` now inherits `RATE_LIMIT_DEFAULT` (120/minute) from the
change already landed. That is *probably* fine, but HTML5 video seeking can emit
bursts of range requests, and a throttled seek looks like a broken player.

**Open question:** give the stream endpoint its own higher limit (e.g.
`RATE_LIMIT_STREAM = 600/minute`), or exempt it and rely on the media-token TTL?
Needs a real measurement against the frontend player before deciding.

---

## 3. Proposed implementation (assuming B + 3 + 1)

1. **`app/core/upload.py` (new)** — `spool_upload(file: UploadFile, max_bytes: int)`
   streaming an `UploadFile` into a `SpooledTemporaryFile`, raising
   `FileTooLargeError` the moment the running total exceeds `max_bytes`.
2. **`app/api/media.py`** — reject on `Content-Length` when present; enforce
   `MAX_UPLOAD_FILES`; spool each part; pass file handles to the service.
3. **`app/services/media.py`** — accept a file handle rather than `bytes`; wrap
   `probe_duration`, `extract`, `moderate_media_content` and `storage.upload` in
   `asyncio.to_thread`. Sniff MIME from the first 2 KB rather than the whole file.
4. **`app/services/media.py`** — count *all* parts toward a total-file cap, not
   just recognised image/video types.
5. **`app/core/storage.py`** — add `stream_range()` using `httpx.AsyncClient.stream`,
   returning an async iterator; keep `download_range` for callers that genuinely
   need bytes.
6. **`app/api/media.py`** — return `StreamingResponse` from the content endpoint.

### Test plan

- Upload of `MAX_UPLOAD_SIZE_MB + 1` returns 413 **without** the full body ever
  being buffered (assert via a spooled-file size probe).
- Upload of `MAX_UPLOAD_FILES + 1` parts returns 422 before any part is read.
- Non-image/video parts count toward the file cap.
- A concurrent-request test: while a slow upload is in flight (fake storage that
  sleeps), `/health/live` still responds within a threshold. This is the
  regression test for §1.1 and should fail on today's code.
- Streaming endpoint returns identical bytes and `Content-Range` headers as the
  buffered implementation, for both 200 and 206.

### Rollout

No migration, no config change required beyond the new `MAX_UPLOAD_FILES`
setting (default it so existing deploys are unaffected). Ship behind no flag —
the behaviour change is strictly "rejects earlier, uses less memory".

---

## 4. Out of scope

- Moving moderation to the worker (option C above) — separate spec.
- Multi-replica scaling, which would additionally require moving the slowapi
  limiter to Redis storage (`storage_uri`) since it is currently in-process.
