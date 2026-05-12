"""Multi-year value with age decay.

For dynasty leagues, a 27-year-old RB and a 22-year-old RB projecting the same season
have *very* different values. The 22-year-old has 3–4 more peak-production seasons
ahead; the 27-year-old has 1, maybe 2.

Approach
- Per-position age curves: peak age, plateau width, decay slope.
- Strategy lens (Compete/Balanced/Rebuild) multiplies each future year's weight.
- Output: a single "dynasty value" that's the discounted sum of expected league points
  over the user's age_horizon_years.

Notes on the curves
- These are practitioner consensus estimates, not regression outputs. Phase 2 idea:
  fit them from nflfastR + historical Sleeper projections so they're explicitly
  league-aware. For Phase 1, hard-coded defaults are fine.
- The curves return MULTIPLIERS on next year's projection, year by year. We don't
  re-project per year — we apply a decay factor to the current season's projection.
- This is fast (no per-year stat re-roll) and good enough for ranking. We can
  upgrade to per-year stat sims later without changing the interface.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgeCurve:
    """One position's age curve.

    multiplier(age) returns a 0–1+ scalar applied to the player's projection.
    The curve is piecewise-linear:
        - Constant 1.0 between [peak_start, peak_end]
        - Linear rise from `early_age` (multiplier=`early_mult`) up to peak_start
        - Linear decline from peak_end down to `late_age` (multiplier=`late_mult`)
        - After late_age, decays at `late_decay` per year (clipped to 0)
    """

    early_age: int
    early_mult: float
    peak_start: int
    peak_end: int
    late_age: int
    late_mult: float
    late_decay: float

    def multiplier(self, age: int | None) -> float:
        if age is None:
            return 1.0  # unknown age = treat as peak (safer than penalizing)
        if age <= self.early_age:
            return self.early_mult
        if age >= self.late_age:
            extra = age - self.late_age
            return max(0.0, self.late_mult - extra * self.late_decay)
        if self.peak_start <= age <= self.peak_end:
            return 1.0
        if age < self.peak_start:
            span = self.peak_start - self.early_age
            frac = (age - self.early_age) / span if span > 0 else 1.0
            return self.early_mult + frac * (1.0 - self.early_mult)
        # peak_end < age < late_age
        span = self.late_age - self.peak_end
        frac = (age - self.peak_end) / span if span > 0 else 1.0
        return 1.0 + frac * (self.late_mult - 1.0)


# Defaults are calibrated against real dynasty markets (KTC, KeepTradeCut SF rankings):
#   - Peak STARTS at the age when a young pedigree starter is producing now, not when
#     they "fully develop." A 22yo starter is at peak; we don't penalize for being young.
#   - Pre-peak discount applies only to truly raw/developmental players (≤21).
#   - RB decay is the steepest (cliff at ~28). QB is the flattest. WR/TE in between.
DEFAULT_CURVES: dict[str, AgeCurve] = {
    "QB": AgeCurve(early_age=20, early_mult=0.95, peak_start=22, peak_end=33, late_age=35, late_mult=0.85, late_decay=0.06),
    "RB": AgeCurve(early_age=20, early_mult=0.92, peak_start=22, peak_end=26, late_age=28, late_mult=0.70, late_decay=0.15),
    "WR": AgeCurve(early_age=20, early_mult=0.92, peak_start=22, peak_end=29, late_age=31, late_mult=0.80, late_decay=0.10),
    "TE": AgeCurve(early_age=21, early_mult=0.90, peak_start=23, peak_end=30, late_age=32, late_mult=0.80, late_decay=0.10),
}


# Strategy weights: how much each future year contributes to dynasty value.
# - Compete:   prioritizes near-term production. Year-1 = 1.0, year-N decays steeply.
# - Balanced:  steady decay.
# - Rebuild:   discounts year-1 (you're not winning this year), values years 2-N.
STRATEGY_WEIGHTS: dict[str, list[float]] = {
    "compete":  [1.00, 0.65, 0.35, 0.15, 0.05, 0.00, 0.00, 0.00],
    "balanced": [1.00, 0.80, 0.65, 0.50, 0.35, 0.20, 0.10, 0.05],
    "rebuild":  [0.60, 0.95, 0.90, 0.75, 0.55, 0.35, 0.20, 0.10],
}


def discounted_dynasty_value(
    season_points: float,
    position: str,
    age: int | None,
    *,
    strategy: str = "balanced",
    horizon_years: int = 4,
    curves: dict[str, AgeCurve] | None = None,
) -> float:
    """Multi-year dynasty value for one player.

    Args:
        season_points: this season's projected league-adjusted points.
        position: 'QB' | 'RB' | 'WR' | 'TE'.
        age: current age. None = treat as peak.
        strategy: 'compete' | 'balanced' | 'rebuild'.
        horizon_years: how many seasons to project forward.
        curves: override the default age curves.

    Returns:
        A weighted, age-adjusted sum of expected points across the horizon.
    """
    return sum(c["points"] for c in dynasty_value_breakdown(
        season_points, position, age,
        strategy=strategy, horizon_years=horizon_years, curves=curves,
    ))


def dynasty_value_breakdown(
    season_points: float,
    position: str,
    age: int | None,
    *,
    strategy: str = "balanced",
    horizon_years: int = 4,
    curves: dict[str, AgeCurve] | None = None,
) -> list[dict]:
    """Year-by-year decomposition of a player's dynasty value.

    Returns a list of dicts, one per horizon year:
        [
            {'year': 0, 'age': 29, 'age_mult': 1.0, 'strategy_weight': 1.0, 'points': 612.0},
            {'year': 1, 'age': 30, 'age_mult': 1.0, 'strategy_weight': 0.65, 'points': 397.8},
            ...
        ]

    Used by the UI's "Why?" tab to show exactly how a player's dynasty value was built.
    Round-tripped: `discounted_dynasty_value(...)` equals `sum(c['points'] for c in breakdown(...))`.
    """
    curves = curves or DEFAULT_CURVES
    weights = STRATEGY_WEIGHTS.get(strategy, STRATEGY_WEIGHTS["balanced"])
    horizon = max(1, min(horizon_years, len(weights)))
    curve = curves.get(position)

    if curve is None:
        return [{"year": 0, "age": age, "age_mult": 1.0, "strategy_weight": 1.0, "points": round(season_points, 2)}]

    rows: list[dict] = []
    a = age
    for y in range(horizon):
        mult = curve.multiplier(a)
        pts = season_points * mult * weights[y]
        rows.append({
            "year": y,
            "age": a,
            "age_mult": round(mult, 3),
            "strategy_weight": round(weights[y], 3),
            "points": round(pts, 2),
        })
        if a is not None:
            a += 1
    return rows
