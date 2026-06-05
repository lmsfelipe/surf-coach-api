# SPEC — Surf Coach Frontend (Overview & Architecture)

> **Document 1 of 5** in the frontend spec set.
> **Status:** Draft for review
> **Last updated:** 2026-06-01
> **Companion docs:** `DESIGN_Pages_and_Elements.md`, `SPEC_FRONTEND_StyleGuide.md`, layout specs, codegen.

---

## 1. Product Summary

A mobile-first web app where a surfer logs surf sessions, uploads media (1 video **or** up to 3 images), and receives AI-generated performance feedback (narrative + 6 scores + 3 tips) and an optional AI training plan. This frontend integrates with the **Surf Coach API** (see `../FRONTEND_INTEGRATION.md`) and **Supabase Auth/Storage**.

### Locked decisions

| Decision | Choice |
|---|---|
| Feature scope | **API-backed MVP only** — no marketplace/coaches/photographers/board-recommendation |
| UI language | **Portuguese (pt-BR)**, hardcoded strings (no i18n framework) |
| Form factor | **Mobile-first responsive**, scales up to desktop |
| Auth | **Supabase email/password only** (signup, login, logout, password reset) |
| Primary nav | **Bottom tab bar** (mobile), adapts to side rail on desktop |
| Score viz | **Progress bars** per dimension + prominent overall score (no chart lib) |
| Design source | Existing **Claude Design** system (tokens + core components already defined) |

---

## 2. Tech Stack

| Concern | Choice | Notes |
|---|---|---|
| Build tool | **Vite** | Raw React, **not** Next.js |
| Language | **TypeScript** | strict mode |
| Routing | **TanStack Router** | file-based or code-based routes, typesafe |
| Data fetching | **TanStack Query** + **Suspense** | `useSuspenseQuery` for reads, `useMutation` for writes |
| Styling | **Tailwind CSS** | tokens mirror the Claude Design system |
| Components | **shadcn/ui** via **Shadcn MCP** | search + add components through MCP |
| Loading UX | **Skeleton** components | every suspense boundary has a skeleton fallback |
| Global state | **Zustand** | only where needed (auth/session, UI prefs) — server state lives in Query |
| Forms | **react-hook-form** + **zod** | `zodResolver`, schema-per-form |
| Auth/Storage | **@supabase/supabase-js** | auth session + direct avatar upload to Storage |
| HTTP | thin `fetch` wrapper | injects Bearer token, parses error envelope |
| Icons | **lucide-react** | ships with shadcn |
| Dates | **date-fns** | format/parse `YYYY-MM-DD` and ISO |
| Theme | **dark mode default** | `class` strategy; light theme available but dark is the default |
| Testing | **Vitest** + **Testing Library** | unit + component tests; `@testing-library/react`, `user-event`, `jsdom`; MSW for API mocking |

> **State split rule:** Server data is **never** mirrored into Zustand. TanStack Query is the single source of truth for anything from the API. Zustand holds only auth session/token and ephemeral UI prefs.

---

## 3. Route Map

All app routes except `/auth/*` are protected (require a Supabase session). Unauthenticated access redirects to `/auth/login`.

```
/                              → redirect to /sessions (or /onboarding if profile incomplete)
/auth/login                    → Login
/auth/signup                   → Signup
/auth/forgot-password          → Request reset email
/auth/reset-password           → Set new password (from email link)

/onboarding                    → First-run: complete required profile (surfLevel + height + weight)

# Tab 1 — Sessões
/sessions                      → Session history list (home)
/sessions/new                  → Create session form (opened by center FAB)
/sessions/$sessionId           → Session detail (Option A: compact hub, links out)
/sessions/$sessionId/upload    → Media upload screen (standalone route)
/sessions/$sessionId/review    → AI review view (standalone route)
/sessions/$sessionId/plan      → Training plan view (standalone route)

# Tab 2 — Treinos
/training-plans                → Training plans list (needs future GET /api/v1/training-plans)
/training-plans/$planId        → Plan view (or reuse /sessions/$id/plan)

# Tab 3 — Perfil (Pranchas live under here, not in the tab bar)
/profile                       → Profile view + entry to Pranchas/Editar/Configurações
/profile/edit                  → Edit profile form
/boards                        → Surfboard inventory list (reached from Profile)
/boards/new                    → Add board form
/boards/$boardId/edit          → Edit board form
/settings                      → Account/settings (logout, etc.)
```

> **Bottom tab bar:** `Sessões` · `Treinos` · `Perfil`, flanking a center **＋ FAB** whose only action is **Nova sessão**. Pranchas is accessed from the Profile screen, not the tab bar.

> `upload`, `review`, and `plan` are **standalone routes** (decided), each with its own suspense/error boundary and skeleton. The detail screen links into them and shows their summarized state.

### Route protection

- A root `__authenticated` layout route guards everything except `/auth/*`.
- Guard reads the Supabase session from the auth store; if absent, redirect to `/auth/login` preserving intended destination.
- After login, the first `GET /me` runs; onboarding is **incomplete** until `surfLevel`, `heightCm`, and `weightKg` are all set — if any is missing, redirect to `/onboarding`.

---

## 4. App Architecture

### 4.1 Folder structure

```
src/
  main.tsx                     # app bootstrap: providers (Query, Router, Supabase)
  routeTree.gen.ts             # generated by TanStack Router
  routes/                      # file-based routes
    __root.tsx
    _auth/                     # public auth layout
      login.tsx
      signup.tsx
      forgot-password.tsx
      reset-password.tsx
    _app/                      # authenticated layout (bottom tab bar)
      sessions/
        index.tsx
        new.tsx
        $sessionId/
          index.tsx
          upload.tsx
          review.tsx
          plan.tsx
      boards/
        index.tsx
        new.tsx
        $boardId.edit.tsx
      profile/
        index.tsx
        edit.tsx
      settings.tsx
      onboarding.tsx
  lib/
    supabase.ts                # supabase client singleton
    api/
      client.ts                # fetch wrapper: base URL, auth header, error envelope parsing
      errors.ts                # ApiError class, error-code → pt-BR message map
      endpoints/               # one module per resource
        profile.ts
        surfboards.ts
        sessions.ts
        media.ts
        reviews.ts
        trainingPlans.ts
    queryKeys.ts               # centralized query key factory
  hooks/
    queries/                   # useSuspenseQuery wrappers per resource
    mutations/                 # useMutation wrappers per resource
  stores/
    authStore.ts               # zustand: session, user, token getters
  components/
    ui/                        # shadcn components (added via MCP)
    skeletons/                 # skeleton fallbacks per screen/section
    layout/                    # BottomTabBar, AppShell, TopBar
    forms/                     # reusable form fields wrapping RHF + shadcn
    feedback/                  # ScoreBars, ReviewCard, EmptyState, ErrorState
  schemas/                     # zod schemas (mirror API validation constraints)
    profile.ts
    surfboard.ts
    session.ts
  types/                       # shared TS types mirroring API data models
  utils/                       # formatters (dates, file size, scores)
  config/                      # env, constants (MIME types, limits)
```

### 4.2 Providers (composition at `main.tsx`)

```
<QueryClientProvider>
  <RouterProvider router={router} />   // router context carries queryClient + auth
</QueryClientProvider>
```

- Supabase client is a module singleton (`lib/supabase.ts`), imported where needed.
- Auth store subscribes to `supabase.auth.onAuthStateChange` to keep token/session fresh.

---

## 5. API Integration Patterns

### 5.1 Fetch wrapper (`lib/api/client.ts`)

- Base URL from `import.meta.env.VITE_API_BASE_URL` (dev: `http://localhost:8000`).
- Reads current access token from the auth store; sets `Authorization: Bearer <token>`.
- On `401` with `INVALID_TOKEN`: attempt one silent Supabase refresh, then retry once; if still 401, sign out + redirect to login.
- Parses the API error envelope `{ error: { code, message, details } }` into a typed `ApiError`.
- Returns parsed JSON for 2xx; throws `ApiError` otherwise; treats `204` as `void`.

### 5.2 Error handling

- `lib/api/errors.ts` maps each API `code` (see integration guide §Error Code Reference) to a **pt-BR** user-facing message.
- Field-level validation (`VALIDATION_ERROR.details`) is surfaced back into the corresponding RHF field.
- A global toast renders unexpected errors (`INTERNAL_ERROR`, `500`, network failures).
- Specific business cases are handled at the call site, not as toasts:
  - `REVIEW_ALREADY_EXISTS` → navigate to existing review.
  - `NO_MEDIA_FOR_SESSION` → prompt to upload first.
  - `TRAINING_PLAN_ALREADY_EXISTS` → navigate to existing plan.
  - `AI_GENERATION_FAILED` / `AI_PARSE_FAILED` → inline error with **retry** button.

### 5.3 TanStack Query conventions

- **Reads:** `useSuspenseQuery` so each screen declares a `<Suspense fallback={<Skeleton/>}>` boundary; no manual `isLoading` branching.
- **Query keys:** centralized factory in `lib/queryKeys.ts`, e.g. `qk.sessions.list()`, `qk.sessions.detail(id)`, `qk.reviews.bySession(id)`.
- **Mutations:** invalidate the affected keys on success; optimistic updates only for cheap, reversible actions (e.g., delete board).
- **AI endpoints** (`POST /reviews`, `POST /training-plans`) are slow (3–20s): use `useMutation` with an explicit pending UI (not a skeleton — a progress/“analisando…” state), disable the trigger while pending.
- **Error boundaries:** each route segment wraps its suspense boundary in an error boundary that renders `ErrorState` with retry.

### 5.4 Resource → endpoint map

| Resource | Reads | Writes |
|---|---|---|
| Profile | `GET /me` | `PATCH /me` |
| Surfboards | `GET /api/v1/surfboards`, `GET .../{id}` | `POST`, `PATCH`, `DELETE` |
| Sessions | `GET /api/v1/sessions`, `GET .../{id}` | `POST`, `DELETE` |
| Media | `GET /api/v1/sessions/{id}/media`, `GET /api/v1/media/{id}` | `POST` (multipart), `DELETE` |
| Reviews | `GET /api/v1/sessions/{id}/review`, `GET /api/v1/reviews/{id}` | `POST /api/v1/reviews` |
| Training plans | `GET /api/v1/reviews/{id}/training-plan`, `GET .../{planId}` | `POST /api/v1/training-plans` |

---

## 6. Auth & Onboarding Flow

1. **Signup/Login** via Supabase email/password → store session in auth store (and Supabase persists to localStorage).
2. On authenticated app load, run `GET /me` (auto-creates profile on first call).
3. If the profile lacks a user-set `surfLevel` (required field), route to **`/onboarding`** to capture it (plus optional fields).
4. **Token refresh:** handled by Supabase SDK + the 401-retry path in the fetch wrapper.
5. **Logout:** `supabase.auth.signOut()`, clear auth store, redirect to `/auth/login`.
6. **Password reset:** `/auth/forgot-password` → Supabase `resetPasswordForEmail`; `/auth/reset-password` handles the redirect link and `updateUser`.

> Avatar upload is **client-direct** to the Supabase Storage **`profile-media`** bucket (separate from the `surf-media` session-media bucket); the resulting public URL is saved via `PATCH /me` (`avatarUrl`). The API never receives the avatar bytes. Session media still uploads through the API to `surf-media`.

---

## 7. Media Upload Rules (frontend-enforced)

Mirror API limits client-side for fast feedback (server still authoritative):

| Rule | Value |
|---|---|
| Accepted images | `image/jpeg`, `image/png`, `image/webp` |
| Accepted videos | `video/mp4`, `video/quicktime`, `video/x-m4v` |
| Max file size | 100 MB |
| Max video duration | 120 s (probe via a hidden `<video>` element before upload) |
| PRD selection rule | **1 video OR up to 3 images** per session (enforced in the picker UI) |
| Progress | per-file progress bar during multipart upload |

---

## 8. Validation (zod schemas mirror API)

Schemas in `src/schemas/` mirror the constraints table in the integration guide:

- `surfLevel ∈ {beginner, intermediate, advanced, pro}`
- `heightCm` int 100–250 · `weightKg` int 30–200 · `gender ∈ {male, female}`
- `name` ≤200 · `location` 1–200 · `notes` ≤1000
- `waveSize` > 0 · `boardType ∈ {shortboard, longboard, funboard, bodyboard, other}` · `boardSize` > 0 · `volume` > 0 · `label` ≤200

Each form uses `zodResolver`; server `VALIDATION_ERROR.details` is mapped back onto fields as a fallback.

---

## 8a. Session Card Data Resolution

Each session card (history list) displays: **location, date, wave size, board, overall score**. The list endpoint (`GET /api/v1/sessions`) returns `location`, `sessionDate`, `waveSize`, and `surfboardId` — but **not** the board label or the review's `overallScore`. To populate the card without N requests:

- **Board label:** fetch `GET /api/v1/surfboards` once (cached via `qk.surfboards.list()`) and resolve `surfboardId → label` client-side. Show `—` when `surfboardId` is null.
- **Overall score:** the list endpoint does not include it. For MVP, fetch each session's review lazily with `GET /api/v1/sessions/{id}/review` per card (suspense + skeleton on the score chip only), tolerating `404` (no review yet → show "Sem análise"). Cards render immediately; the score chip resolves independently so one slow/missing review never blocks the list.

> If per-card review requests prove too chatty, the fallback is to drop the score from the card and show it only on the detail screen. Flagged as a possible later optimization, not a blocker.

---

## 9. Testing (Vitest + Testing Library)

| Layer | What to test | Tools |
|---|---|---|
| **Unit** | zod schemas (valid/invalid cases), formatters (date, file size, score), error-code → pt-BR message map, query-key factory | Vitest |
| **Component** | forms (validation errors, submit), `ScoreBars`, session card, empty/error/skeleton states, media picker (1 video OR ≤3 images rule), confirmation dialogs | Vitest + `@testing-library/react` + `user-event` |
| **API mocking** | mock all endpoints incl. error envelopes and AI-slow/failure paths | **MSW** |
| **Hooks** | query/mutation hooks against MSW (invalidation, optimistic updates, 401-retry) | Vitest + Testing Library `renderHook` |

Conventions:
- Co-locate tests as `*.test.ts(x)` next to source.
- `jsdom` environment; `setupTests.ts` wires Testing Library matchers + MSW server lifecycle.
- Every reusable component in `components/feedback/` and `components/forms/` ships with a component test.
- Target: schemas, the API error map, and the media-selection rule are **must-cover**; aim for meaningful coverage over a percentage gate.

---

## 10. Global UX Conventions

- **Skeletons** for every initial list/detail load.
- **Empty states** for: no sessions, no boards, session without media, session without review/plan.
- **Error states** with retry for failed queries and AI generation.
- **Confirmation dialogs** for destructive actions (delete session — warns about cascade; delete board — warns sessions keep but reference nulls; delete media).
- **Optimistic + toast** feedback on create/update/delete where safe.
- **Offline/network** banner on fetch failure.
- All copy in **pt-BR**.

---

## 11. Environment & Config

```
VITE_API_BASE_URL=http://localhost:8000
VITE_SUPABASE_URL=...
VITE_SUPABASE_ANON_KEY=...
VITE_SUPABASE_AVATAR_BUCKET=profile-media   # client-direct avatar uploads
# session media uploads go through the API to the surf-media bucket (server-side)
```

Constants (`src/config/`): accepted MIME types, size/duration limits, board-type and surf-level option lists (with pt-BR labels), score-dimension labels.

---

## 12. Out of Scope (this MVP)

Marketplace, coaches, human review, photographers/booking, board recommendation, profile sharing, social features, native mobile, i18n. These exist in the PRD but are **not** built and **not** stubbed in the UI.

---

## 13. Resolved Decisions Log

| # | Question | Decision |
|---|---|---|
| 1 | upload/review/plan routing | **Standalone routes** under `/sessions/$sessionId/*` |
| 2 | Storage buckets | Avatar → **`profile-media`** (client-direct); session media → `surf-media` (via API) |
| 3 | Theme | **Dark mode default** (light available) |
| 4 | Session card fields | **location, date, wave size, board, overall score** (board + score resolved per §8a) |
| 5 | Testing | **Vitest + Testing Library** (unit + component), **MSW** for API mocking (§9) |
| 6 | Primary action | **Center ＋ FAB** in tab bar → only **Nova sessão** |
| 7 | Bottom tabs | **Sessões · Treinos · Perfil**; Pranchas moved under Profile |
| 8 | Detail layout | **Option A** — compact hub linking to standalone /review, /plan |
| 9 | Onboarding | **surfLevel + height + weight required**; name captured at signup (required) |
| 10 | Treinos | Depends on future **`GET /api/v1/training-plans`** list endpoint (user will add) |
```

> **Backend dependency:** the **Treinos** tab needs a new **`GET /api/v1/training-plans`** endpoint returning the user's plans (with enough metadata — source session location/date, workout count, focus areas — to render cards without N+1 fetches). Until it ships, the tab renders an empty/placeholder state.