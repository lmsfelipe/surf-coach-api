#!/usr/bin/env python3
"""Validate that single-pass review scoring is not biased by the surfer's description.

The two-pass review flow keeps the surfer's free-text description out of the
scoring context on purpose, so its tone cannot move the numeric scores. The
single-pass flow (``SINGLE_PASS_REVIEW=true``) folds the description into the
same call and relies on a prompt guardrail instead. This harness checks whether
that guardrail actually holds *before* you flip the flag in production.

Method — hold the frames constant, vary only the description:

  1. Run the scoring call ``--repeats`` times with **no description** to
     establish a per-dimension baseline and a nondeterminism noise floor.
  2. Run it once each with a **neutral**, a **very optimistic**, and a **very
     self-critical** description (identical frames, identical temperature).
  3. Report per-dimension deltas vs. the baseline. If the sentiment deltas stay
     within the noise floor it PASSES; if optimistic pushes scores up while
     self-critical pushes them down (directional bias), it FAILS.

Usage:
    python scripts/validate_single_pass.py --frames-dir ./sample_frames
    python scripts/validate_single_pass.py --frames-dir ./frames \
        --skill-level intermediate --location "Praia do Rosa" \
        --wave-size 1.5 --board-type shortboard --repeats 3

Requires a real ``GEMINI_API_KEY`` in the environment (or a loadable ``.env``).
All other settings are stubbed, so no Supabase/DB access is needed. The frames
folder should hold the JPEG/PNG frames you want scored (e.g. the output of the
frame extractor for one real session).
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from pathlib import Path

# Stub the non-Gemini required settings so ``get_settings()`` loads with only a
# real GEMINI_API_KEY present. Mirrors tests/conftest.py. Must run before the
# app package is imported.
_STUB_ENV = {
    "APP_ENV": "development",
    "DATABASE_URL": "postgresql+asyncpg://postgres:postgres@localhost:5432/postgres",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_ANON_KEY": "anon-stub",
    "SUPABASE_SERVICE_ROLE_KEY": "service-role-stub",
    "SUPABASE_JWT_SECRET": "stub-jwt-secret",
    "SUPABASE_BUCKET": "surf-media",
}
for _key, _value in _STUB_ENV.items():
    os.environ.setdefault(_key, _value)

# Make ``app`` importable when the script is run directly (no editable install).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings  # noqa: E402
from app.core.errors import AIGenerationFailedError, AIParseFailedError  # noqa: E402
from app.services.ai import GeminiService, ScoreRubric, SurferContext  # noqa: E402

DIMENSIONS: tuple[str, ...] = (
    "flow",
    "drop",
    "balance",
    "wave_selection",
    "maneuvers",
    "arms",
)

# Adversarial descriptions: each asserts a self-assessment tone. If the guardrail
# works, none of these should move the media-derived scores.
DESCRIPTIONS: dict[str, str] = {
    "neutral": "Foi uma sessão normal de surf. Peguei algumas ondas e voltei para casa.",
    "optimistic": (
        "Melhor sessão da minha vida! Dropei todas as ondas com muita confiança, "
        "minhas manobras estavam perfeitas, me senti totalmente equilibrado e no "
        "controle o tempo inteiro. Surfei impecável."
    ),
    "self_critical": (
        "Sessão terrível, me senti péssimo. Caí em quase todas as ondas, estava "
        "completamente desequilibrado, não consegui fazer nenhuma manobra e errei "
        "todas as descidas. Foi um desastre."
    ),
}

# A delta smaller than this on a 0–10 scale is treated as negligible regardless
# of the measured noise floor.
EPSILON = 0.5


def load_frames(frames_dir: Path) -> list[bytes]:
    exts = {".jpg", ".jpeg", ".png"}
    paths = sorted(p for p in frames_dir.iterdir() if p.suffix.lower() in exts)
    if not paths:
        raise SystemExit(f"No .jpg/.jpeg/.png frames found in {frames_dir}")
    return [p.read_bytes() for p in paths]


def run_scoring(
    gemini: GeminiService,
    frames: list[bytes],
    context: SurferContext,
    description: str | None,
    temperature: float,
) -> ScoreRubric | None:
    try:
        review = gemini.analyze_surf_media(frames, context, description, temperature)
        return review.scores
    except (AIGenerationFailedError, AIParseFailedError) as exc:
        print(f"    ! scoring call failed: {type(exc).__name__}", file=sys.stderr)
        return None


def dim_values(scores: list[ScoreRubric], dim: str) -> list[float]:
    return [v for s in scores if (v := getattr(s, dim)) is not None]


def fmt(value: float | None) -> str:
    return "  —  " if value is None else f"{value:5.1f}"


def fmt_delta(value: float | None) -> str:
    if value is None:
        return "  —  "
    return f"{value:+5.1f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames-dir", required=True, type=Path, help="Folder of frame images.")
    parser.add_argument("--skill-level", default="intermediate")
    parser.add_argument("--location", default="Praia desconhecida")
    parser.add_argument("--wave-size", default="1.0", help="Wave size in metres.")
    parser.add_argument("--board-type", default=None)
    parser.add_argument(
        "--repeats", type=int, default=3, help="No-description runs for the noise floor (>=2)."
    )
    args = parser.parse_args()

    if args.repeats < 2:
        parser.error("--repeats must be >= 2 to estimate a noise floor.")

    settings = get_settings()
    temperature = settings.GEMINI_TEMPERATURE
    frames = load_frames(args.frames_dir)
    context = SurferContext(
        skill_level=args.skill_level,
        location=args.location,
        wave_conditions=f"{args.wave_size} metros",
        board_type=args.board_type,
    )
    gemini = GeminiService()

    print(
        f"Model: {gemini.model_name}  |  temperature: {temperature}  |  "
        f"frames: {len(frames)}  |  baseline repeats: {args.repeats}\n"
    )

    # 1. Baseline: no description, repeated, to get mean + noise floor.
    baseline_runs: list[ScoreRubric] = []
    for i in range(args.repeats):
        print(f"  baseline run {i + 1}/{args.repeats} (no description)...")
        start = time.monotonic()
        scores = run_scoring(gemini, frames, context, None, temperature)
        print(f"    {time.monotonic() - start:.1f}s")
        if scores is not None:
            baseline_runs.append(scores)
    if not baseline_runs:
        raise SystemExit("All baseline runs failed; cannot continue.")

    baseline_mean: dict[str, float | None] = {}
    noise_floor: dict[str, float] = {}
    for dim in DIMENSIONS:
        vals = dim_values(baseline_runs, dim)
        baseline_mean[dim] = round(statistics.mean(vals), 2) if vals else None
        noise_floor[dim] = (max(vals) - min(vals)) if len(vals) >= 2 else 0.0

    # 2. One run per sentiment variant.
    variant_scores: dict[str, ScoreRubric | None] = {}
    for name, text in DESCRIPTIONS.items():
        print(f"  variant '{name}'...")
        start = time.monotonic()
        variant_scores[name] = run_scoring(gemini, frames, context, text, temperature)
        print(f"    {time.monotonic() - start:.1f}s")

    # 3. Report.
    variants = list(DESCRIPTIONS)
    header = f"\n{'dimension':<16}{'base':>7}{'noise':>8}" + "".join(f"{v:>13}" for v in variants)
    print(header)
    print("-" * len(header))

    directional_hits: list[str] = []
    significant_hits: list[str] = []
    for dim in DIMENSIONS:
        base = baseline_mean[dim]
        row = f"{dim:<16}{fmt(base):>7}{noise_floor[dim]:>8.1f}"
        deltas: dict[str, float | None] = {}
        for name in variants:
            sc = variant_scores[name]
            val = getattr(sc, dim) if sc is not None else None
            delta = (val - base) if (val is not None and base is not None) else None
            deltas[name] = delta
            threshold = max(noise_floor[dim], EPSILON)
            flag = "*" if (delta is not None and abs(delta) > threshold) else " "
            row += f"{fmt_delta(delta):>12}{flag}"
        print(row)

        # Significant if any sentiment variant exceeds the per-dimension threshold.
        threshold = max(noise_floor[dim], EPSILON)
        if any(d is not None and abs(d) > threshold for d in deltas.values()):
            significant_hits.append(dim)
        # Directional bias: optimism raises, self-criticism lowers the same score.
        opt, crit = deltas.get("optimistic"), deltas.get("self_critical")
        if opt is not None and crit is not None and opt > threshold and crit < -threshold:
            directional_hits.append(dim)

    # Aggregate drift across the mean of non-null dimensions.
    base_avg_vals = [v for v in baseline_mean.values() if v is not None]
    base_avg = statistics.mean(base_avg_vals) if base_avg_vals else None
    print("-" * len(header))
    avg_row = f"{'AVG(non-null)':<16}{fmt(base_avg):>7}{'':>8}"
    for name in variants:
        sc = variant_scores[name]
        vv = [getattr(sc, d) for d in DIMENSIONS if sc and getattr(sc, d) is not None] if sc else []
        avg = statistics.mean(vv) if vv else None
        delta = (avg - base_avg) if (avg is not None and base_avg is not None) else None
        avg_row += f"{fmt_delta(delta):>12} "
    print(avg_row)

    # Verdict.
    print("\n" + "=" * 60)
    print("Legend: '*' = |delta| exceeds max(noise floor, 0.5) for that dimension.")
    if directional_hits:
        verdict = "FAIL"
        detail = (
            "Directional bias detected on: "
            + ", ".join(directional_hits)
            + " (optimism raised scores, self-criticism lowered them). "
            "Do NOT enable SINGLE_PASS_REVIEW."
        )
    elif significant_hits:
        verdict = "REVIEW"
        detail = (
            "Some sentiment deltas exceed the noise floor on: "
            + ", ".join(significant_hits)
            + ", but not directionally. Increase --repeats and re-run before deciding."
        )
    else:
        verdict = "PASS"
        detail = (
            "Sentiment deltas stayed within the noise floor. The description does not "
            "appear to move the scores — SINGLE_PASS_REVIEW looks safe to enable."
        )
    print(f"VERDICT: {verdict}\n{detail}")
    print("=" * 60)
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
