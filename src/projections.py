"""Apply a LeagueConfig's scoring to per-stat projections.

This is the core differentiator of Sleeper Cell. Generic dynasty values (KTC,
DynastyProcess, FantasyPros ECR) assume "PPR Superflex" or similar — they cannot
see custom scoring twists like:
  • 6pt passing TDs (vs 4pt default)
  • Completion bonus (+0.25/pass_cmp)
  • First-down bonuses (+0.25/pass_fd, +0.25/rush_fd, +0.25/rec_fd)
  • TE Premium (+1.0 bonus_rec_te)
  • 0.1/rush_att (workhorse RB bonus)
  • RB receiving bonus (+0.25 bonus_rec_rb)

For our test case (Joe Burrow, 2026 season projections):
  • Sleeper-default scoring:  ~308 points
  • Trade Whores scoring:     ~577 points
  • Δ ≈ +269 points/season — about +90% — which generic values cannot represent.

The function below is a pure dot-product. Pass any LeagueConfig and any stats dict
(missing keys are treated as 0) and it returns the league-adjusted total.

ALL stat keys are exactly what Sleeper returns from both `/v1/league/{id}` scoring_settings
AND the bulk projections endpoint. No remapping required — that's the whole reason
we keep the original keys in LeagueConfig.scoring.

Pieces also handled:
  • points-allowed scoring tiers (pts_allow_0, pts_allow_1_6, ...): collapse to one effective
    value via expected-allowed input. (Phase 2; not used for skill-position players.)
  • Bonus stats keyed by special names (bonus_rec_rb, bonus_rec_te, bonus_pass_yd_300, etc.)
    are applied as standard scoring keys against the player's stats. We don't compute the
    bonus eligibility (e.g. games where a QB hit 300 yards) — Sleeper's projections already
    expose those derived stats; if missing we treat as 0.
"""
from __future__ import annotations

from dataclasses import dataclass

from .league_config import LeagueConfig


# Stats we ignore when applying scoring (they're not scoring inputs). Sleeper's projections
# endpoint also returns ADP-related keys that look like stats; filter them.
NON_SCORING_PREFIXES = ("adp_", "cmp_pct", "gp")


def score_stats(stats: dict[str, float], cfg: LeagueConfig) -> float:
    """Compute season fantasy points for one player using cfg's scoring.

    Args:
        stats: dict from the projections endpoint (`item['stats']`).
        cfg:   a LeagueConfig.

    Returns:
        Total projected season fantasy points for this league.
    """
    if not stats:
        return 0.0
    total = 0.0
    for stat_key, multiplier in cfg.scoring.items():
        if multiplier == 0:
            continue
        if any(stat_key.startswith(p) for p in NON_SCORING_PREFIXES):
            continue
        value = stats.get(stat_key)
        if value is None:
            continue
        total += float(value) * float(multiplier)
    return round(total, 3)


# ---------------------------------------------------------------------------
# Convenience: batch + extract


@dataclass
class PlayerProjection:
    """Normalized projection record used by the rest of the app."""

    player_id: str
    name: str
    position: str
    team: str | None
    age: int | None
    years_exp: int | None
    stats: dict[str, float]
    league_points: float          # season points under THIS league's scoring
    sleeper_pts_ppr: float        # for sanity-check / fallback display
    last_modified_ms: int | None
    company: str | None           # which projection provider Sleeper is sourcing


def build_projection_index(
    season_projections: list[dict],
    cfg: LeagueConfig,
    *,
    positions: tuple[str, ...] = ("QB", "RB", "WR", "TE"),
) -> dict[str, PlayerProjection]:
    """Index the bulk projections payload by player_id, applying league scoring.

    Args:
        season_projections: list returned by SleeperClient.get_season_projections.
        cfg: league config (for scoring).
        positions: which positions to keep. Phase 1 = skill positions only.

    Returns:
        dict mapping player_id -> PlayerProjection.
    """
    out: dict[str, PlayerProjection] = {}
    for item in season_projections:
        if not isinstance(item, dict):
            continue
        player = item.get("player") or {}
        position = player.get("position")
        if position not in positions:
            continue
        stats = item.get("stats") or {}
        pid = str(item.get("player_id") or "")
        if not pid:
            continue

        league_pts = score_stats(stats, cfg)
        full_name = (
            (player.get("first_name") or "").strip()
            + " "
            + (player.get("last_name") or "").strip()
        ).strip() or pid

        out[pid] = PlayerProjection(
            player_id=pid,
            name=full_name,
            position=position,
            team=player.get("team"),
            age=player.get("age"),
            years_exp=player.get("years_exp"),
            stats=stats,
            league_points=league_pts,
            sleeper_pts_ppr=float(stats.get("pts_ppr") or 0.0),
            last_modified_ms=item.get("last_modified") or item.get("updated_at"),
            company=item.get("company"),
        )
    return out


def points_per_game(proj: PlayerProjection) -> float:
    """Convert season points to PPG using the projection's reported games-played."""
    gp = float(proj.stats.get("gp") or 0)
    return round(proj.league_points / gp, 2) if gp > 0 else 0.0


def explain_scoring_components(stats: dict[str, float], cfg: LeagueConfig, *, top_n: int = 8) -> list[tuple[str, float]]:
    """Return the top-N scoring contributions for one player.

    Useful for the UI: when someone asks "why is Burrow worth 577 pts?" we surface:
        pass_yd*0.04 = 160.3
        pass_cmp*0.25 = 94.3
        pass_fd*0.25 = 100.2
        pass_td*6 = 198.0
        ...
    """
    contribs: list[tuple[str, float]] = []
    for stat_key, multiplier in cfg.scoring.items():
        if multiplier == 0:
            continue
        if any(stat_key.startswith(p) for p in NON_SCORING_PREFIXES):
            continue
        value = stats.get(stat_key)
        if not value:
            continue
        contribs.append((stat_key, round(float(value) * float(multiplier), 2)))
    contribs.sort(key=lambda x: -abs(x[1]))
    return contribs[:top_n]
