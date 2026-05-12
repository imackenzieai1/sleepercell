"""Composite player value: projections × scoring × dynasty curve.

This is the module the rest of the app asks "what is this player worth?". It binds
together:
  • per-stat projections (from src/projections.py)
  • LeagueConfig scoring (already applied in projections.build_projection_index)
  • Multi-year dynasty curve (from src/dynasty_curve.py)
  • PlayerIndex records (age + DP cross-walk for sanity-checking)

Output: a ValuedPlayer with:
  - season_points (this league's scoring, this season)
  - dynasty_value (strategy-weighted, age-decayed, horizon-summed)
  - overall_rank / position_rank (computed across the input population)
  - replacement_delta — value above the positional replacement player. The "VBD"
    in classic value-based drafting.

Why the rank is computed inside this module:
- All downstream consumers (UI, recommendation engine, tier engine) want consistent
  league-relative ranks. Computing them once here avoids drift.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .dynasty_curve import discounted_dynasty_value
from .league_config import LeagueConfig
from .player_index import PlayerIndex, PlayerRecord
from .projections import PlayerProjection


@dataclass
class ValuedPlayer:
    player_id: str
    name: str
    position: str
    team: str | None
    age: int | None
    season_points: float
    dynasty_value: float
    overall_rank: int
    position_rank: int
    # Replacement-level math
    replacement_delta: float = 0.0
    # Sanity-check fields
    sleeper_pts_ppr: float | None = None
    dp_value_2qb: float | None = None
    dp_value_1qb: float | None = None
    # KTC / FantasyCalc overlay (optional, populated when a community-values overlay is loaded)
    ktc_value: float | None = None
    league_fit: float | None = None      # 1.0 = position-median; >1 = above-median TW fit
    adjusted_ktc: float | None = None    # ktc_value × league_fit
    # Common-size percentile ranks (0-100), computed in value_population() across the population.
    # Same number means same thing across metrics: e.g. 99 = top 1% by that metric.
    pct_dynasty: float | None = None
    pct_vbd: float | None = None
    pct_community: float | None = None
    pct_adj_community: float | None = None
    match_method: str = "unmatched"


# Replacement-level slot counts: roughly where "starters end and bench begins" given
# a 12-team league. For Superflex we treat QB starter pool as larger.
DEFAULT_REPLACEMENT_RANKS = {
    "QB": 24,   # 12 teams × 1 QB + most SF spots used for QBs
    "RB": 36,   # 12 × ~3 (2 RB + heavy FLEX usage)
    "WR": 48,   # 12 × ~4
    "TE": 18,   # 12 × ~1.5 (only top-tier TEs out-score top WRs in TEP)
}


def compute_league_fit(
    projections: dict[str, PlayerProjection],
    *,
    min_baseline_pts: float = 30.0,
) -> dict[str, float]:
    """Per-position-normalized league fit = (TW pts / default PPR) ÷ position median.

    1.0  → average TW fit for the position
    >1.0 → this league's scoring rewards this player more than the median peer
    <1.0 → less than median fit (e.g., low-volume QB in a completion-bonus league)

    Used as a multiplier on KTC values: KTC × fit = adjusted-KTC.
    """
    from collections import defaultdict

    by_pos_fits: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for pid, proj in projections.items():
        std = float(proj.sleeper_pts_ppr or 0)
        if std < min_baseline_pts or proj.league_points <= 0:
            continue
        fit = proj.league_points / std
        by_pos_fits[proj.position].append((pid, fit))

    out: dict[str, float] = {}
    for pos, lst in by_pos_fits.items():
        fits_only = sorted(f for _, f in lst)
        if not fits_only:
            continue
        median = fits_only[len(fits_only) // 2] or 1.0
        for pid, fit in lst:
            out[pid] = round(fit / median, 4)
    return out


def value_population(
    projections: dict[str, PlayerProjection],
    index: PlayerIndex,
    cfg: LeagueConfig,
    *,
    replacement_ranks: dict[str, int] | None = None,
    ktc_values: dict[str, float] | None = None,
) -> list[ValuedPlayer]:
    """Score and rank every player with a projection.

    Args:
        projections: from `build_projection_index`. player_id -> PlayerProjection.
        index: a built PlayerIndex (for age + DP cross-walk).
        cfg: a LeagueConfig (strategy + horizon).
        replacement_ranks: overrides per-position. Default: DEFAULT_REPLACEMENT_RANKS,
            scaled by league size if not exactly 12 teams.

    Returns:
        List of ValuedPlayer, sorted by dynasty_value descending.
    """
    replacement_ranks = replacement_ranks or scaled_replacement_ranks(cfg.teams)

    # Adjust SF to upweight QB pool size when Superflex is enabled
    if cfg.superflex:
        replacement_ranks = {**replacement_ranks, "QB": max(replacement_ranks["QB"], 24)}
    else:
        replacement_ranks = {**replacement_ranks, "QB": min(replacement_ranks["QB"], 14)}

    # Pre-compute league-fit per player (used to adjust KTC values)
    league_fits = compute_league_fit(projections) if ktc_values else {}

    # 1) Build initial valued list (no ranks yet)
    interim: list[ValuedPlayer] = []
    for pid, proj in projections.items():
        record: PlayerRecord | None = index.resolve(pid)
        age = (record.age if record else None) or proj.age
        dyn_value = discounted_dynasty_value(
            season_points=proj.league_points,
            position=proj.position,
            age=age,
            strategy=cfg.strategy,
            horizon_years=cfg.age_horizon_years,
        )
        ktc_v = (ktc_values or {}).get(pid)
        fit_v = league_fits.get(pid) if ktc_v else None
        adj_ktc = round(ktc_v * fit_v, 1) if (ktc_v and fit_v) else None
        interim.append(
            ValuedPlayer(
                player_id=pid,
                name=proj.name,
                position=proj.position,
                team=proj.team,
                age=age,
                season_points=proj.league_points,
                dynasty_value=dyn_value,
                overall_rank=0,
                position_rank=0,
                sleeper_pts_ppr=proj.sleeper_pts_ppr,
                dp_value_2qb=(record.dp_value_2qb if record else None),
                dp_value_1qb=(record.dp_value_1qb if record else None),
                ktc_value=ktc_v,
                league_fit=fit_v,
                adjusted_ktc=adj_ktc,
                match_method=(record.match_method if record else "unmatched"),
            )
        )

    # 2) Sort by dynasty value and assign overall + positional ranks
    interim.sort(key=lambda p: -p.dynasty_value)
    pos_counters: dict[str, int] = {}
    for i, vp in enumerate(interim, start=1):
        vp.overall_rank = i
        pos_counters[vp.position] = pos_counters.get(vp.position, 0) + 1
        vp.position_rank = pos_counters[vp.position]

    # 3) Compute replacement-level VBD per position
    replacement_values: dict[str, float] = {}
    for pos, k in replacement_ranks.items():
        same_pos = [p for p in interim if p.position == pos]
        if len(same_pos) >= k:
            replacement_values[pos] = same_pos[k - 1].dynasty_value
        elif same_pos:
            replacement_values[pos] = same_pos[-1].dynasty_value
        else:
            replacement_values[pos] = 0.0
    for vp in interim:
        rv = replacement_values.get(vp.position, 0.0)
        vp.replacement_delta = round(vp.dynasty_value - rv, 3)

    # 4) Common-size: compute 0-100 percentile rank per metric.
    # Each player's pct_X tells you "this player is in the top (100-X)% of all skill players
    # ranked by that metric." Same number means the same thing across every column in the UI.
    from .normalize import attach_percentiles
    attach_percentiles(
        interim,
        attr_pairs=[
            ("dynasty_value",     "pct_dynasty"),
            ("replacement_delta", "pct_vbd"),
            ("ktc_value",         "pct_community"),
            ("adjusted_ktc",      "pct_adj_community"),
        ],
    )

    return interim


def scaled_replacement_ranks(teams: int) -> dict[str, int]:
    """Scale the defaults (which assume 12 teams) to the actual league size."""
    factor = teams / 12.0
    return {pos: max(1, int(round(k * factor))) for pos, k in DEFAULT_REPLACEMENT_RANKS.items()}


def filter_undrafted(
    valued: Iterable[ValuedPlayer],
    drafted_ids: set[str],
) -> list[ValuedPlayer]:
    """Convenience: drop already-drafted players."""
    return [v for v in valued if v.player_id not in drafted_ids]
