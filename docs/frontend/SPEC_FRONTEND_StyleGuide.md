# SPEC — SurfRise Frontend Style Guide

> **Document 3 of 5** in the frontend spec set.
> **Status:** Draft for review
> **Last updated:** 2026-06-01
> **Source of truth:** the exported **Claude Design** system — _"Midnight Electric"_ (dark direction).

## 0. Provenance

This style guide is a faithful translation of the exported SurfRise design system into the React/Tailwind/shadcn stack. **Do not invent tokens** — everything here traces back to these files:

| File | Role |
|---|---|
| `colors_and_type.css` | **Authoritative** token source (CSS variables): colors, type, spacing, radii, shadows, motion. **Dark "Midnight Electric".** |
| `ui_kits/app/components.jsx` | Reference implementations: AppHeader, Button, Field, TextInput, Select, Slider, SessionCard, ScoreRow, Pill, Eyebrow, FAB, BottomNav |
| `ui_kits/app/screens.jsx` | Reference screens: Sessions list, New session, Session detail, Workout |
| `ui_kits/app/icons.jsx` | Curated Lucide-style icon set |
| root `README.md` | Voice & content rules, "one accent per screen", motion philosophy |

> **Theme decision:** the app ships **dark-only "Midnight Electric"** for the MVP. A light "Ocean Serenade" variant exists in the design system but is **out of scope** now. (The root README describes the older light direction with Poppins/Lato — that is superseded by `colors_and_type.css`'s dark tokens with Inter Tight / Plus Jakarta Sans / Archivo. Its **voice/content rules still apply.**)

> **Brand name:** **SurfRise** — wordmark renders "Surf" + "Rise" (accent), font `Pacifico`. The "·" middle dot (U+00B7) is the metadata separator everywhere (`Canal 1 · Santos/SP`).

---

## 1. Color Tokens

### 1.1 Core palette (one accent only)

| Token (CSS var) | Hex | Role |
|---|---|---|
| `--color-midnight` | `#0B1020` | Page canvas / `--bg` |
| `--color-surface` | `#141B2E` | Cards |
| `--color-surface-2` | `#1A2236` | Nested surfaces, inputs |
| `--color-electric` | `#3D5BFF` | **The single accent** — primary actions, links, focus, highlights |

### 1.2 Text & lines

| Token | Hex | Role |
|---|---|---|
| `--color-text` | `#FFFFFF` | Primary text |
| `--color-text-soft` | `#B9C2DC` | Body copy / secondary |
| `--color-text-muted` | `#8590AB` | Captions, helper, timestamps |
| `--color-text-faint` | `#6C7891` | Disabled, hints |
| `--color-line` | `#1F2740` | Solid borders on surface |
| `--color-line-soft` | `rgba(255,255,255,0.06)` | Dividers inside cards |

### 1.3 Status

| Token | Hex |
|---|---|
| `--success` | `#00C781` |
| `--warning` | `#FFB547` |
| `--danger` | `#FF4D6D` |

### 1.4 Ramps (extend only through these — never add hues)

- **Electric ramp** `--eb-00 … --eb-100`: `#EEF1FF, #C5CEFF, #9CABFF, #7288FF, #5872FF, #4862FF, #3D5BFF (canonical), #2C45D9, #1F32A8`
- **Midnight ramp** `--mn-00 … --mn-100`: `#2A334D, #232B43, #1F2740, #1A2236, #141B2E, #0F1525, #0B1020, #070B17, #04060F`

### 1.5 Score color ramp (for ScoreRow progress bars)

The kit draws score bars in **white-55%** by default and **electric** when a row is the standout. To make the 0–10 value legible at a glance we add a value-based ramp (built from existing status tokens — no new hues):

| Score | Fill |
|---|---|
| `0.0–3.9` | `--danger` `#FF4D6D` |
| `4.0–6.9` | `--warning` `#FFB547` |
| `7.0–10.0` | `--success` `#00C781` |
| "standout"/overall | `--color-electric` (accent) |

> Default per the kit = subtle white-55% bars + accent on the best row. The value-ramp is opt-in (`tone="value"`) for screens where color-coding helps (review screen). Confirm in review if you want the ramp on by default or only the kit's accent-one-row style.

---

## 2. shadcn / Tailwind Token Bridge

shadcn/ui expects semantic CSS variables. We map them onto SurfRise tokens so every shadcn component themes correctly without overrides. **Import `colors_and_type.css` verbatim**, then add this bridge.

### 2.1 `src/index.css` (bridge layer)

```css
@import './styles/colors_and_type.css'; /* the exported file, unchanged */
@import 'tailwindcss';

:root {
  /* shadcn semantic tokens → SurfRise (dark Midnight Electric) */
  --background:           var(--color-midnight);
  --foreground:           var(--color-text);
  --card:                 var(--color-surface);
  --card-foreground:      var(--color-text);
  --popover:              var(--color-surface);
  --popover-foreground:   var(--color-text);
  --primary:              var(--color-electric);
  --primary-foreground:   #FFFFFF;
  --secondary:            var(--color-surface-2);
  --secondary-foreground: var(--color-text);
  --muted:                var(--color-surface-2);
  --muted-foreground:     var(--color-text-muted);
  --accent:               var(--color-electric);
  --accent-foreground:    #FFFFFF;
  --destructive:          var(--danger);
  --destructive-foreground:#FFFFFF;
  --border:               var(--color-line);
  --input:                var(--color-line);
  --ring:                 var(--color-electric);

  --radius:               var(--radius-md); /* 12px base for shadcn */
}

/* App is dark-only for MVP: apply the dark class at the root. */
html { color-scheme: dark; }
```

### 2.2 Tailwind theme (`@theme` / `tailwind.config`)

Expose SurfRise scales as Tailwind utilities (Tailwind v4 `@theme` shown; mirror in JS config if on v3):

```css
@theme {
  /* fonts */
  --font-display: 'Inter Tight', system-ui, sans-serif;
  --font-body:    'Plus Jakarta Sans', system-ui, sans-serif;
  --font-heading: 'Archivo', 'Inter Tight', system-ui, sans-serif;

  /* radii */
  --radius-sm: 8px;
  --radius-md: 12px;
  --radius-lg: 18px;
  --radius-pill: 999px;

  /* spacing already matches Tailwind's 4px base (space-1..16) */

  /* elevation */
  --shadow-sm: 0 1px 0 rgba(255,255,255,0.04) inset, 0 4px 12px rgba(0,0,0,0.30);
  --shadow-md: 0 1px 0 rgba(255,255,255,0.05) inset, 0 12px 28px rgba(0,0,0,0.40);
  --shadow-lg: 0 1px 0 rgba(255,255,255,0.06) inset, 0 24px 56px rgba(0,0,0,0.55);
}
```

---

## 3. Typography

### 3.1 Families

| Family | CSS var | Use |
|---|---|---|
| **Inter Tight** | `--font-display` | Display, big numerics (scores), tabular numbers |
| **Plus Jakarta Sans** | `--font-body` | UI labels, body copy, buttons |
| **Archivo** | `--font-heading` | h1/h2 headings |
| **Pacifico** | — | Logo wordmark only |
| mono | `--font-mono` | code/model version strings |

Load via the Google Fonts `@import` already in `colors_and_type.css`.

### 3.2 Scale (from tokens)

| Class | Size / line | Tracking | Font / weight |
|---|---|---|---|
| `.t-display` | 48 / 50 | -0.045em | Inter Tight 300 (brand signature: light + huge) |
| `h1 / .t-h1` | 28 / 34 | -0.025em | Archivo 800 |
| `h2 / .t-h2` | 20 / 26 | -0.01em | Archivo 700 |
| `h3 / .t-h3` | 16 / 22 | — | Plus Jakarta 600 |
| `.t-eyebrow` | 10 / 14 | 0.18em, UPPERCASE | Plus Jakarta 700, muted |
| `p / .t-body-l` | 14 / 21 | — | Plus Jakarta, text-soft |
| `.t-body` | 12 / 18 | — | Plus Jakarta, text-soft |
| `.t-caption` | 10 / 14 | — | Plus Jakarta, muted |
| `.t-numeric` | — | -0.03em | Inter Tight 500, tabular-nums |

> **Score numerals** (session card 32px, hero 68px, ScoreRow 18px) all use `.t-numeric` style: Inter Tight, `font-variant-numeric: tabular-nums`, tight tracking, with the decimal point optionally accent-colored (`6<accent>.</accent>7` on the hero).

---

## 4. Spacing · Radii · Elevation · Motion

- **Spacing:** 4px base — `--space-1..16` (4,8,12,16,20,24,32,40,48,64). Maps 1:1 to Tailwind spacing.
- **Radii:** inputs/chips `sm 8`, buttons/tiles `md 12`, **cards/sheets `lg 18` (signature)**, pills `999`.
- **Elevation:** `shadow-sm` (cards), `shadow-md` (menus/toast/hero), `shadow-lg` (modals/sheets); `shadow-focus` = 3px electric @35%; `shadow-glow` for the accent glow.
- **Motion:** entry `cubic-bezier(0.22,1,0.36,1)`, symmetric `cubic-bezier(0.65,0,0.35,1)`. Durations 120 / 200 / 360ms. **Fades over slides** (opacity + 4–8px translate). Never linear, never spring-bounce.

---

## 5. Component Inventory → shadcn Mapping

Each existing SurfRise component maps to a shadcn primitive (added via **Shadcn MCP**) themed by the §2 bridge. "Custom" = build on top of shadcn/Tailwind, no off-the-shelf primitive.

| SurfRise component | shadcn base | Notes |
|---|---|---|
| `Button` (primary/accent/secondary/outline/ghost/surface) | `button` | Map variants: primary/accent→`default` (electric), secondary/surface→`secondary`, outline→`outline`, ghost→`ghost`, danger→`destructive`. Radius 10–12, weight 600. |
| `Field` + label + hint + **error** | `form` + `label` | Wrap RHF; add **error text** in `--danger` (kit lacked it — **[NEW]**). |
| `TextInput` | `input` | surface-2 bg, `--border`, radius 8. |
| `Select` | `select` | custom chevron already in kit; prefer shadcn `select` for a11y. |
| Textarea | `textarea` | per New Session screen. |
| `Slider` (wave size, live readout) | `slider` | keep the big Inter Tight readout + unit; **meters** (see §8). |
| `SessionCard` | Custom (card) | location, date, **score (Inter Tight)**, wave (IconWave), board (IconBoard), chevron. |
| `ScoreRow` | Custom | label + value + thin progress bar; supports `accent` and value-ramp (§1.5). |
| Score panel (6 rows) | Custom | wraps ScoreRow ×6 inside a `card`. |
| `Pill` (brand/accent/action/success/muted) | `badge` | extend badge with the 5 tones. |
| `Eyebrow` | Custom | `.t-eyebrow` span. |
| `AppHeader` | Custom | logo OR back button + title + avatar; transparent bg. |
| `FAB` | Custom | 56px round, electric, glow shadow; center, above bottom nav. |
| `BottomNav` | Custom | **3 tabs** + center FAB (see §7). Glass blur `rgba(11,16,32,0.85)` + `blur(16px)`, 2px electric indicator above active tab. |
| Hero session card | Custom | gradient `160deg #1A2236→#141B2E→#0B1020`, radial electric glow, big 68px score. |
| Exercise card | Custom | 64px media tile (play/barbell), numbered badge, sets/reps pills, description. |
| Avatar | `avatar` | image + initials fallback (kit shows "MV" initials). |
| Toast | `sonner` | dark surface, shadow-md. |
| Confirm dialog | `alert-dialog` | destructive actions. |
| Skeleton | `skeleton` | surface-2 blocks (see §6). |
| Dropzone / file picker | Custom | **[NEW]** — enforces 1 video OR ≤3 images. |
| AI progress state | Custom | **3-dot pulser** in electric (NOT a spinner — see §6). |
| Tabs (if needed) | `tabs` | — |

**Components the kit does NOT yet have (must design/build):** field **error** text, Dropzone/file picker, Media thumbnail (with progress + delete), Training Plan Accordion, Training Plan Card, Profile Header, Empty State, Error State, Offline banner, AI progress pulser. These were flagged **[NEW]** in Doc 2 §3.

---

## 6. Loading & Feedback Conventions

The brand rule (root README): **"Never a spinner."** Reconciled with our suspense architecture:

- **Content loading** (lists, detail initial fetch) → **shadcn `skeleton`** blocks in `--color-surface-2`, matching the final layout. Every `useSuspenseQuery` boundary has a skeleton fallback.
- **Indeterminate / AI generation** (`POST /reviews`, `POST /training-plans`, 3–20s) → **3-dot pulser** in electric + copy ("Analisando sua sessão…"). A single thin electric top progress bar is the alternate.
- **Buttons busy** → replace label with the 3-dot pulser (no circular spinner), keep button width, disable.
- **Toasts** (`sonner`) for success/unexpected errors. **No exclamation marks** in system copy.
- **Empty / Error states** are first-class components (Doc 2 §3) with the brand's inviting/blame-the-system tone.

---

## 7. Navigation (reconciled)

- **Bottom Tab Bar = 3 tabs:** `Sessões` (IconHome), `Treinos` (IconBarbell), `Perfil` (IconUser) + a **center ＋ FAB** (IconPlus) whose only action is **Nova sessão**.
- The kit's 4th tab **"Evolução" (IconProgress) is dropped** for the MVP (no progress API). Reserved for post-MVP.
- Bar: 78px tall, glass `rgba(11,16,32,0.85)` + `backdrop-filter: blur(16px)`, `--color-line-soft` top border, 2px electric indicator + glow above the active tab. On desktop ≥`md` → left side rail; FAB becomes a "Nova sessão" button at the top of the rail.
- **Pranchas** is NOT a tab — reached from the Profile screen.

---

## 8. Units — Wave Size (meters in UI, feet in API)

- **API stores `waveSize` in feet.** **UI displays & inputs meters.**
- Convert at the API boundary: `meters = feet * 0.3048`, `feet = meters / 0.3048`.
- Slider: range ~`0.0–4.0 m`, step `0.1`, big Inter Tight readout + `m` unit (per the kit's Slider). Store the converted feet value on submit.
- Centralize conversion in `utils/units.ts`; never scatter the 0.3048 factor.

---

## 9. Score Dimensions — Labels (pt-BR, reconciled with kit voice)

API returns 6 dimensions; the kit displayed 5 with nicer phrasing. Final labels:

| API field | Label (pt-BR) |
|---|---|
| `scoreFlow` | Fluxo (flow) |
| `scoreDrop` | Take-off & pop-up |
| `scoreBalance` | Postura & equilíbrio |
| `scoreWaveSelection` | Escolha da onda |
| `scoreManeuvers` | Manobras |
| `scoreArms` | Braços |
| `overallScore` | Nota geral |

> The review screen renders **all 6** (the kit's mock showed 5 because `scoreArms` happened to be null in sample data — bars render only for non-null scores, exactly like the API contract).

---

## 10. Iconography

- **Source:** the kit's inline Lucide-style set (1.8 stroke, rounded caps, 24 viewBox, `currentColor`). In code we use **`lucide-react`** (MIT, closest match — confirmed by the design system).
- Mapping kit → lucide-react: `IconWave→Waves`, `IconBoard→` (custom surfboard path, keep as inline SVG), `IconSun→Sun`, `IconClock→Clock`, `IconProgress→TrendingUp`, `IconBarbell→Dumbbell`, `IconUser→User`, `IconHome→Home`, `IconPlay→Play`, `IconSparkle→Sparkles`, `IconPlus→Plus`, chevrons → `ChevronLeft/Right/Down`, `IconCheck→Check`, `IconPin→MapPin`, `IconCalendar→Calendar`, `IconImage→Image`, `IconVideo→Video`, `IconArrowRight→ArrowRight`.
- **Keep the hand-drawn surfboard (`IconBoard`) and wave glyphs** as inline SVG — they're brand-specific and have no exact lucide equivalent.
- **No emoji** anywhere in product UI. Color = `currentColor`; electric only for the single accent action.

---

## 11. Voice & Content Rules (from the design system — apply in all copy)

- **Language:** Portuguese (Brasil), informal **"você"**. **Sentence-case** labels/buttons/headings (UPPERCASE only for tiny eyebrows + unit symbols).
- **Separator:** `·` middle dot for metadata (`Canal 1 · Santos/SP`, `25 mai 2026 · 06:42`).
- **Specificity over fluff:** name what happened. AI review must reference concrete observations, never generic praise.
- **Surfer-English kept untranslated:** drop, take-off, bottom-turn, pop-up, line-up, shortboard, longboard, funboard, glass-off.
- **Tone matrix:** empty = inviting/low-pressure; AI narrative = observant, kind, second-person; tips = imperative, one verb; errors = plain, blame-the-system ("Não conseguimos enviar o vídeo. Tente de novo?"); buttons = verb-first ("Salvar sessão", "Analisar com IA", "Ver treino sugerido").
- **No emoji. No exclamation marks** in system messages. No corporate hedging.
- **One accent per screen** — exactly one electric CTA competing for attention. If you want two, it's a hierarchy problem.

---

## 12. Reconciliation / Decisions Log

| # | Item | Decision |
|---|---|---|
| 1 | Theme | **Dark-only "Midnight Electric"** for MVP; light direction out of scope |
| 2 | Token source | `colors_and_type.css` is authoritative; root README dark-superseded except voice rules |
| 3 | Accent | **Electric `#3D5BFF`** is the single accent (coral collapsed) |
| 4 | Bottom nav | **3 tabs** (Sessões · Treinos · Perfil) + center FAB; **Evolução dropped** |
| 5 | Wave unit | **Meters in UI**, feet in API, convert via `utils/units.ts` (×0.3048) |
| 6 | Loading | **Skeletons** for content; **3-dot pulser** (no spinner) for AI/indeterminate & busy buttons |
| 7 | Score labels | Kit's pt-BR phrasing for the 6 API dimensions (§9) |
| 8 | Icons | `lucide-react`; keep inline SVG for surfboard/wave brand glyphs |
| 9 | Brand | **SurfRise** wordmark (Pacifico), "·" separator, no emoji |

---

## 13. Resolved Decisions Log (cont.)

| # | Item | Decision |
|---|---|---|
| 10 | Score bars | **Kit default** — subtle white bars, electric on the standout row (value ramp §1.5 NOT used by default) |
| 11 | Light theme | **Fully skipped** for MVP — dark-only |
| 12 | Brand glyphs | **Keep surfboard/wave as inline SVG**; everything else via `lucide-react` |
| 13 | Repo | Frontend lives in a **separate repo** (user will create); these specs travel to it, and codegen (Doc 5) targets it |
```