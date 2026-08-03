# SPEC — Media Upload Optimization (Concurrent Batch Store + Bulk Persist)

**Status:** Proposed
**Owner:** Backend
**Date:** 2026-07-30
**Related specs:** `PLAN_BACKEND_Request_Path_Blocking.md`, `SPEC_BACKEND_Media_Proxy.md`, `SPEC_BACKEND_Video_Optimization.md`

---

## 1. Problem

`POST /api/v1/sessions/{id}/media/` accepts a batch of up to `MAX_UPLOAD_FILES`
parts (default 13; the product shape today is **one video, or a sequence of up
to 10 photos**). The route spools every part to disk, then processes them in a
**sequential `for` loop** (`app/api/media.py:107-109`), calling
`MediaService.upload` once per file.

`PLAN_BACKEND_Request_Path_Blocking.md` already fixed the *blocking* problem —
every expensive call inside `upload()` (`magic`, the OpenCV duration probe, the
Supabase PUT) now runs on a worker thread via `asyncio.to_thread`, so a single
upload no longer freezes the event loop. But the calls are still **awaited one
file at a time**, so for a batch the per-file latencies **add up**:

| Per-file work (moderation **disabled**, the current prod config) | Cost | Runs per file |
|---|---|---|
| `sessions_repo.get(session_id)` — ownership check | one DB round-trip | ✅ redundantly, every file (`app/services/media.py:113`) |
| `magic.from_buffer(head)` | ms | ✅ |
| duration probe (video only; header read, cheap) | ms | ✅ |
| `storage.upload_file` → Supabase PUT | **network round-trip, dominant** | ✅ |
| `media_repo.create` → `add + commit + refresh` | **DB round-trip + commit** | ✅ |

For a **10-photo batch** this is **10 sequential Supabase round-trips + 10
separate commits + 10 identical session fetches**, so wall-clock time is the
*sum* of ten network hops even though the files are small and independent. The
dominant cost — the Supabase PUT — is I/O-bound and already on a thread, so it is
the ideal candidate to overlap.

Moderation is **off** for the controlled release
(`CONTENT_MODERATION_ENABLED=false`, `app/core/config.py:47`); when it is
re-enabled it adds a per-file Gemini round-trip, which this design also
parallelizes for free (§4.6).

### 1.1 Why not the other levers

| Alternative | Why it's out of scope here |
|---|---|
| **Direct-to-storage upload** (client → Supabase via signed URL) | Removes the API's byte-level validation (true content-type via libmagic, duration cap, count limits) and couples the client to the Supabase host. Deliberately kept — see `SPEC_BACKEND_Media_Proxy.md`. |
| **Stream-through pipelining** (overlap receive + send) | Would defer the duration check to the worker, giving up the synchronous validation the proxy exists to provide. Helps only the single-video case; not worth it now. |
| **More uvicorn workers / replicas** | `railway.json` pins `--workers 1`, `numReplicas: 1` **on purpose** (`DEPLOY.md` §7) to bound Supabase session-mode connections. Out of bounds for this spec. |

## 2. Goals

- Cut wall-clock time for multi-file uploads from **O(N)** sequential network
  hops toward **O(1)** — a 10-photo batch should upload in roughly the time of
  the slowest single photo.
- Collapse **N commits → 1** and **N session fetches → 1**.
- **Keep the media proxy and all synchronous validation** exactly as they are
  (type sniffing, size, duration, count limits).
- **Report per-file storage outcomes** — when a transient storage failure hits
  *some* files of an otherwise-valid batch, the files that stored are saved and
  the failures are returned with their reason (storage-stage partial success,
  §4.5). Validation and moderation stay whole-request, unchanged.
- **Zero impact** on the media proxy read path (`MediaOut` unchanged, streaming
  endpoint untouched) and on the video optimization pipeline (§4.6).
- **No migration, no breaking contract change.** Full success stays `201` +
  `list[MediaOut]`; the new `207` shape is purely **additive**, so backend can
  ship ahead of the frontend (§9). Existing tests and
  `SPEC_FRONTEND_ContentModeration.md` stay valid (§10 S7).

## 3. Non-goals

- Removing or bypassing the proxy; changing the existing `201`/`4xx` upload
  responses (the `207` is additive, §4.5).
- Changing worker/replica counts or the connection-pool strategy.
- Stream-through pipelining of a single large upload.
- Re-enabling or changing content moderation (this spec only makes it faster
  *if* re-enabled).
- Any change to `MediaOut`, the streaming endpoint, or `MediaService.get_media*`.

---

## 4. Design

Split `MediaService.upload` (one file, do-everything) into a **batch** method
with three phases. The split is deliberate: it keeps **validation and moderation
whole-request** (a bad file rejects the batch, exactly as today) and makes
**only the storage stage** partial — the narrow scope chosen to avoid changing
the existing error contract (see §4.5, §10).

- **Phase 0 — validate & moderate (fail-fast, whole-request).** Per file: size
  check, libmagic sniff, duration probe/cap, and moderation (if enabled). Any
  failure raises immediately and the **whole request** returns the existing 4xx
  error envelope, **before a single object is stored**. No behavior change here.
- **Phase A — store (concurrent, no DB session access).** Only reached when every
  file validated. Per file: the Supabase PUT, thread-offloaded and run under
  `asyncio.gather` bounded by a semaphore. The only expected failure is
  `STORAGE_UPLOAD_FAILED`; that file becomes a per-file `failed` entry while its
  siblings still succeed. Produces a plain metadata record per stored file —
  **no ORM, no session touched.**
- **Phase B — persist (serial, one commit).** Build `Media` rows for the files
  that stored cleanly and insert them in a single statement.

The session ownership check is hoisted **above** all three phases and done once.

### 4.1 The correctness constraint that dictates the split

A SQLAlchemy `AsyncSession` is **not safe for concurrent use** — multiple
coroutines issuing statements on the same session at once corrupt its state.
`get_media_service` builds one session per request and shares it across both
repos (`app/api/media.py:30-41`). Therefore **no `gather`-ed coroutine may touch
the DB.** This is the reason Phase A returns inert metadata and all persistence
is quarantined into the serial Phase B — not a stylistic choice.

### 4.2 New service method — `upload_batch`

```python
# app/services/media.py
async def upload_batch(
    self,
    session_id: UUID,
    uploads: list[SpooledUpload],
    user: AuthUser,
) -> BatchUploadResult:
    # (1) Authorize once — same session for the whole batch.
    session = await self.sessions_repo.get(session_id)
    if session is None:
        raise NotFoundError("Session not found.")
    if session.profile_id != user.id:
        raise ForbiddenError()

    # (2) Phase 0: validate + moderate EVERY file first. Any failure raises here
    #     and the whole request returns the existing 4xx — nothing is stored yet.
    validated = [await self._validate(session_id, u, user) for u in uploads]

    # (3) Phase A: store bytes concurrently. Only STORAGE_UPLOAD_FAILED is
    #     expected; return_exceptions keeps one file's failure from cancelling
    #     the siblings still uploading.
    sem = asyncio.Semaphore(self.settings.UPLOAD_CONCURRENCY)

    async def _store(v: _ValidatedMedia) -> _PreparedMedia:
        async with sem:
            return await self._store_object(v)

    results = await asyncio.gather(
        *(_store(v) for v in validated), return_exceptions=True
    )

    # (4) Partition. gather preserves order, so results zip back to their parts.
    prepared: list[_PreparedMedia] = []
    failed: list[FailedUpload] = []
    for v, result in zip(validated, results):
        if isinstance(result, _PreparedMedia):
            prepared.append(result)
        elif isinstance(result, StorageUploadFailedError):
            failed.append(FailedUpload.from_error(v.file_name, result))
            await self._cleanup_partial(v)  # best-effort delete of a half-write
        else:
            raise result  # not an expected storage failure: surface it

    # (5) All files failed to store → whole-request 502, unchanged contract.
    if uploads and not prepared:
        raise StorageUploadFailedError()

    # (6) Phase B: persist the files that stored cleanly — one bulk insert, one
    #     commit. `prepared` keeps upload order, so response order is stable.
    stored = await self.media_repo.create_many(session_id=session_id, items=prepared)
    return BatchUploadResult(succeeded=stored, failed=failed)
```

Phase 0 validation/moderation failures raise straight out (existing 4xx envelope,
unchanged). In Phase A a file either stores cleanly (→ a row in Phase B) or fails
with `StorageUploadFailedError` (→ a `failed` entry + best-effort cleanup). If
**every** file fails to store, the request returns a whole-request 502 — the same
error a single-file storage failure gives today. Any other (unexpected) exception
is re-raised: partial success is only for the storage stage, never a mask for
bugs.

`_validate` is Phase 0 for one file (the size/mime/duration/moderation portion of
today's `upload()`), returning a `_ValidatedMedia` (the spooled handle + resolved
`media_type`, `detected_mime`, `storage_key`, `duration`). `_store_object` is the
Supabase PUT. Neither touches the DB session. `upload()` becomes a thin wrapper
over `upload_batch(session_id, [upload], user)` so existing single-file callers
keep working. Phase A returns:

```python
@dataclass
class _PreparedMedia:
    media_type: str          # "image" | "video"
    storage_url: str         # returned by storage.upload_file
    file_name: str
    file_size_bytes: int
    duration_seconds: Decimal | None
```

### 4.3 Bounded concurrency

`UPLOAD_CONCURRENCY` (new setting, default **4**) caps how many files are in
flight. Rationale:

- The parallelized cost is the Supabase PUT, a network wait — a handful in flight
  saturates the link without piling threads onto the single worker. `to_thread`
  draws from the default executor (`min(32, cpu+4)` threads; ~6 on the small
  instance), so 4 concurrent PUTs leave headroom for health checks and other
  requests.
- The single-video case is `N=1` — the semaphore is a no-op there.
- **`UPLOAD_CONCURRENCY=1` reproduces today's sequential behavior** (still with
  the single-fetch + bulk-insert wins), giving a config-only rollback lever.

### 4.4 New repository method — `create_many`

Replaces N × (`add` + `commit` + `refresh`) with a **single INSERT … RETURNING**
so all rows are persisted and their server-generated `id` + `created_at` come
back in one round-trip:

```python
# app/repositories/media.py
async def create_many(
    self, *, session_id: UUID, items: list[_PreparedMedia]
) -> list[Media]:
    if not items:
        return []
    result = await self.db.execute(
        # sort_by_parameter_order=True is REQUIRED: without it, RETURNING with
        # executemany does not guarantee returned-row order matches input order,
        # which would scramble `succeeded[]` vs upload order. See §10 (S1).
        insert(Media).returning(Media, sort_by_parameter_order=True),
        [
            {
                "session_id": session_id,
                "media_type": it.media_type,
                "storage_url": it.storage_url,
                "file_name": it.file_name,
                "file_size_bytes": it.file_size_bytes,
                "duration_seconds": it.duration_seconds,
            }
            for it in items
        ],
    )
    await self.db.commit()
    return list(result.scalars().all())
```

`Media.id` (server default `gen_random_uuid()`) and `Media.created_at` (server
default `now()`) are populated by `RETURNING`, so the route can mint media tokens
and build `MediaOut` without extra refreshes. `optimized_at` is **not** written,
so it stays `NULL` — see §4.6. The `sort_by_parameter_order` flag (SQLAlchemy
2.0.10+; we require `sqlalchemy>=2.0`) is what makes the returned order
deterministic — see §10 (S1) for the alternative of assigning `id` client-side.

### 4.5 Failure semantics — storage-stage partial success

**Decision (§10, confirmed):** partial success is scoped to the **storage stage
only**. Everything that can be judged from the bytes up front — type, size,
duration, count caps, moderation — stays a **whole-request** failure exactly as
today. This keeps the existing error contract (and the shipped
`SPEC_FRONTEND_ContentModeration.md`) intact, and makes the `207` envelope purely
**additive**.

Three tiers, by *when* the failure is knowable:

- **Batch-level validation** (count caps: `TOO_MANY_FILES`, `TOO_FEW_PHOTOS`,
  `TOO_MANY_PHOTOS`, `TOO_MANY_VIDEOS`) — describe the *set*; reject the whole
  request 4xx before any storage write. Unchanged.
- **Per-file validation & moderation** (`FILE_TOO_LARGE`, `INVALID_MEDIA_TYPE`,
  `VIDEO_TOO_LONG`, `EXPLICIT_CONTENT`, `MEDIA_NOT_SURF_RELATED`) — run in
  **Phase 0** across all files; the first failure rejects the **whole request**
  4xx, before any storage write. **Unchanged from today** — a bad file still
  fails the batch, so the client never has to reconcile a validation failure with
  a partial save.
- **Storage failure** (`STORAGE_UPLOAD_FAILED`) — the one failure that can hit
  *some* files of an otherwise-valid batch (a transient Supabase/network blip).
  Only this becomes a per-file `failed` entry; the files that stored still
  succeed.

#### Response shape (additive — the `201`/`4xx` responses are unchanged)

| Outcome | Status | Body |
|---|---|---|
| All files stored | `201 Created` | `list[MediaOut]` — **byte-for-byte as today** |
| Some stored, some `STORAGE_UPLOAD_FAILED` | `207 Multi-Status` | `BatchUploadResult` (**new**) |
| Validation / moderation failure | `4xx` | existing `{ error: {code,message,details} }` — unchanged |
| Every file failed to store | `502` | existing `{ error: { code: STORAGE_UPLOAD_FAILED } }` — unchanged |

Only the **`207`** case introduces a new body; a client that only ever sees full
success or a validation error behaves exactly as before. The `207` envelope:

```jsonc
// BatchUploadResult — 207 Multi-Status only
{
  "succeeded": [ /* MediaOut, in upload order */
    { "id": "…", "sessionId": "…", "mediaType": "image",
      "contentUrl": "/api/v1/media/…/content?token=…", "fileName": "wave1.jpg",
      "fileSizeBytes": 812345, "durationSeconds": null, "createdAt": "…" }
  ],
  "failed": [
    { "fileName": "wave7.jpg", "code": "STORAGE_UPLOAD_FAILED",
      "message": "Media upload failed.", "details": null }
  ]
}
```

FastAPI keeps `response_model=list[MediaOut]` for `201` and declares the `207`
shape via `responses={207: {"model": BatchUploadResult}}`; the route sets the
status code from the result (§4.7). `failed[].code`/`message`/`details` come
straight off the domain `AppError`.

#### The min-photos wrinkle (accepted)

`validate_upload_counts` enforces `MIN_PHOTOS = 3` on the **submitted** set. If
three photos pass Phase 0 but one then fails its PUT, the session ends up with two
saved photos — below the floor. Storage-stage partial success **accepts** this:
the floor is a submission-time guard, not a post-store guarantee, and the client
restores the set by retrying the one `failed` file (§11). Re-checking the floor
against *succeeded* count would force all-or-nothing on a transient network blip.

### 4.6 Invariants that keep the proxy and video-opt pipelines intact

Verified against `SPEC_BACKEND_Media_Proxy.md` and
`SPEC_BACKEND_Video_Optimization.md`. This change must hold all of:

| Invariant | Why |
|---|---|
| Storage key stays `{user_id}/{session_id}/{media_id}.{ext}` (`app/services/media.py:145-147`) | The video optimizer derives the key from `storage_url` by finding the `{user}/{session}/` marker. |
| `MediaService._extract_storage_key` kept as-is | The optimize worker reuses it verbatim (`SPEC_BACKEND_Video_Optimization.md` §4.4). |
| `optimized_at` left `NULL` on insert | It is the optimizer's work-queue anchor; setting it would hide the video from the sweeper. |
| `media_type`, `created_at`, `duration_seconds`, `file_size_bytes`, upload content-type unchanged | The sweeper filters on `media_type='video'` + `created_at`; playback is content-type driven. |
| `MediaOut` and the token flow unchanged | The proxy read path is untouched. |

Two reinforcing facts: the optimizer never calls `MediaService.upload`, so
restructuring it is invisible to the worker; and the 15-minute
`VIDEO_OPTIMIZE_GRACE_SEC` window means the sweeper won't look at a video until
long after upload, so there is no timing race with the now-concurrent path. The
change is also directionally consistent with the video spec's §4.10 ("heavy work
belongs in the worker, keep the request path light").

### 4.7 Route change

`upload_media` (`app/api/media.py:86-113`) keeps spooling and the two count
validations (`validate_file_count`, `validate_upload_counts`) exactly as they are
— these run before Phase 0 and need all parts. The per-file loop is replaced by a
single `service.upload_batch(...)` call. Spooling stays sequential (it reads one
multipart body stream); only the **store** leg is parallelized.

Because success (`201`) keeps its `list[MediaOut]` shape and only the `207` case
carries the new envelope, the route uses a **status-dependent response**:

```python
@router.post(
    "/sessions/{session_id}/media/",
    response_model=list[MediaOut],            # the 201 shape — unchanged
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
    responses={207: {"model": BatchUploadResult}},   # the new, additive shape
)
async def upload_media(..., response: Response) -> Any:
    result = await service.upload_batch(...)
    succeeded = [_media_to_out(m, user.id) for m in result.succeeded]
    if not result.failed:
        return succeeded                       # 201, list[MediaOut] — as today
    response.status_code = status.HTTP_207_MULTI_STATUS
    return BatchUploadResult(succeeded=succeeded, failed=result.failed)
```

New Pydantic schemas in `app/schemas/media.py`: `FailedUpload`
(`fileName`, `code`, `message`, `details`) and `BatchUploadResult`
(`succeeded: list[MediaOut]`, `failed: list[FailedUpload]`), camelCase aliases as
elsewhere. `MediaOut`, the `GET` list/detail responses, and the streaming
endpoint are **untouched**. The only frontend-visible addition is the `207`
branch (§11); a client that never triggers a partial storage failure sees no
change.

## 5. Config summary

| Setting | Default | Purpose |
|---|---|---|
| `UPLOAD_CONCURRENCY` | `4` | Max files stored in flight per batch. `1` = sequential (rollback lever). |

Added to `Settings` (`app/core/config.py`); ignored in dev unless set.

## 6. Testing

Follows the in-memory-fakes convention (`tests/fake_deps.py`). `FakeMediaRepo`
gains `create_many`; `FakeStorageClient` already records uploads/deletes.

**Existing tests stay green** — the `201` success shape and every `4xx`
validation/moderation response are unchanged (that is the whole point of the
storage-only scope). New tests cover only the added behavior:

- **Batch persist:** N photos → `create_many` called once, returns N rows in
  upload order; `FakeSessionsRepo.get` called **once** for the batch.
- **Concurrency bound:** with `UPLOAD_CONCURRENCY=2`, no more than 2 `_store_object`
  calls run simultaneously (use `FakeStorageClient(upload_delay_sec=…)` + an
  in-flight counter).
- **Order preserved:** `succeeded` order == input order despite staggered store
  latency (guards the `sort_by_parameter_order` requirement, S1).
- **Storage partial success:** one file's PUT raises `StorageUploadFailedError` →
  the other N-1 are persisted (`create_many` gets N-1 items), `failed` has one
  entry with that file's name + `STORAGE_UPLOAD_FAILED`, its half-written object
  is deleted (`FakeStorageClient.deleted`), status `207`.
- **All stored / all failed:** all succeed → `201` + `list[MediaOut]` (existing
  test shape); all fail to store → `502` error envelope (existing shape).
- **Validation/moderation still whole-request:** a wrong-type part → `422`
  error envelope, **no** partial save (existing tests assert this and must keep
  passing); exceeding `MAX_PHOTOS` → `422`, before any storage write.
- **Unexpected error not swallowed:** a non-`StorageUploadFailedError` from a
  store propagates and fails the request (guards against masking bugs).
- **Single video unchanged:** `N=1` success path returns the same `201` +
  `[MediaOut]` as today.
- **Invariants:** stored key format and `optimized_at IS NULL` asserted on
  created rows (guards the video-opt contract, §4.6).
- **`UPLOAD_CONCURRENCY=1`:** behaves sequentially, still one commit / one fetch.

## 7. Acceptance criteria

- [ ] A 10-photo batch issues **one** session fetch and **one** commit
      (down from 10 + 10).
- [ ] The 10 Supabase PUTs run concurrently (bounded by `UPLOAD_CONCURRENCY`),
      not serially — wall time ≈ slowest single PUT, not the sum.
- [ ] All-success is `201` + `list[MediaOut]`, byte-for-byte as today; order
      matches upload order.
- [ ] A **storage** failure on some files → `207` with those files in `failed[]`
      (`fileName` + `STORAGE_UPLOAD_FAILED`), the rest saved, half-writes cleaned
      up. Every file failing storage → `502` error envelope.
- [ ] Validation/moderation and count violations still reject the **whole
      request** with the existing 4xx error envelope — never a `207`.
- [ ] Created video rows have `optimized_at = NULL` and the storage key format
      unchanged; a subsequent optimize sweep still picks them up.
- [ ] **All existing media/upload/moderation tests pass unchanged.**
- [ ] `UPLOAD_CONCURRENCY=1` yields sequential stores (rollback verified).

## 8. Open questions / decisions

1. **Partial-success scope (§4.5) — decided: storage stage only.** Validation,
   moderation, and count caps stay whole-request 4xx (existing contract and
   `SPEC_FRONTEND_ContentModeration.md` untouched). Only `STORAGE_UPLOAD_FAILED`
   on a subset of an already-valid batch yields a `207` + `BatchUploadResult`.
   The min-photos floor is a submission-time guard, not a post-store guarantee.
2. **`UPLOAD_CONCURRENCY` default.** 4 is a safe start for the single small
   worker; tune against real Supabase PUT latency and the thread-pool size once
   measured.
3. **Moderation re-enablement.** When `CONTENT_MODERATION_ENABLED` returns, its
   per-file Gemini round-trip runs in Phase 0. It stays whole-request (a rejected
   file fails the batch, per this decision), but the Phase 0 checks may be run
   concurrently to parallelize the Gemini latency; at that point also reuse a
   single `genai.Client` instead of constructing one per call
   (`app/services/ai.py:317`). Follow-up, out of scope here.

## 9. Rollout

The `207` branch (§4.7) is additive, so backend can ship **ahead of** the
frontend without breaking the existing contract:

1. **Backend first (safe).** Ship `upload_batch`, `create_many`,
   `UPLOAD_CONCURRENCY`, and the `207` response. Existing clients only ever see
   `201`/`4xx` (unchanged) until a real partial storage failure occurs — and even
   then a client that ignores `207` degrades to "treated as an error," no worse
   than today. No migration.
2. **Frontend follow-up.** Add the `207` handling from §11 (surface `failed`,
   offer retry). Can land in a later release; not a blocking co-deploy like the
   `storageUrl → contentUrl` change was.
3. Verify on staging: 10-photo batch timing (expect ~single-PUT wall time), the
   one-commit/one-fetch assertions in logs, a forced mid-batch storage failure
   returning `207` with the right `failed` entry and no orphaned object, an
   all-fail-storage batch returning `502`, and a wrong-type part still returning
   the `422` error envelope.
4. If store concurrency regresses anything, set `UPLOAD_CONCURRENCY=1` to fall
   back to sequential stores without a redeploy (keeps the single-fetch +
   bulk-insert + partial-success wins).

---

## 10. Side effects & compatibility

A deliberate audit of what this change touches, and how each risk is contained.

| # | Side effect | Severity | Containment |
|---|---|---|---|
| **S1** | **Bulk-insert row order.** `insert().returning()` with executemany does **not** guarantee returned rows match input order — `succeeded[]` could scramble vs upload order. | Correctness | Pass `sort_by_parameter_order=True` (SQLAlchemy 2.0.10+; `insert(Media).returning(Media, sort_by_parameter_order=True)`). **Alternative:** assign `id = uuid4()` in `_store_object` and pass it into `create_many` — deterministic order **and** it fixes the pre-existing quirk where the storage-key UUID (`app/services/media.py:145`) differs from the row's `gen_random_uuid()` id. Covered by the order-preservation test (§6). |
| **S2** | **Fakes/tests.** `FakeMediaRepo` has only `create`; the service now calls `create_many`. | Test-only | Add `create_many` to `FakeMediaRepo` (mirror the real signature, append to `_store`, return in input order). `FakeStorageClient` already records `uploaded`/`deleted` and supports `upload_delay_sec` for the concurrency test. |
| **S3** | **Concurrency vs shared clients.** Phase A runs up to `UPLOAD_CONCURRENCY` stores at once, all sharing one `StorageClient` (one `supabase` client → one `httpx.Client`) and the default thread-pool. | Low | `httpx.Client` is thread-safe for concurrent requests; the semaphore bounds in-flight work to a handful, well under the `min(32, cpu+4)` executor. The **DB** session is *not* shared into Phase A (§4.1), which is the only client that isn't concurrency-safe. |
| **S4** | **Memory.** The moderation image path reads whole files into memory; with concurrency that is up to `UPLOAD_CONCURRENCY` files at once instead of one. | Low | Bounded by the semaphore (4 × ≤`MAX_UPLOAD_SIZE_MB`). Moderation is off today; revisit the cap if it returns with large images. Video frame-extraction is unaffected (single video, `N=1`). |
| **S5** | **`upload()` callers.** The single-file method is refactored. | None | Grep confirms the only caller is the route (`app/api/media.py:108`); `upload()` is kept as a thin wrapper over `upload_batch`. The worker/optimizer never call it. |
| **S6** | **Video-optimization pipeline.** Depends on `media_type`, `optimized_at IS NULL`, `created_at`, `storage_url`/key, and `_extract_storage_key`. | None | All preserved by the §4.6 invariants; the 15-min grace window rules out any race. Verified against `SPEC_BACKEND_Video_Optimization.md`. |
| **S7** | **Response contract.** The upload endpoint gains a `207` shape. | Contained | Scoped to storage failures only (§4.5 decision), so `201` stays `list[MediaOut]` and all `4xx` validation/moderation responses are unchanged. **Existing backend tests and `SPEC_FRONTEND_ContentModeration.md` stay valid.** The `207` is additive (§11). |

**Doc drift (cosmetic).** `SPEC_BACKEND_Video_Optimization.md` cites hard line
numbers (`app/services/media.py:126`, `:128`) that shift when `upload()` is
split; and `SPEC_FRONTEND_ContentModeration.md` §2's "always all-or-nothing"
sentence should gain a footnote that a *storage* failure can now be partial
(`207`) — moderation itself is still whole-request. Update both when
implementing.

## 11. Frontend / UI implementation

> **Audience:** Frontend (Surf Coach UI).
> **Stack:** React + TypeScript + TanStack Query + shadcn/ui + react-hook-form
> (same as `SPEC_FRONTEND_ContentModeration.md`).

### 11.1 What changes for the client

**Almost nothing, unless a storage upload partially fails.** The upload endpoint
still returns `201` + `MediaOut[]` on full success and the same `4xx` error
envelope for validation/moderation. The **only** new case is:

- **`207 Multi-Status`** → some files stored, some hit `STORAGE_UPLOAD_FAILED`.
  Body is `{ succeeded: MediaOut[], failed: FailedUpload[] }`.

Everything in `SPEC_FRONTEND_ContentModeration.md` (the `422` moderation copy,
the error map) stays exactly as-is — moderation is still whole-request.

### 11.2 Types

```ts
// src/types/api.ts
export interface FailedUpload {
  fileName: string;
  code: "STORAGE_UPLOAD_FAILED";   // the only per-file code today
  message: string;                 // English; do NOT surface — use pt-BR copy
  details: Record<string, unknown> | null;
}

export interface BatchUploadResult {
  succeeded: Media[];
  failed: FailedUpload[];
}
```

### 11.3 The upload mutation — branch on status

`fetch`/`axios` must **not** treat `207` as an error. Read the status and
normalize both success shapes into one result the form can render:

```ts
// src/hooks/queries/media.ts
type UploadOutcome = { succeeded: Media[]; failed: FailedUpload[] };

async function uploadMedia(sessionId: string, files: File[]): Promise<UploadOutcome> {
  const form = new FormData();
  files.forEach((f) => form.append("file", f));

  const res = await apiFetch(`/api/v1/sessions/${sessionId}/media/`, {
    method: "POST",
    body: form,
    // treat 207 as a resolved response, not a throw:
    validateStatus: (s) => s === 201 || s === 207,
  });

  if (res.status === 201) {
    return { succeeded: res.data as Media[], failed: [] };   // unchanged path
  }
  const body = res.data as BatchUploadResult;                // 207
  return { succeeded: body.succeeded, failed: body.failed };
}

export function useUploadMedia(sessionId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (files: File[]) => uploadMedia(sessionId, files),
    onSuccess: ({ succeeded }) => {
      if (succeeded.length) {
        qc.invalidateQueries({ queryKey: ["media", sessionId] });
      }
    },
  });
}
```

`4xx`/`502` still reject the mutation and flow through the existing
`error`-handling path (moderation, invalid type, all-storage-failed) — no change.

### 11.4 UI behavior on `207`

The mutation resolves (it is a partial *success*), so drive UI off the result,
not `onError`:

```tsx
const { mutate, isPending } = useUploadMedia(sessionId);

mutate(selectedFiles, {
  onSuccess: ({ succeeded, failed }) => {
    if (failed.length === 0) {
      toast.success("Fotos enviadas!");
      return;
    }
    // Partial: keep the succeeded ones, surface the failures, offer retry.
    setFailedFiles(failed);                        // [{ fileName, code }]
    toast.warning(
      `${succeeded.length} enviada(s), ${failed.length} falhou(aram). Toque para tentar de novo.`,
    );
  },
});
```

- **Show which files failed** by `fileName`, with a **Retry** action that
  re-submits **only** the failed files (map `failed[].fileName` back to the
  original `File` objects still held in form state).
- **Do not double-count** — the `succeeded` media are already saved; retry must
  upload the failed subset only, never the whole batch (avoids duplicate
  objects).
- **Min-photos floor (§4.5).** If the successful count drops the session below 3
  photos, the retry is not just nice-to-have — block "continue to review" until
  the user retries or adds files, reusing the existing `NO_MEDIA_FOR_SESSION` /
  min-photo guard copy.

### 11.5 pt-BR copy

One new code to add to the central error map (alongside the moderation codes from
`SPEC_FRONTEND_ContentModeration.md` §3). Use hardcoded pt-BR — never the English
`message`:

| `code` | pt-BR copy (suggested) |
|---|---|
| `STORAGE_UPLOAD_FAILED` (per-file, in `failed[]`) | "Não foi possível enviar {fileName}. Tente novamente." |
| `STORAGE_UPLOAD_FAILED` (whole request, `502`) | "Falha no envio. Verifique sua conexão e tente novamente." |

### 11.6 Testing (MSW)

Mirror the `SPEC_FRONTEND_ContentModeration.md` §handlers approach:

- **Full success:** handler returns `201` + `Media[]` → mutation resolves,
  `failed` empty, no warning (regression guard for the unchanged path).
- **Partial:** handler returns `207` + `{ succeeded:[2], failed:[1] }` → the two
  render in the gallery, the one failure shows with a retry affordance.
- **Retry:** retrying the failed file issues a new upload containing only that
  file; on `201` it joins the gallery and the warning clears.
- **All-storage-failed:** `502` error envelope → existing error path (toast),
  nothing added to the gallery.
- **Moderation `422`:** unchanged — still whole-request, existing copy.
