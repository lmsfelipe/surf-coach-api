# DEPLOY — Production Runbook (UI + API)

**Status:** Ready to execute
**Owner:** Backend
**Date:** 2026-07-22
**Target scale:** ≤100 active users at launch
**Related:** `SPEC_BACKEND_Merge_Worker_Into_API.md`, `SPEC_BACKEND_Job_Queue_Hardening.md`, `SPEC_BACKEND_Media_Proxy.md`

---

## 1. What gets deployed where

| Component | Repo | Platform | Notes |
|---|---|---|---|
| **UI** (Vite/React SPA) | `surf-coach-ui` | **Cloudflare Pages** | Static `dist/`, free tier, global CDN |
| **API** (FastAPI) | `surf-coach-api` | **Railway** service #1 (the only one) | Dockerfile `prod` stage; runs migrations **and** background jobs |
| **Postgres** | — | **Supabase** (existing) | Session pooler; no separate managed DB |
| **Auth + Storage** | — | **Supabase** (existing) | JWT + `surf-media` / `profile-media` buckets |
| **Gemini** | — | Google AI | `gemini-2.0-flash` |
| **Errors** | — | Sentry (optional) | `SENTRY_DSN` empty = disabled |

There is no worker service and no Redis: reviews and training plans run inside
the API process behind an in-process `JobRunner`. With nothing polling, the
service produces no outbound traffic when idle, so Railway **Serverless** puts it
to sleep (§17) and an idle day costs ≈ $0.

Expected cost: **a few $/mo** at launch (mostly active-minute RAM), rising to
~$25–30/mo once Supabase Pro is needed for video storage.

## 2. Pre-flight — already verified in the repos

- [x] Dockerfile installs deps from `uv.lock` (`uv sync --locked`, hash-verified) —
      every build ships the exact versions CI tested, and `prod` gets runtime deps
      only, no test/lint tools. See §16.
- [x] `railway.json` committed (one service; `--workers 1`).
- [x] `ALLOWED_HOSTS` setting exists and is honored in `app/main.py`
      (**fail-open** when unset — an unset var cannot 400 the whole deploy).
- [x] `public/_redirects` committed in the UI repo (SPA deep-link fallback).
- [x] Migrations through `0013_add_media_optimized_at.py` present. `media.optimized_at`
      and `optimize_attempts` are unused since the server-side optimizer was removed;
      they stay until a later migration drops them.
- [x] Both repos pushed to GitHub (`lmsfelipe/surf-coach-api`, `lmsfelipe/surf-coach-ui`).

## 3. Decide before starting

1. **UI branch.** `surf-coach-ui` is on `feat/frontend-implementation`; the API
   is on `main`. Either merge the UI branch to `main` (recommended — keeps
   "production = main" consistent across both repos) or point Cloudflare Pages
   at the feature branch. **The rest of this runbook assumes `main`.**
2. **Region.** Pick the **same region** for Railway and the Supabase project
   (e.g. both `us-east`). Cross-region adds latency to every DB call and every
   media download a job makes.
3. **Domains.** Custom domains are optional for launch — the generated
   `*.up.railway.app` and `*.pages.dev` URLs work fine. Adding them later only
   requires updating `CORS_ORIGINS`, `ALLOWED_HOSTS`, `VITE_API_BASE_URL`, and
   the Supabase auth redirect list.

## 4. Deploy order — and why it matters

```
Supabase (DB/buckets/auth)  →  API  →  Frontend  →  Wire-back
```

Two hard ordering constraints:

- **Supabase before the API.** Migrations run as the API's
  `preDeployCommand: alembic upgrade head`, and the app needs the project keys to
  construct `Settings` at import time.
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
   - **Connection budget.** Session mode caps *every* client of the project at
     the pooler's pool size — **15** by default (Database → Settings →
     Connection pooling). Each pooled connection the app holds counts, idle or
     not, and an over-cap connect fails with `EMAXCONNSESSION … max clients
     reached in session mode`. One process now serves HTTP **and** jobs from a
     single pool of `DB_POOL_SIZE + DB_MAX_OVERFLOW` (5 + 0 by default), leaving
     plenty of room for the pre-deploy migration and a redeploy's overlap. Jobs
     release their connection across downloads and Gemini, so
     `JOBS_MAX_CONCURRENT` (2) fits easily — but keep
     `JOBS_MAX_CONCURRENT + concurrent HTTP DB use ≤ DB_POOL_SIZE` if you raise it.
3. **Keys.** Grab `SUPABASE_URL`, anon key, service-role key, and the JWT secret
   (Settings → API).
4. Leave auth redirect URLs for §9 (needs the Pages domain).

## 6. Phase 2 — (removed)

There is no Redis and no worker service any more. If you are migrating an
existing deployment, follow the rollout in
`docs/SPEC_BACKEND_Merge_Worker_Into_API.md` §6: deploy the API, smoke-test,
remove the worker service, keep Redis for about a week as a rollback path, then
delete it and the `REDIS_URL` shared variable.

## 7. Phase 3 — API service

1. Railway → **New → Deploy from GitHub repo → `surf-coach-api`**, branch `main`.
2. Railway auto-detects `railway.json`, which already sets:
   - build from `Dockerfile` (final stage = `prod`)
   - `preDeployCommand: alembic upgrade head`
   - `startCommand: uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`
   - `healthcheckPath: /health`, `numReplicas: 1`
3. Set env vars (§13).
4. **Settings → Networking → Generate Domain.** Record it — this is
   `VITE_API_BASE_URL` for the frontend.
5. **Settings → Serverless: on**, and set `RAILWAY_DEPLOYMENT_DRAINING_SECONDS`
   to at least `JOB_SHUTDOWN_GRACE_SEC + 5` (30 with the defaults) so a redeploy
   can drain its jobs instead of being SIGKILLed mid-drain (§17).
6. Verify: the deploy log shows alembic running to `0013`, the startup sweep line
   appears once, and the health check goes green.

```bash
curl -s https://<your-api>.up.railway.app/health
```

> `numReplicas: 1` and `--workers 1` are deliberate: each uvicorn worker would
> run its own `JobRunner` and its own DB pool, multiplying idle RAM and the
> Supabase session-mode connection count, and two instances would race the
> pre-deploy migration.

## 8. Phase 4 — (removed)

Jobs run in the API process. Nothing to deploy here.

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
      5-minute polling window (confirms the job runner and Gemini end-to-end).
- [ ] **Replay the video** — it plays and seeks (confirms the media proxy +
      Range requests).
- [ ] Generate a training plan (confirms the second task).
- [ ] `GET /health/live` stays fast (< 100 ms) while a review is running —
      confirms the heavy steps are off the event loop.
- [ ] Redeploy mid-review → the row lands `failed` with "Processing was
      interrupted." within `JOB_SHUTDOWN_GRACE_SEC` (confirms the drain), and a
      Retry works.
- [ ] After ≥ 15 min with no traffic, the Railway service shows **Sleeping** (§17).
- [ ] Check browser console/network for CORS errors (confirms `CORS_ORIGINS`).

## 12. Phase 8 — (removed)

Videos are compressed in the browser before upload (Mediabunny/WebCodecs, 720p),
so there is no server-side transcode to enable. The optimizer and its periodic
sweep were removed with the worker: a sweep on a timer would keep the service
awake, which is exactly what §17 trades away.

---

## 13. Environment variables

### API

| Variable | Value / note |
|---|---|
| `APP_ENV` | `production` |
| `DATABASE_URL` | `postgresql+asyncpg://…@…pooler.supabase.com:5432/postgres` (session mode) |
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` / `SUPABASE_SERVICE_ROLE_KEY` / `SUPABASE_JWT_SECRET` | from Supabase |
| `SUPABASE_BUCKET` | `surf-media` |
| `GEMINI_API_KEY` | Google AI key |
| `GEMINI_MODEL` | `gemini-2.0-flash` |
| `CORS_ORIGINS` | `["https://<app>.pages.dev"]` (JSON array) |
| `ALLOWED_HOSTS` | leave **unset** initially (fail-open) |
| `SENTRY_DSN` | optional; empty disables |
| `LOG_LEVEL` | `INFO` |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | leave at `5` / `0` — sized for Supabase's session-mode cap (§5) |
| `RAILWAY_DEPLOYMENT_DRAINING_SECONDS` | ≥ `JOB_SHUTDOWN_GRACE_SEC + 5` (30 with the defaults) — see §17 |
| `PORT` | injected by Railway — do not set |

Defaults that need no override unless tuning: `FRAME_EXTRACT_COUNT`,
`MAX_UPLOAD_SIZE_MB`, `MAX_UPLOAD_FILES`, `MIN_UPLOAD_PHOTOS`, `MAX_UPLOAD_PHOTOS`,
`MAX_UPLOAD_VIDEOS`, `MAX_VIDEO_DURATION_SEC`, `TRAINING_WORKOUTS_PER_PLAN`,
`CONTENT_MODERATION_ENABLED`, `RATE_LIMIT_*`, and the job/Gemini budget below.

**Job runner** (all optional; the defaults are safe and the app refuses to boot on
an inconsistent combination):

| Variable | Default | Note |
|---|---|---|
| `JOBS_MAX_CONCURRENT` | `2` | Jobs running at once. Each review holds video bytes + frames in RAM, in the container that also serves HTTP. |
| `JOBS_MAX_QUEUED` | `4` | Jobs allowed to wait for a slot; past this a submit is rejected and the row reads "busy, try again". |
| `JOB_MAX_WAIT_SEC` | `60` | Cap on the wait part of the budget, so a job that starts has `JOB_TIMEOUT_SEC − JOB_MAX_WAIT_SEC` left to run. |
| `JOB_TIMEOUT_SEC` | `240` | Total budget from submit (wait + run). Must stay under `STUCK_JOB_THRESHOLD_SEC`. |
| `JOB_SHUTDOWN_GRACE_SEC` | `25` | How long a redeploy waits for running jobs. |
| `STUCK_JOB_THRESHOLD_SEC` | `300` | Age at which the startup sweep and the read-path check fail a `processing` row. |
| `GEMINI_REQUEST_TIMEOUT_SEC` | `90` | Per-attempt HTTP timeout; a Gemini thread cannot be cancelled, so without it a hung call outlives its job. |
| `GEMINI_MIN_ATTEMPT_SEC` | `10` | Don't start (or retry into) an attempt with less budget left than this. |
| `GEMINI_DEADLINE_MARGIN_SEC` | `5` | Stop Gemini work this long before the job deadline. |

After a week of traffic, tune from the `job.finished` logs: take p95 `run_sec` as
`R`, pick `JOBS_MAX_CONCURRENT` from RAM headroom, then
`JOBS_MAX_QUEUED ≈ JOBS_MAX_CONCURRENT × JOB_MAX_WAIT_SEC / R`.

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
| API | Railway → Deployments → redeploy the previous build (instant) |
| Frontend | Cloudflare Pages → Deployments → rollback to prior deployment |
| Job throughput | Raise `JOBS_MAX_CONCURRENT` / `JOBS_MAX_QUEUED`, or lower them if RAM is tight — config only, no deploy of code |
| Back to the worker | Only possible while Redis still exists: redeploy the pre-merge API build and re-add the worker service from `railway.worker.json` at that commit (spec §6) |
| Database | Migration `0013` is **additive and nullable**, so rolling back application code needs **no** DB downgrade |
| Dependency upgrade | Redeploy the previous build as above, then `git revert` the `uv.lock` change so the next deploy doesn't bring it back (§16) |

## 15. Post-launch watch list

- **Supabase storage usage** — the one cost line that grows monotonically. The
  1 GB free tier is the trigger for the $25 Pro plan.
- **`job.finished` logs** — one line per job with `outcome`, `wait_sec` and
  `run_sec`. `outcome=rejected_busy` or `wait_timeout` (also a Sentry breadcrumb)
  means the limits are too tight for the traffic; `run_sec` creeping toward
  `JOB_TIMEOUT_SEC` is the early warning to raise it or split the work.
- **Container RAM** — reviews now spike memory in the process that serves HTTP.
  An OOM takes HTTP down with it; Railway restarts it and the startup sweep fails
  the orphaned rows, but `JOBS_MAX_CONCURRENT` is the lever.
- **Supabase connection count** — should stay at or under 5 with one replica in
  session mode; a spike means the pool or replica count changed.
  `EMAXCONNSESSION` in Sentry means the cap of 15 was hit (§5).
- **Idle state** — the service should read **Sleeping** after ~10–15 min without
  traffic. If it never sleeps, something is producing outbound traffic on a timer
  (§17).

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

## 17. Serverless, sleeping, and the drain window

Railway bills RAM per minute of runtime, so an always-awake service costs money
with zero users. A Serverless service sleeps after ~10 min **without outbound
traffic**, and traffic to other services in the same project counts — which is
why the old arq worker (polling Redis every ~0.5 s) kept itself, Redis, and the
bill awake. Nothing in the API runs on a timer now: no cron, no queue polling.
SQLAlchemy only pings on checkout (`pool_pre_ping`), idle asyncpg connections
send nothing, and Sentry only sends events and request sessions.

Two consequences to keep in mind:

- **No sleeping mid-job.** A running job talks to Supabase and Gemini, which is
  outbound traffic, so the container stays awake until it finishes — at most
  `JOB_TIMEOUT_SEC` (240 s), well inside the idle window.
- **Cold start.** The first request after sleeping waits for the container to
  boot: Python imports plus the startup sweep (two indexed `UPDATE`s, capped at
  10 s). Heavy imports (`cv2`, `google.genai`) are deliberately lazy; keep them
  that way.

**Drain window.** Railway sends SIGTERM, then SIGKILL after the deployment
draining period. `JobRunner.close()` cancels queued jobs immediately and gives
running ones `JOB_SHUTDOWN_GRACE_SEC` (25 s) before cancelling them; each then
marks its own row `failed` with "Processing was interrupted." so the user sees a
Retry button instead of a spinner. Set
`RAILWAY_DEPLOYMENT_DRAINING_SECONDS ≥ JOB_SHUTDOWN_GRACE_SEC + 5`, or the drain
is killed half-way and the next startup sweep has to clean up instead — still
correct, just slower for the user. Check the variable's name and default in
Railway's docs when setting it.
