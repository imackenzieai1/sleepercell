"""Value-Based Drafting (VBD) — context-aware best available.

Why this exists
- valuation.py already computes `replacement_delta` (player_value − positional replacement).
- But the BEST decision for YOUR pick depends on whether you can wait for that
  position. If you're picking 25th and your next pick isn't until 48, you might
  prefer the player at 27 (RB1) over the player at 25 (third-tier WR) because the
  RB will not be there at 48 — i.e. the "opportunity cost" of not taking RB now is
  higher than the absolute value gap.

This module computes:
  • likely_next_available_by_pos: for each position, the value of the best player
    likely still on the board at your NEXT pick (given pick distance and league
    positional draft rate).
  • dynamic_vbd: player.dynasty_value − likely_next_available_at_position.

When `likely_next_available > player.dynasty_value`, dynamic_vbd is negative —
meaning you should consider waiting on that position.

Heuristics
- "Likely available at pick N" uses a probabilistic position-draft-rate model:
  given current positional scarcity (from team_analysis.positional_scarcity), what's
  the expected count of additional picks at each position before our next pick?
- The "best still available" is then the player at rank (current_pos_rank + expected_count).

This is intentionally a simple model — Phase 2 can replace it with simulation. The
key contract is the function signature: given (valued, current_pick, my_next_pick,
scarcity), return a per-position likely-available value.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .valuation import ValuedPlayer


@dataclass
class VBDContext:
    """Inputs to the VBD calculation."""

    valued_undrafted: list[ValuedPlayer]
    """Players still on the board, sorted by dynasty_value desc."""

    picks_until_my_next: int
    """How many picks happen before my next pick. 0 = I'm on the clock."""

    positional_scarcity: dict[str, float]
    """Multiplier per position; >1.0 = drafted above baseline rate."""

    # Baseline expected position split (12-team SF, dynasty heuristic)
    baseline_split: dict[str, float] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.baseline_split is None:
            self.baseline_split = {"QB": 0.30, "RB": 0.25, "WR": 0.35, "TE": 0.10}


def expected_picks_at_pos_before_my_next(ctx: VBDContext) -> dict[str, int]:
    """Estimate how many additional players at each position get drafted before my next pick."""
    out: dict[str, int] = {}
    for pos, baseline in ctx.baseline_split.items():
        scarcity = ctx.positional_scarcity.get(pos, 1.0)
        # Cap the multiplier so a freak run doesn't make us think every pick is the same position.
        scarcity = max(0.4, min(scarcity, 2.0))
        expected_share = baseline * scarcity
        out[pos] = max(0, round(expected_share * ctx.picks_until_my_next))
    return out


def likely_next_available_value(ctx: VBDContext) -> dict[str, float]:
    """For each position, the dynasty_value of the player likely still on the board at my next pick."""
    # Group undrafted by position
    by_pos: dict[str, list[ValuedPlayer]] = defaultdict(list)
    for vp in ctx.valued_undrafted:
        by_pos[vp.position].append(vp)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda p: -p.dynasty_value)

    expected = expected_picks_at_pos_before_my_next(ctx)

    out: dict[str, float] = {}
    for pos, players in by_pos.items():
        # We expect `expected[pos]` more players at this position to be drafted before our next pick.
        # The 0-indexed best-still-available is the player at index `expected[pos]`.
        idx = expected.get(pos, 0)
        if idx < len(players):
            out[pos] = players[idx].dynasty_value
        elif players:
            out[pos] = players[-1].dynasty_value
        else:
            out[pos] = 0.0
    return out


def dynamic_vbd(ctx: VBDContext) -> dict[str, float]:
    """For each undrafted player, value above the position's likely-next-available player.

    Positive = take now, you can't wait.
    Negative = you can wait; better positions exist this pick.

    Returns: {player_id: dynamic_vbd}
    """
    likely = likely_next_available_value(ctx)
    return {
        vp.player_id: round(vp.dynasty_value - likely.get(vp.position, 0.0), 3)
        for vp in ctx.valued_undrafted
    }


def best_available_dynamic(ctx: VBDContext, *, top_n: int = 10) -> list[ValuedPlayer]:
    """Rank undrafted players by dynamic_vbd (descending) and return the top N."""
    deltas = dynamic_vbd(ctx)
    sorted_by_delta = sorted(
        ctx.valued_undrafted,
        key=lambda vp: -deltas.get(vp.player_id, vp.replacement_delta),
    )
    return sorted_by_delta[:top_n]
