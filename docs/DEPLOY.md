# DEPLOY — Production Runbook (UI + API + Worker)

**Status:** Ready to execute
**Owner:** Backend
**Date:** 2026-07-22
**Target scale:** ≤100 active users at launch
**Related:** `SPEC_BACKEND_Video_Optimization.md`, `SPEC_BACKEND_Async_Review_Processing.md`, `SPEC_BACKEND_Media_Proxy.md`

---

## 1. What gets deployed where

| Component | Repo | Platform | Notes |
|---|---|---|---|
| **UI** (Vite/React SPA) | `surf-coach-ui` | **Cloudflare Pages** | Static `dist/`, free tier, global CDN |
| **API** (FastAPI) | `surf-coach-api` | **Railway** service #1 | Dockerfile `prod` stage; runs migrations |
| **Worker** (arq) | `surf-coach-api` | **Railway** service #2 | Same image, different start command |
| **Postgres** | — | **Supabase** (existing) | Session pooler; no separate managed DB |
| **Redis** | — | **Railway** plugin | arq job queue |
| **Auth + Storage** | — | **Supabase** (existing) | JWT + `surf-media` / `profile-media` buckets |
| **Gemini** | — | Google AI | `gemini-2.0-flash` |
| **Errors** | — | Sentry (optional) | `SENTRY_DSN` empty = disabled |

Expected cost: **~$12–20/mo** at launch, rising to ~$40/mo once Supabase Pro is
needed for video storage.

## 2. Pre-flight — already verified in the repos

- [x] `ffmpeg` installed in the Dockerfile `base` stage (inherited by `prod`).
- [x] Dockerfile installs deps from `uv.lock` (`uv sync --locked`, hash-verified) —
      every build ships the exact versions CI tested, and `prod` gets runtime deps
      only, no test/lint tools. See §16.
- [x] `railway.json` (API) and `railway.worker.json` (worker) committed.
- [x] `ALLOWED_HOSTS` setting exists and is honored in `app/main.py`
      (**fail-open** when unset — an unset var cannot 400 the whole deploy).
- [x] `public/_redirects` committed in the UI repo (SPA deep-link fallback).
- [x] Migrations through `0013_add_media_optimized_at.py` present.
- [x] Both repos pushed to GitHub (`lmsfelipe/surf-coach-api`, `lmsfelipe/surf-coach-ui`).

## 3. Decide before starting

1. **UI branch.** `surf-coach-ui` is on `feat/frontend-implementation`; the API
   is on `main`. Either merge the UI branch to `main` (recommended — keeps
   "production = main" consistent across both repos) or point Cloudflare Pages
   at the feature branch. **The rest of this runbook assumes `main`.**
2. **Region.** Pick the **same region** for Railway and the Supabase project
   (e.g. both `us-east`). Cross-region adds latency to every DB call and every
   worker media download.
3. **Domains.** Custom domains are optional for launch — the generated
   `*.up.railway.app` and `*.pages.dev` URLs work fine. Adding them later only
   requires updating `CORS_ORIGINS`, `ALLOWED_HOSTS`, `VITE_API_BASE_URL`, and
   the Supabase auth redirect list.

## 4. Deploy order — and why it matters

```
Supabase (DB/buckets/auth)  →  Redis  →  API  →  Worker  →  Frontend  →  Wire-back
```

Three hard ordering constraints:

- **Redis before API/worker.** `WorkerSettings.redis_settings` is evaluated at
  *import* time (`RedisSettings.from_dsn(...)`, `app/worker.py:154`). A missing
  or malformed `REDIS_URL` crashes the worker on boot, not at first job.
- **API before worker.** Migrations run *only* on the API service
  (`preDeployCommand: alembic upgrade head`). The worker's optimize sweep
  queries `media.optimized_at` (migration `0013`); starting it against an
  un-migrated DB errors every 5 minutes.
- **Frontend after API**, because it needs `VITE_API_BASE_URL` baked in at
  build time (Vite inlines `VITE_*` — changing it later requires a rebuild).

**The chicken-and-egg:** the API's `CORS_ORIGINS` needs the Pages URL, which
doesn't exist until the frontend deploys. Resolution: generate the Railway
domain first, deploy the frontend against it, then set `CORS_ORIGINS` and
redeploy the API (§9). Leave `ALLOWED_HOSTS` **unset** for the initial bring-up
— it's fail-open by design precisely so this ordering isn't painful.

---

## 5. Phase 1 — Supabase

1. **Buckets.** Confirm `surf-media` (session media) and `profile-media`
   (avatars) exist. Both should be **private** — session media is served through
   the API media proxy, not public URLs.
2. **Connection string.** Click the green **Connect** button at the **top of the
   dashboard** — connection strings live here now, not under Settings → Database.
   In the modal, choose **Session pooler** and copy the URI.
   - It should be port **5432** on `…pooler.supabase.com` (session mode), not
     transaction mode's **6543**.
   - **If the modal only shows a Transaction pooler string:** the two are
     identical apart from the port — copy it and change `:6543` to `:5432` to get
     the session-mode string. Anatomy:
     `postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres`
   - Rewrite the driver prefix to `postgresql+asyncpg://`.
   - **Why session mode:** Railway runs a persistent container with a bounded
     SQLAlchemy pool, so we don't need transaction pooling — and session mode is
     IPv4-reachable *and* supports asyncpg prepared statements, avoiding a code
     change. (If you *must* use transaction mode/6543, `app/core/db.py` needs
     `connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0}`
     on `create_async_engine` — session mode avoids the change.)
3. **Keys.** Grab `SUPABASE_URL`, anon key, service-role key, and the JWT secret
   (Settings → API).
4. Leave auth redirect URLs for §9 (needs the Pages domain).

## 6. Phase 2 — Redis

Railway project → **New → Database → Redis**. Copy the **private** connection
URL (service-to-service over Railway's internal network — no public egress).
This becomes `REDIS_URL` for both the API and the worker.

> Cost note: Upstash's free tier is the ~$0 alternative, but arq holds blocking
> pops on a long-lived connection, which suits a real Redis better. At a few
> dollars a month, Railway Redis is the safer default for a job queue.

## 7. Phase 3 — API service

1. Railway → **New → Deploy from GitHub repo → `surf-coach-api`**, branch `main`.
2. Railway auto-detects `railway.json`, which already sets:
   - build from `Dockerfile` (final stage = `prod`)
   - `preDeployCommand: alembic upgrade head`
   - `startCommand: uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`
   - `healthcheckPath: /health`, `numReplicas: 1`
3. Set env vars (§11). **Set `VIDEO_OPTIMIZE_ENABLED=false` for now** — per the
   video spec's rollout, ship it disabled and enable after smoke-testing (§10).
4. **Settings → Networking → Generate Domain.** Record it — this is
   `VITE_API_BASE_URL` for the frontend.
5. Verify: the deploy log shows alembic running to `0013`, and the health check
   goes green.

```bash
curl -s https://<your-api>.up.railway.app/health
```

> `numReplicas: 1` is deliberate: it keeps the Supabase session-mode connection
> count small and avoids two instances racing the pre-deploy migration.

## 8. Phase 4 — Worker service

1. In the **same Railway project**: **New → GitHub repo → `surf-coach-api`**
   again. This creates a second service from the same repo.
2. In that service's settings, set the **config-as-code path** to
   **`railway.worker.json`** — this is the step that makes it a worker
   (`startCommand: arq app.worker.WorkerSettings`, no migrations, no health check).
3. Give it the **same env vars as the API** (§11). Use Railway **shared
   variables** at the project level so the two services can't drift.
4. **Do not** generate a public domain — the worker serves no HTTP.
5. Verify from the deploy logs: arq starts and registers the cron jobs
   (`sweep_stuck_jobs` every minute, `sweep_unoptimized_media` every 5 minutes,
   both `run_at_startup=True`).

## 9. Phase 5 — Frontend (Cloudflare Pages)

1. Cloudflare → **Workers & Pages → Create → Pages → Connect to Git →
   `surf-coach-ui`**, production branch `main`.
2. Build settings:
   - Build command: `npm run build`
   - Output directory: `dist`
   - **Set `NODE_VERSION` (e.g. `20` or `22`)** — Vite 5 + Tailwind 4 need a
     modern Node, and Cloudflare's default can be older than the build expects.
3. Set the `VITE_*` env vars (§11) — including `VITE_API_BASE_URL` = the Railway
   domain from §7.
4. `public/_redirects` is already committed, so deep links and refreshes resolve
   to `index.html` instead of 404ing.
5. Record the `*.pages.dev` URL.

## 10. Phase 6 — Wire-back

Now that both URLs exist, close the loop:

1. **API `CORS_ORIGINS`** → `["https://<your-app>.pages.dev"]` (JSON array —
   pydantic parses `list[str]` from env as JSON). Redeploy the API.
2. **Supabase → Auth → URL Configuration** → set Site URL and add the Pages
   domain to the redirect allow-list, so magic links / OAuth return to prod.
3. **`ALLOWED_HOSTS` (optional, defer until after smoke test).** Leaving it
   unset is fail-open and fine behind Railway's proxy. If you do tighten it,
   include the Railway app domain **and** the host Railway's health check uses —
   otherwise the health check stops passing and the service is marked unhealthy.
   Tighten it only while watching the deploy log, and revert if the check goes red.

## 11. Phase 7 — Smoke test

Run top to bottom against production:

- [ ] `GET /health` → 200.
- [ ] Sign up / log in from the Pages URL (confirms Supabase auth + redirect URLs).
- [ ] Deep-link refresh on an inner route (e.g. `/sessions/<id>/plan`) → loads,
      not a 404 (confirms `_redirects`).
- [ ] Create a session; upload a video (confirms storage write + moderation +
      duration probe).
- [ ] Request a review → status goes `processing` → `completed` inside the
      5-minute polling window (confirms Redis, worker, Gemini end-to-end).
- [ ] **Replay the video** — it plays and seeks (confirms the media proxy +
      Range requests).
- [ ] Generate a training plan (confirms the second worker task).
- [ ] Kill the worker service mid-review → the row lands `failed` with a retry
      button within `STUCK_JOB_THRESHOLD_SEC` + 60s (confirms the sweeper).
- [ ] Check browser console/network for CORS errors (confirms `CORS_ORIGINS`).

## 12. Phase 8 — Enable video optimization

Only after §11 passes:

1. Set `VIDEO_OPTIMIZE_ENABLED=true` on **both** API and worker; redeploy.
2. Confirm `ffmpeg` is present in the worker container and the cron is live.
3. Upload landscape, portrait, and iPhone HEVC/`.mov` clips. After
   `VIDEO_OPTIMIZE_GRACE_SEC` (900s default) plus one sweep tick, check worker
   logs for the reduction line (`optimize: media … -> … bytes (-NN%)`).
4. **Re-verify replay** on an optimized video — this is the acceptance gate:
   same media id, same URL, smaller file, still seekable.
5. Watch the first sweep cycles: any pre-existing videos have
   `optimized_at = NULL` and will be compressed gradually, throttled by
   `VIDEO_OPTIMIZE_BATCH` (20/tick). Expect a one-time egress bump as the
   backlog is downloaded to the worker once.
6. Tune `VIDEO_CRF` / `VIDEO_TARGET_HEIGHT` if quality or size needs adjusting.

---

## 13. Environment variables

### API + Worker (identical — use Railway shared variables)

| Variable | Value / note |
|---|---|
| `APP_ENV` | `production` |
| `DATABASE_URL` | `postgresql+asyncpg://…@…pooler.supabase.com:5432/postgres` (session mode) |
| `REDIS_URL` | Railway Redis **private** URL |
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` / `SUPABASE_SERVICE_ROLE_KEY` / `SUPABASE_JWT_SECRET` | from Supabase |
| `SUPABASE_BUCKET` | `surf-media` |
| `GEMINI_API_KEY` | Google AI key |
| `GEMINI_MODEL` | `gemini-2.0-flash` |
| `CORS_ORIGINS` | `["https://<app>.pages.dev"]` (JSON array) |
| `ALLOWED_HOSTS` | leave **unset** initially (fail-open) |
| `SENTRY_DSN` | optional; empty disables |
| `LOG_LEVEL` | `INFO` |
| `VIDEO_OPTIMIZE_ENABLED` | `false` at first deploy → `true` in §12 |
| `PORT` | injected by Railway — do not set |

Defaults that need no override unless tuning: `FRAME_EXTRACT_COUNT`,
`MAX_UPLOAD_SIZE_MB`, `MAX_UPLOAD_FILES`, `MIN_UPLOAD_PHOTOS`, `MAX_UPLOAD_PHOTOS`, `MAX_UPLOAD_VIDEOS`,
`MAX_VIDEO_DURATION_SEC`, `TRAINING_WORKOUTS_PER_PLAN`,
`CONTENT_MODERATION_ENABLED`, `WORKER_MAX_JOBS`, `WORKER_JOB_TIMEOUT_SEC`,
`STUCK_JOB_THRESHOLD_SEC`, `RATE_LIMIT_*`, `VIDEO_TARGET_HEIGHT`, `VIDEO_CRF`,
`VIDEO_KEEP_AUDIO`, `VIDEO_AUDIO_BITRATE_KBPS`, `VIDEO_OPTIMIZE_GRACE_SEC`,
`VIDEO_OPTIMIZE_BATCH`.

### Frontend (Cloudflare Pages)

| Variable | Value / note |
|---|---|
| `VITE_API_BASE_URL` | `https://<your-api>.up.railway.app` |
| `VITE_SUPABASE_URL` | Supabase project URL |
| `VITE_SUPABASE_ANON_KEY` | anon key (public — safe in the bundle) |
| `VITE_SUPABASE_AVATAR_BUCKET` | `profile-media` |
| `NODE_VERSION` | `20` or `22` |
| `SENTRY_AUTH_TOKEN` / `SENTRY_ORG` / `SENTRY_PROJECT` | optional; enables source-map upload at build |

> Never put the **service-role key** in the frontend — it's server-only. The
> frontend gets the anon key.

## 14. Rollback

| Component | How |
|---|---|
| API / Worker | Railway → Deployments → redeploy the previous build (instant) |
| Frontend | Cloudflare Pages → Deployments → rollback to prior deployment |
| Video optimization | Set `VIDEO_OPTIMIZE_ENABLED=false` — the task and sweep both no-op immediately |
| Database | Migration `0013` is **additive and nullable**, so rolling back application code needs **no** DB downgrade |
| Dependency upgrade | Redeploy the previous build as above, then `git revert` the `uv.lock` change so the next deploy doesn't bring it back (§16) |

Note that video optimization is **not** reversible per-file: once a raw video is
replaced by its compressed version, the original bytes are gone. That's the
intended behavior (replay is preserved), but it's why §12 is gated behind a
passing smoke test rather than enabled on first deploy.

## 15. Post-launch watch list

- **Supabase storage usage** — the one cost line that grows monotonically. The
  1 GB free tier is the trigger for the $25 Pro plan.
- **Aggregate optimization ratio** from worker logs — confirms the ~85–90%
  reduction is actually landing.
- **Worker job durations** — transcode + Gemini stacking toward
  `WORKER_JOB_TIMEOUT_SEC` (240s) is the early warning to split or raise it.
- **Supabase connection count** — should stay small with one replica in session
  mode; a spike means the pool or replica count changed.
- **Sentry error rate** after enabling optimization — ffmpeg failures are
  swallowed by design, so they show as logs, not user-facing errors.

## 16. Dependencies — the lock file

`pyproject.toml` declares the *allowed* version ranges; `uv.lock` pins the exact
version and sha256 hashes of every package, for Linux and macOS alike. The
Dockerfile installs from the lock: the `deps` stage runs
`uv sync --locked --no-install-project` (runtime dependencies only — this is what
`prod` ships), and the `dev` stage adds the `dev` extra (pytest, ruff, …) on top.
CI installs the same lock, so the versions CI tested are the versions Railway runs.

Before the lock, every Railway build resolved to whatever was newest on PyPI. That
broke production twice with no code change: SQLAlchemy 2.1 stopped installing
`greenlet` unless the `asyncio` extra was requested, crashing the
`alembic upgrade head` pre-deploy step; and FastAPI 0.137 began nesting included
routers, which slowapi 0.1.10's middleware can't see, so every route silently
dropped out of `RATE_LIMIT_DEFAULT`. Now versions change only when a PR changes
`uv.lock`.

Install uv locally with `brew install uv` (or see https://docs.astral.sh/uv/).

| Task | Command |
|---|---|
| Add or change a dependency | edit `pyproject.toml`, then `uv lock` |
| Upgrade one package | `uv lock --upgrade-package <name>` |
| Upgrade everything | `uv lock --upgrade` |
| Check the lock matches `pyproject.toml` | `uv lock --check` |
| Apply the lock to your local `.venv` | `uv sync --extra dev` |

`uv lock` re-resolves only what it must and prints one line per change
(`Updated fastapi v0.136.3 -> v0.137.0`) — paste that into the PR description.

- **Commit `uv.lock` with `pyproject.toml`.** If they disagree, `uv lock --check`
  fails CI and `uv sync --locked` fails the Railway build, so a mismatch stops the
  deploy rather than resolving something new.
- **Treat a lock change as a code change.** Upgrade in a PR and let CI run the full
  suite — it runs the repository integration tests against Postgres, and
  `tests/test_rate_limit.py` catches the slowapi/FastAPI regression. For a broad
  `uv lock --upgrade`, also build the image locally first:
  `docker build --target prod .`
- **Upper bounds need a reason.** When an upgrade breaks something, cap it in
  `pyproject.toml` with a comment naming the breakage and the test that catches it
  (see `fastapi>=0.115,<0.137`), re-lock, and lift the cap once the cause is fixed.
- **uv itself is pinned** in two places: the Dockerfile
  (`COPY --from=ghcr.io/astral-sh/uv:<version>`) and `.github/workflows/ci.yml`
  (`setup-uv` → `version:`). Bump them together.
