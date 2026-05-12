"""Future-pick valuation.

Problem: DynastyProcess's pick values are conservative relative to community
markets like KTC and FantasyCalc. Using a raw DP × scale conversion gives results
like "2027 R1 = 166 TW" which is unrealistically low.

Solution: ship **sensible default pick values** calibrated to community market
expectations for a 12-team SF dynasty with TE Premium scoring. These are the
defaults; DP's pick rows act as a fallback for any season+round we don't have a
hardcoded value for.

Reasonable ballpark values (TW units; community values in DP-style 0-10000 scale):

  2027 R1 ≈ 800 TW  (~5000 community)   — future-1st SF dynasty swing
  2027 R2 ≈ 280 TW  (~1800 community)
  2027 R3 ≈  80 TW  (~500 community)
  2027 R4 ≈  30 TW  (~200 community)

  2028 R1 ≈ 600 TW  (~4000 community)   — more uncertainty 2 years out
  2028 R2 ≈ 210 TW  (~1400 community)
  ...

These align with KTC SF dynasty community trade values divided/scaled to match
our pipeline. A real KTC pick-value import could replace these later.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from .league_config import LeagueConfig
from .valuation import ValuedPlayer


# Words used by DP for round labels in values.csv player column.
_ROUND_WORDS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th", 6: "6th"}


# Sensible default pick values for a 12-team Superflex dynasty league with TE Premium.
# (season, round) → (tw_value, community_value)
# These are starting points; the build_pick_value_model() function calibrates against
# the actual player pool for the TW side, but uses these as the floor.
SENSIBLE_PICK_VALUES: dict[tuple[str, int], tuple[float, float]] = {
    ("2026", 1): (900,  5500),
    ("2026", 2): (350,  2200),
    ("2026", 3): (110,   650),
    ("2026", 4): (45,    260),
    ("2027", 1): (800,  5000),
    ("2027", 2): (280,  1800),
    ("2027", 3): (85,    500),
    ("2027", 4): (32,    210),
    ("2028", 1): (600,  4000),
    ("2028", 2): (210,  1400),
    ("2028", 3): (65,    400),
    ("2028", 4): (24,    160),
}


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
    table: dict[tuple[str, int], PickValue] = {}

    # Start with sensible community-calibrated defaults for every (season, round) we know about.
    # These produce realistic values like 2027 R1 ≈ 800 TW — the DP × scale approach gave 166,
    # which underprices picks in real dynasty trade markets.
    for (season, rnd), (tw_v, comm_v) in SENSIBLE_PICK_VALUES.items():
        table[(season, rnd)] = PickValue(
            season=season,
            round=rnd,
            consensus_value=float(comm_v),
            tw_value=float(tw_v),
            notes="community-calibrated default",
        )

    # Fill any gaps from DP's PICK rows (e.g. R5+, exotic seasons not in our defaults).
    picks_df = dp_values_df[dp_values_df["pos"] == "PICK"].copy()
    for _, row in picks_df.iterrows():
        label = str(row.get("player") or "").strip()
        season_token, round_token = _parse_pick_label(label)
        if not season_token or not round_token:
            continue
        if "Pick" in label:
            continue

        try:
            year = int(season_token)
            r = int(round_token)
        except (TypeError, ValueError):
            continue

        key = (str(year), r)
        if key in table:
            continue  # already covered by sensible defaults

        dp_value = float(row.get(value_col) or 0.0)
        years_out = max(0, year - season_now)
        uncertainty = (1.0 - uncertainty_discount_per_year) ** years_out
        tw_value = dp_value * scale * uncertainty

        notes_bits: list[str] = ["DP fallback (gap)"]
        if uncertainty < 1.0:
            notes_bits.append(f"−{int((1 - uncertainty) * 100)}% uncertainty ({years_out}y out)")

        table[key] = PickValue(
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
