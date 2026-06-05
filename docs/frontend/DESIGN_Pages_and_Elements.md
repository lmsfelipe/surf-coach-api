# Surf Coach — Pages & Elements (for Claude Design)

> **Document 2 of 5** in the frontend spec set.
> **Purpose:** Paste-ready inventory of every screen, its sections, the components/elements each needs, and all UI states — so the Claude Design system covers every surface before code generation.
> **Status:** Draft for review
> **Last updated:** 2026-06-01

**Global conventions:** brand **SurfRise** · mobile-first · **dark-only "Midnight Electric"** · single accent = electric `#3D5BFF` · pt-BR copy ("você", "·" separator, no emoji) · 3-tab bottom bar + center FAB · progress-bar score viz · **skeletons** for content, **3-dot pulser (no spinner)** for AI/indeterminate · wave size shown in **meters** (feet in API). See `SPEC_FRONTEND_StyleGuide.md` for tokens & components.

---

## How to read this

For each screen you'll find:
- **Route** and **purpose**
- **Sections** (top → bottom)
- **Elements/components** used (mapped to the component inventory in §3)
- **States**: `loading` (skeleton), `empty`, `error`, `success`, plus any business states
- **Key copy** (pt-BR) for the main labels/CTAs

If your existing Design System already defines a token/component, reuse it; this doc flags anything new that the system must add (marked **[NEW]**).

---

## 1. Information Architecture

```
Auth (public)
  Login · Signup · Forgot password · Reset password

App (authenticated)
  Bottom Tab Bar: [ Sessões ]  [ Treinos ]  (＋ FAB: Nova sessão)  [ Perfil ]
  Onboarding (first run)
  Sessões
    Histórico (list)         ← Tab 1, home
    Nova sessão (form)       ← opened by center FAB
    Detalhe da sessão        ← Option A: compact hub, links out
      Upload de mídia
      Análise (AI review)
      Plano de treino
  Treinos                    ← Tab 2
    Lista de planos          ← needs GET /api/v1/training-plans (future endpoint)
    → Plano de treino (reuses /sessions/$id/plan view)
  Perfil                     ← Tab 3
    Ver perfil
    Editar perfil
    Pranchas (inventário)    ← MOVED here from bottom bar
      Nova prancha / Editar prancha
    Configurações (logout)
```

> **Bottom bar = 3 destinations + 1 center FAB.** Tabs: `Sessões`, `Treinos`, `Perfil`, flanking a central **＋ FAB** whose only action is **Nova sessão**. **Pranchas** is no longer a tab — it's reached from the Profile screen.

---

## 2. Global Layout Elements

### 2.1 App Shell (authenticated)
- **Top bar (contextual):** screen title (left), optional action (right, e.g. "+"). Back arrow on detail/sub screens.
- **Content area:** scrollable, safe-area padded, max-width container centered on desktop.
- **Bottom Tab Bar [organism]:** 3 tabs — `Sessões` (wave/list icon), `Treinos` (dumbbell icon), `Perfil` (user icon) — flanking a **center ＋ FAB**. Active tab highlighted. Hidden on auth screens. On desktop ≥`md`, becomes a left side rail (FAB becomes a prominent "Nova sessão" button at top of the rail).
- **Center FAB [organism]:** single action → **Nova sessão** (`/sessions/new`). Always visible in the app shell.

### 2.2 Global feedback
- **Toast/Snackbar [molecule]** — success/error/info; auto-dismiss; bottom-anchored above tab bar.
- **Confirm Dialog [molecule]** — destructive actions; title + body + cancel/confirm (danger).
- **Network/Offline banner [molecule]** — sticky top, appears on fetch failure.
- **Full-screen loader** — only for app boot/auth resolution; screens otherwise use skeletons.

---

## 3. Component Inventory (what the Design System must cover)

### Atoms
- Button (variants: primary, secondary, ghost, danger, icon-only; sizes sm/md/lg; loading spinner state)
- Input (text, number) · Textarea · Select/Dropdown · DatePicker · Switch/Toggle
- Label · Helper text · Field error text **[for RHF+zod errors]**
- Badge/Chip (e.g. surf level, board type, "Sem análise")
- Avatar (image + initials fallback)
- Icon (lucide set)
- Score value pill (big number, e.g. overall `7.1`)
- Progress bar (0–10 score) **[NEW — labeled, valued, color-by-score]**
- Skeleton primitives (line, block, circle)
- Spinner

### Molecules
- Form Field (label + control + error) **[wraps RHF]**
- Session Card **[NEW]** (location, date, wave size, board, overall score)
- Training Plan Card **[NEW]** (session/date reference, workout count, focus areas; links to plan)
- Board Card **[NEW]** (type, size, volume, label; edit/delete actions)
- Media Thumbnail **[NEW]** (image/video preview, video play badge, delete button, upload-progress overlay)
- Score Bar Row **[NEW]** (dimension label + progress bar + numeric value)
- Tip Item **[NEW]** (numbered improvement tip)
- Empty State (icon + title + subtitle + optional CTA)
- Error State (icon + message + retry button)
- Stat/summary row (label + value), used on session detail
- Toast, Confirm Dialog, Offline banner (see §2.2)
- File Picker / Dropzone **[NEW]** (enforces 1 video OR ≤3 images, shows constraints)

### Organisms
- Bottom Tab Bar / Side Rail (+ center ＋ FAB)
- Training Plan List
- App Top Bar (title + back + action)
- Session List
- Board List
- Score Panel **[NEW]** (overall score pill + 6 Score Bar Rows)
- Review Card **[NEW]** (narrative + Score Panel + 3 Tip Items + plan CTA)
- Training Plan Accordion **[NEW]** (workouts → exercises, collapse/expand)
- Profile Header **[NEW]** (avatar, name, surf level, quick stats)
- Auth Form Card (login/signup shells)
- Media Gallery **[NEW]** (grid of Media Thumbnails + add button)
- AI Progress State **[NEW]** ("Analisando sua sessão…" with a **3-dot electric pulser — no spinner**; distinct from skeleton)

### Design tokens the system must define (or that we'll add)
- Color: brand/primary, surface levels (dark theme), text (primary/muted), success/warning/danger, **score scale ramp** (low→high, e.g. red→amber→green) **[NEW]**
- Typography scale (display, h1–h3, body, caption, numeric/score)
- Spacing scale, radius, shadow/elevation (dark-mode aware)
- Component states: hover/active/focus/disabled, focus ring (a11y)
- Breakpoints: mobile (base), `md` (tablet/desktop side-rail switch)

---

## 4. Screens

### A. Auth

#### A1 — Login `/auth/login`
- **Sections:** Brand/logo · Email field · Password field · "Entrar" button · links: "Esqueci minha senha", "Criar conta".
- **Components:** Auth Form Card, Form Field ×2, Button (primary), text links.
- **States:** loading (button spinner), error (invalid creds → inline alert "E-mail ou senha inválidos"), success (redirect).
- **Copy:** Title "Entrar" · CTA "Entrar" · "Esqueci minha senha" · "Não tem conta? Criar conta".

#### A2 — Signup `/auth/signup`
- **Sections:** Brand · **Name (required)** · Email · Password · Confirm password · "Criar conta" button · link "Já tenho conta".
- **States:** loading, validation errors (zod — name required), error (email already used → alert), success (→ onboarding).
- **Copy:** Title "Criar conta" · label "Nome" · CTA "Criar conta".
- **Note:** name is captured at signup and saved to the profile via `PATCH /me` immediately after first `GET /me`.

#### A3 — Forgot Password `/auth/forgot-password`
- **Sections:** Email field · "Enviar link" · confirmation message.
- **States:** loading, success ("Enviamos um link para seu e-mail"), error.

#### A4 — Reset Password `/auth/reset-password`
- **Sections:** New password · Confirm · "Salvar nova senha".
- **States:** loading, validation, success (→ login), invalid/expired link error.

---

### B. Onboarding `/onboarding`
- **Purpose:** capture required profile data on first run for better AI plans.
- **Sections:** Welcome heading · **Surf level (required)** · **Height cm (required, 100–250)** · **Weight kg (required, 30–200)** · optional: gender, birthday, avatar upload · "Concluir". (Name already captured at signup.)
- **Components:** Form Field set, Select (surf level), number inputs, Avatar uploader (client-direct to `profile-media`), Button.
- **States:** loading, validation (surfLevel + height + weight required), success (→ Sessões), avatar upload progress/error.
- **Copy:** Title "Bem-vindo!" · "Qual seu nível de surf?" · "Altura (cm)" · "Peso (kg)" · CTA "Concluir".
- **Note:** onboarding is considered complete only once `surfLevel`, `heightCm`, and `weightKg` are set; the route guard uses this to decide first-run redirect.

---

### C. Sessions (Tab 1)

#### C1 — Session History `/sessions` (home)
- **Sections:** Top bar "Sessões" + "+" action · scrollable list of **Session Cards** · primary "Nova sessão" CTA.
- **Session Card shows:** location, date, wave size (m), board label (or "—"), overall score chip (or "Sem análise"). _Wave size displayed in meters; stored as feet — see style guide §8._
- **Components:** Session List (organism), Session Card (molecule), Empty State, Error State, Skeleton list.
- **States:**
  - `loading` → skeleton cards.
  - `empty` → "Nenhuma sessão ainda" + "Registrar primeira sessão" CTA.
  - `error` → Error State + retry.
  - per-card score chip resolves independently (skeleton chip → score / "Sem análise").
- **Copy:** "Sessões" · "Nova sessão" · "Sem análise" · empty: "Nenhuma sessão ainda".

#### C2 — New Session `/sessions/new`
- **Sections:** Back + title "Nova sessão" · Date (required) · Location (required) · Wave size ft (required) · Surfboard selector (optional, from inventory) · Notes (optional) · "Salvar".
- **Components:** Form Fields, DatePicker, number input, Select (boards), Textarea, Button.
- **States:** loading (boards select uses skeleton/disabled), validation errors, submit pending, success (→ detail or upload), error toast.
- **Copy:** labels "Data", "Local", "Tamanho da onda (m)", "Prancha", "Observações" · CTA "Salvar e enviar mídia". (Wave size via Slider in meters; board size stays in feet — surf convention.)

#### C3 — Session Detail `/sessions/$sessionId` (Option A — compact hub)
- **Layout:** compact hub. Review and plan show **short summaries** only; full content lives on standalone `/review` and `/plan` routes (each with its own skeleton/error/retry).
- **Sections:**
  1. Header: location + date, wave size, board, overall score (if any).
  2. Media Gallery (thumbnails) + "Adicionar mídia" → upload route.
  3. Review entry: if review exists → summary + "Ver análise"; else if media exists → "Gerar análise"; else → hint "Adicione mídia para analisar".
  4. Plan entry: if plan exists → "Ver plano"; else if review exists → "Gerar plano de treino".
  5. Danger: "Excluir sessão" (confirm — warns cascade).
- **Components:** Profile-less header/stat rows, Media Gallery, Review Card (summary), Buttons, Confirm Dialog.
- **States:** loading (skeleton header + gallery), empty media, no review, no plan, error, delete pending.
- **Copy:** "Adicionar mídia" · "Gerar análise" · "Ver análise" · "Gerar plano de treino" · "Excluir sessão".

#### C4 — Media Upload `/sessions/$sessionId/upload`
- **Sections:** Back + title · File Picker/Dropzone (rules: **1 vídeo OU até 3 imagens**, ≤100MB, vídeo ≤120s) · selected files list with per-file progress · existing media grid with delete · "Enviar".
- **Components:** File Picker/Dropzone, Media Thumbnail (with progress overlay + delete), Button, error messages.
- **States:** idle, validating (client checks MIME/size/duration), uploading (progress per file), success, per-file error (`FILE_TOO_LARGE`, `VIDEO_TOO_LONG`, `INVALID_MEDIA_TYPE`), delete confirm.
- **Copy:** "Adicionar mídia" · rule hint "1 vídeo ou até 3 imagens" · errors mapped to pt-BR.

#### C5 — AI Review `/sessions/$sessionId/review`
- **Sections:**
  1. Overall score pill (prominent).
  2. Score Panel: 6 Score Bar Rows — Flow, Drop, Equilíbrio, Escolha de ondas, Manobras, Braços.
  3. Narrative text (pt-BR prose).
  4. 3 improvement Tip Items.
  5. CTA "Gerar plano de treino" (or "Ver plano" if exists).
- **Components:** Score value pill, Score Panel, Score Bar Row ×6, Tip Item ×3, Review Card, Button.
- **States:**
  - **generation pending** → AI Progress State "Analisando sua sessão…" (3–15s).
  - `success` → full review.
  - `error` → `AI_GENERATION_FAILED`/`AI_PARSE_FAILED` with **retry**.
  - `NO_MEDIA_FOR_SESSION` → prompt to upload.
  - `REVIEW_ALREADY_EXISTS` → show existing.
- **Copy:** dimension labels (pt-BR) · "Pontuação geral" · "Dicas de melhoria" · "Analisando sua sessão…".

#### C6 — Training Plan `/sessions/$sessionId/plan`
- **Sections:** Title · plan meta (gerado por IA) · Training Plan Accordion: workouts (sequence, title, focus area) → exercises (name, description, sets, reps, optional video link).
- **Components:** Training Plan Accordion (organism), exercise rows, optional video link, Button.
- **States:** generation pending (AI Progress State, 5–20s), success, error + retry, `TRAINING_PLAN_ALREADY_EXISTS` → show existing.
- **Copy:** "Plano de treino" · "Séries" · "Repetições" · "Gerado por IA".

---

### D. Treinos (Tab 2)

> **Data dependency:** requires a future **`GET /api/v1/training-plans`** (list) endpoint (user confirmed they'll add it). Spec'd as a dependency; until it exists, the tab shows an empty/placeholder state.

#### D1 — Training Plans List `/training-plans`
- **Sections:** Top bar "Treinos" · list of **Training Plan Cards** (most recent first).
- **Training Plan Card shows:** source session (location + date), workout count, focus-area chips, "Ver plano".
- **Components:** Training Plan List, Training Plan Card, Empty State, Error State, Skeleton.
- **States:** loading (skeleton), empty ("Nenhum treino gerado ainda" + hint to generate from a session's review), error + retry.
- **Copy:** "Treinos" · empty "Nenhum treino gerado ainda" · "Ver plano".
- **Navigation:** card → reuses the plan view at `/sessions/$sessionId/plan` (or `/training-plans/$planId` if linking by plan id).

---

### E. Profile (Tab 3)

#### E1 — Profile `/profile`
- **Sections:** Profile Header (avatar, name, surf level badge, quick stats: height/weight) · menu list: **"Minhas pranchas"** → `/boards`, "Editar perfil" → `/profile/edit`, "Configurações" → `/settings`.
- **Components:** Profile Header, menu/list rows, Buttons, stat rows.
- **States:** loading (skeleton header), error.
- **Copy:** "Perfil" · "Minhas pranchas" · "Editar perfil" · "Configurações".

#### E2 — Edit Profile `/profile/edit`
- **Sections:** Avatar uploader (client-direct → `profile-media`) · Name · Surf level (required) · Gender · Birthday · Height · Weight · "Salvar".
- **States:** loading prefill, validation, avatar upload progress/error, submit pending, success.
- **Copy:** field labels (pt-BR) · CTA "Salvar alterações".

#### E3 — Board Inventory `/boards` (reached from Profile → "Minhas pranchas")
- **Sections:** Back + title "Pranchas" + "+" · list of Board Cards.
- **Board Card shows:** type (badge), size (ft), volume (L, if any), label.
- **Components:** Board List, Board Card, Empty State, Error State, Skeleton.
- **States:** loading, empty ("Nenhuma prancha cadastrada" + CTA), error.
- **Copy:** "Pranchas" · "Nova prancha".

#### E4 — Add/Edit Board `/boards/new`, `/boards/$boardId/edit`
- **Sections:** Board type (required select) · Size ft (required) · Volume L (optional) · Label (optional) · Save; edit also has "Excluir prancha".
- **States:** loading (edit prefill), validation, submit pending, success, delete confirm (warns sessions keep, reference nulls).
- **Copy:** labels "Tipo", "Tamanho (pés)", "Volume (litros)", "Apelido" · CTA "Salvar".

#### E5 — Settings `/settings`
- **Sections:** account info · "Sair" (logout) · app version.
- **States:** logout pending.
- **Copy:** "Configurações" · "Sair".

---

## 5. Score Dimension Labels (pt-BR)

| API field | Label (pt-BR) |
|---|---|
| `scoreFlow` | Fluxo (flow) |
| `scoreDrop` | Take-off & pop-up |
| `scoreBalance` | Postura & equilíbrio |
| `scoreWaveSelection` | Escolha da onda |
| `scoreManeuvers` | Manobras |
| `scoreArms` | Braços |
| `overallScore` | Nota geral |

Progress-bar color ramp by value: 0–4 danger, 4–7 warning, 7–10 success (define exact stops in the style guide).

---

## 6. State Coverage Matrix (must be designed)

| Screen | loading | empty | error | success | special |
|---|---|---|---|---|---|
| Session list | ✓ skeleton | ✓ | ✓ retry | ✓ | per-card score chip |
| Session detail | ✓ | media/review/plan empty | ✓ | ✓ | delete confirm (cascade) |
| Upload | — | no files | per-file error | ✓ | progress, duration/size/MIME validation |
| Review | AI progress | no media → prompt | AI fail → retry | ✓ | already-exists redirect |
| Plan | AI progress | — | AI fail → retry | ✓ | already-exists redirect |
| Treinos list | ✓ skeleton | ✓ | ✓ retry | ✓ | needs future list endpoint |
| Boards list | ✓ | ✓ | ✓ | ✓ | — |
| Board form | prefill | — | validation | ✓ | delete confirm |
| Profile | ✓ | — | ✓ | ✓ | avatar upload |
| Auth | button spinner | — | inline alerts | redirect | — |

---

## 7. Resolved Decisions Log

| # | Question | Decision |
|---|---|---|
| 1 | Primary action | **Center ＋ FAB** in the tab bar; its only action is **Nova sessão** |
| 2 | Bottom tabs | **Sessões · Treinos · Perfil** (Pranchas moved under Profile) |
| 3 | Detail layout | **Option A** — compact hub, links to standalone /review and /plan |
| 4 | Score ramp | Derive low/mid/high from existing palette (exact stops in style guide) |
| 5 | Onboarding | **surfLevel + height + weight required**; name captured at signup |
| 6 | Signup | **Name required** |
| 7 | Treinos data | Depends on future **`GET /api/v1/training-plans`** list endpoint |
```