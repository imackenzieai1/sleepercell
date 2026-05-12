"""Future-pick valuation.

Converting a "2027 R2" pick into a player-equivalent value is non-trivial:
  - We don't have 2027 rookie projections (those players are CFB right now).
  - DynastyProcess's `values.csv` carries explicit PICK rows like "2027 1st", "2027 2nd",
    etc. with both value_1qb and value_2qb. That's the market price.
  - But the market price is in DP's units (~0–10000 scale). Our valuation pipeline
    produces TW values in "dynasty horizon points" (~0–1500 scale). We need a
    consistent way to compare picks AND players on both scales.

Strategy
--------
- "Consensus value" of a pick = DP's value_2qb (Superflex) or value_1qb (1QB) from
  the row matching the season+round.
- "TW (our) value" of a pick is harder. We approximate as:

      pick_tw_value = pick_dp_value * tw_to_dp_scale_factor

  where the scale factor is calibrated from CURRENT players. For each known player
  we have both TW value and DP value_2qb. The median ratio across the top-50
  players gives us a stable conversion.

  We further apply a small **uncertainty discount** to picks because they're a
  bet on an unknown future class. Default discount: 10 % per year out (so a 2028
  pick gets a 20 % discount over a 2026 player).

- For Trade Whores specifically, we additionally apply a **format premium**:
  - 2027 R1: light QB premium (+10 %) because every 2027 R1 is a future
    SF-quality QB swing.

Limitations / Phase 2 work
- Doesn't know about specific 2027 rookie prospects. When 2027 prospects start
  earning ADP attention, we'd switch to projecting them via the same per-stat
  pipeline as current players.
- Class-strength deltas (2027 considered "average", 2026 "deep") aren't priced.
  Could be added as a per-year multiplier later.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from .league_config import LeagueConfig
from .valuation import ValuedPlayer


# Words used by DP for round labels in values.csv player column.
_ROUND_WORDS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th", 6: "6th"}


@dataclass(frozen=True)
class PickValue:
    season: str
    round: int
    consensus_value: float    # DP value_2qb (or value_1qb if not SF)
    tw_value: float           # our scale (player-comparable)
    notes: str = ""


@dataclass(frozen=True)
class PickValueModel:
    """Pre-built table: (season, round) → PickValue."""

    cfg: LeagueConfig
    table: dict[tuple[str, int], PickValue]
    tw_to_dp_scale: float
    """Calibration constant: tw_value ≈ dp_value × this scale."""

    def get(self, season: str, round_no: int) -> PickValue | None:
        return self.table.get((str(season), int(round_no)))


def _calibrate_tw_dp_scale(valued: Iterable[ValuedPlayer]) -> float:
    """Median ratio (TW dyn_value) / (DP value_2qb) across players we have both for.

    Only uses the top ~50 players to avoid noisy zero-value tails. Returns a scalar
    that, when multiplied by a DP value, gives an approximate TW-equivalent.
    """
    pairs: list[tuple[float, float]] = []
    for v in valued:
        dp = v.dp_value_2qb if v.dp_value_2qb else v.dp_value_1qb
        if dp and dp > 100 and v.dynasty_value > 0:
            pairs.append((v.dynasty_value, dp))
        if len(pairs) >= 50:
            break
    if not pairs:
        # No anchor — pick something reasonable so the math doesn't crash.
        return 0.12
    ratios = sorted(t / d for t, d in pairs)
    return ratios[len(ratios) // 2]


def build_pick_value_model(
    valued: list[ValuedPlayer],
    dp_values_df: pd.DataFrame,
    cfg: LeagueConfig,
    *,
    uncertainty_discount_per_year: float = 0.10,
    current_season: int | None = None,
    qb_premium_2027_r1: float = 0.10,
) -> PickValueModel:
    """Build a (season, round) → PickValue lookup for all future drafts in DP.

    Args:
        valued: full ranked player list. Used to calibrate TW↔DP scale.
        dp_values_df: pandas DataFrame from DataLayer.values() — includes PICK rows.
        cfg: LeagueConfig.
        uncertainty_discount_per_year: how much each year-out reduces a pick's TW value.
        current_season: defaults to cfg.season as int.
        qb_premium_2027_r1: bump applied to 2027 R1 in Superflex (QB swing potential).

    Returns:
        PickValueModel.
    """
    season_now = current_season or int(cfg.season or 2026)
    sf = cfg.superflex
    value_col = "value_2qb" if sf else "value_1qb"

    scale = _calibrate_tw_dp_scale(valued)

    # Filter DP values to PICK rows
    picks_df = dp_values_df[dp_values_df["pos"] == "PICK"].copy()
    table: dict[tuple[str, int], PickValue] = {}

    for _, row in picks_df.iterrows():
        label = str(row.get("player") or "").strip()
        # Two label formats we care about:
        #   "2026 Pick 1.01" — current-year specific slot; we collapse to round 1
        #   "2027 1st"       — future-year round bucket
        season_token, round_token = _parse_pick_label(label)
        if not season_token or not round_token:
            continue
        # For "2026 Pick 1.NN" we only keep the *round-level* aggregate; build later.
        if "Pick" in label:
            continue

        try:
            year = int(season_token)
            r = int(round_token)
        except (TypeError, ValueError):
            continue

        dp_value = float(row.get(value_col) or 0.0)
        years_out = max(0, year - season_now)
        uncertainty = (1.0 - uncertainty_discount_per_year) ** years_out
        tw_value = dp_value * scale * uncertainty

        notes_bits: list[str] = []
        if uncertainty < 1.0:
            notes_bits.append(f"−{int((1 - uncertainty) * 100)}% uncertainty ({years_out}y out)")

        # SF QB premium on 2027 R1
        if sf and year == 2027 and r == 1 and qb_premium_2027_r1:
            tw_value *= (1 + qb_premium_2027_r1)
            notes_bits.append(f"+{int(qb_premium_2027_r1 * 100)}% SF QB swing")

        table[(str(year), r)] = PickValue(
            season=str(year),
            round=r,
            consensus_value=round(dp_value, 1),
            tw_value=round(tw_value, 1),
            notes=" · ".join(notes_bits),
        )

    return PickValueModel(cfg=cfg, table=table, tw_to_dp_scale=scale)


def _parse_pick_label(label: str) -> tuple[str | None, str | None]:
    """Extract (season, round) from DP's player column.

    Accepts: "2027 1st", "2027 2nd", "2027 3rd", "2027 4th", etc.
    Returns (None, None) for labels we can't parse.
    """
    parts = label.split()
    if len(parts) < 2:
        return None, None
    year, *rest = parts
    if not year.isdigit():
        return None, None
    word = rest[-1].lower()
    for n, w in _ROUND_WORDS.items():
        if word == w:
            return year, str(n)
    return None, None
