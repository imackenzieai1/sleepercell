"""Per-team roster construction and positional needs.

Inputs
- A DraftState (so we know which picks each roster has made).
- A ProjectionIndex (so we can resolve player_id → name/position).
- A LeagueConfig (for the target starting lineup).

Outputs
- `team_summary(roster_id)` → counts by position, depth chart, hole flags.
- `positional_needs(roster_id)` → per-position need score (0 = full, 1 = critical).
- `league_needs()` → matrix of all rosters × positions, used for opponent-need detection
  and trade-target identification.

Need score formula
- For each starter slot type (QB, RB, WR, TE, FLEX, SF), compute desired depth = starters
  + reasonable bench layer. e.g. QB target depth = 2 in SF (1 starter + 1 backup that
  is itself a flex starter), RB target depth = 4 (2 starters + 2 flex contributors),
  WR target depth = 5 in a 3WR + flex league.
- need = max(0, (target - have) / target).
- The flex/SF slots contribute partial credit to multiple positions.

The thresholds are reasonable defaults — they're not gospel. A future revision can
make them configurable per league.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from .draft_state import DraftState
from .league_config import LeagueConfig
from .projections import PlayerProjection


# Defaults for "comfortable depth" per position in a 12-team SF dynasty.
DEFAULT_DEPTH_TARGETS = {"QB": 2, "RB": 4, "WR": 5, "TE": 2}


@dataclass
class TeamSummary:
    roster_id: int
    counts: dict[str, int]
    depth_targets: dict[str, int]
    needs: dict[str, float]            # 0 = full, 1 = critical
    drafted_players: list[PlayerProjection]


def team_summary(
    state: DraftState,
    roster_id: int,
    projections: dict[str, PlayerProjection],
    *,
    depth_targets: dict[str, int] | None = None,
) -> TeamSummary:
    depth_targets = dict(depth_targets or DEFAULT_DEPTH_TARGETS)
    # Adjust for league config: Superflex pushes QB target higher; TEP pushes TE higher.
    if state.cfg.superflex:
        depth_targets["QB"] = max(depth_targets["QB"], 2)
    else:
        depth_targets["QB"] = min(depth_targets["QB"], 1)
    if state.cfg.te_premium:
        depth_targets["TE"] = max(depth_targets["TE"], 2)

    picks_made = [p for p in state.picks if int(p.get("roster_id") or 0) == roster_id]
    drafted: list[PlayerProjection] = []
    for pk in picks_made:
        pid = str(pk.get("player_id") or "")
        if pid and pid in projections:
            drafted.append(projections[pid])

    counts = Counter([p.position for p in drafted])
    counts_dict = {pos: int(counts.get(pos, 0)) for pos in depth_targets.keys()}
    needs = {
        pos: max(0.0, (depth_targets[pos] - counts_dict[pos]) / depth_targets[pos])
        for pos in depth_targets
    }
    return TeamSummary(
        roster_id=roster_id,
        counts=counts_dict,
        depth_targets=depth_targets,
        needs=needs,
        drafted_players=drafted,
    )


def league_needs(
    state: DraftState,
    projections: dict[str, PlayerProjection],
    *,
    depth_targets: dict[str, int] | None = None,
) -> dict[int, TeamSummary]:
    """Build TeamSummary for every roster in the league."""
    return {
        rid: team_summary(state, rid, projections, depth_targets=depth_targets)
        for rid in state.slot_to_roster.values()
    }


def positional_scarcity(
    state: DraftState,
    projections: dict[str, PlayerProjection],
) -> dict[str, float]:
    """How fast each position is being drafted relative to baseline.

    Baseline assumption: in a 12-team SF, the expected positional split through
    pick N is roughly 30% QB, 25% RB, 35% WR, 10% TE. If QBs are coming off the
    board at 50% of total picks, scarcity is acute.

    Returns a multiplier 1.0 = baseline; >1.0 = above-average draft rate.
    """
    baseline = {"QB": 0.30, "RB": 0.25, "WR": 0.35, "TE": 0.10}
    counts: dict[str, int] = defaultdict(int)
    for pk in state.picks:
        pid = str(pk.get("player_id") or "")
        if pid in projections:
            counts[projections[pid].position] += 1
    total = sum(counts.values()) or 1
    out = {}
    for pos, b in baseline.items():
        actual = counts[pos] / total
        out[pos] = round(actual / b, 2) if b else 1.0
    return out
