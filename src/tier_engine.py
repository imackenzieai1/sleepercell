"""Algorithmic per-position tier detection.

Inputs
- A list of ValuedPlayer.
- An optional `value_fn` callable extracting the metric to tier on (defaults to
  dynasty_value). Letting the caller pick the metric lets us compare tier shapes
  under different value systems (our model vs community vs VBD vs Dyn VBD).

Algorithm
1. Sort players within position by the chosen value descending.
2. Walk down the list, opening a new tier when the gap to the previous player exceeds
   k × (in-tier mean gap), where k is configurable (default 1.5).
3. Cap tier counts per position so we don't end up with 17 tiers — collapse the long
   tail into a final "depth" tier.

Tier-cliff alert
- A tier is "cliffed" when ≤N players from that tier remain undrafted (default 2).
- The UI surfaces these as red badges next to position cards.

Why it's its own module
- Tiers are derivative of values, not source-of-truth. They should be recomputed when
  the underlying valuation changes (e.g. strategy switch). Keeping them stateless
  keeps the contract simple.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Callable

from .valuation import ValuedPlayer


# Default value accessor — what we tier on if nothing else is passed.
def _default_value(p: ValuedPlayer) -> float:
    return float(p.dynasty_value or 0)


@dataclass
class Tier:
    position: str
    tier_number: int   # 1 = elite, increasing
    players: list[ValuedPlayer]

    @property
    def size(self) -> int:
        return len(self.players)

    @property
    def value_floor(self) -> float:
        return min(p.dynasty_value for p in self.players) if self.players else 0.0

    @property
    def value_ceiling(self) -> float:
        return max(p.dynasty_value for p in self.players) if self.players else 0.0


@dataclass
class TierCliffAlert:
    position: str
    tier_number: int
    remaining: int
    next_pick_distance: int | None = None  # if known; informational for UI
    severity: str = "watch"  # "watch" | "imminent" | "critical"


def detect_tiers(
    valued: list[ValuedPlayer],
    *,
    gap_multiplier: float = 1.5,
    max_tiers_per_pos: int = 8,
    min_tier_size: int = 1,
    value_fn: Callable[[ValuedPlayer], float] | None = None,
) -> dict[str, list[Tier]]:
    """Group players into tiers by position based on value gaps.

    Args:
        valued: list of ValuedPlayer.
        gap_multiplier: how much bigger than the in-tier mean gap a break must be.
        max_tiers_per_pos: cap on number of tiers per position.
        min_tier_size: minimum players a tier must have before a cliff can open.
        value_fn: optional accessor → float for the value being tiered on. Defaults to
            dynasty_value. Use e.g. `lambda p: p.ktc_value or 0` to tier on community
            values, or pass a closure over a dict for dynamic-VBD-tiered views.

    Returns: {'QB': [Tier1, Tier2, ...], 'RB': [...], ...}
    Players whose value_fn returns ≤0 are excluded (they'd produce zero-gap noise).
    """
    vfn = value_fn or _default_value
    by_pos: dict[str, list[ValuedPlayer]] = defaultdict(list)
    for p in valued:
        v = vfn(p)
        if v is None or v <= 0:
            continue
        by_pos[p.position].append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda p: -vfn(p))

    result: dict[str, list[Tier]] = {}
    for pos, players in by_pos.items():
        result[pos] = _tier_one_position(
            players, value_fn=vfn,
            gap_multiplier=gap_multiplier,
            max_tiers=max_tiers_per_pos,
            min_tier_size=min_tier_size,
        )
    return result


def _tier_one_position(
    players: list[ValuedPlayer],
    *,
    value_fn: Callable[[ValuedPlayer], float],
    gap_multiplier: float,
    max_tiers: int,
    min_tier_size: int,
) -> list[Tier]:
    if not players:
        return []
    tiers: list[Tier] = []
    current: list[ValuedPlayer] = [players[0]]
    gaps_in_tier: list[float] = []

    for i in range(1, len(players)):
        prev = value_fn(players[i - 1])
        cur = value_fn(players[i])
        gap = prev - cur
        mean_gap = sum(gaps_in_tier) / len(gaps_in_tier) if gaps_in_tier else gap
        is_cliff = (
            len(current) >= min_tier_size
            and len(tiers) < max_tiers - 1
            and gap > mean_gap * gap_multiplier
            and gap > 1.0
        )
        if is_cliff:
            tiers.append(Tier(position=players[0].position, tier_number=len(tiers) + 1, players=current))
            current = [players[i]]
            gaps_in_tier = []
        else:
            current.append(players[i])
            gaps_in_tier.append(gap)

    if current:
        tiers.append(Tier(position=players[0].position, tier_number=len(tiers) + 1, players=current))
    return tiers


def tier_cliff_alerts(
    tiers_by_pos: dict[str, list[Tier]],
    drafted_ids: set[str],
    *,
    threshold: int = 2,
) -> list[TierCliffAlert]:
    """Flag tiers where ≤threshold players remain undrafted.

    Severity:
        critical  — 0 remaining (tier closed)
        imminent  — 1 remaining
        watch     — 2 remaining (or ≤ threshold)
    """
    alerts: list[TierCliffAlert] = []
    for pos, tiers in tiers_by_pos.items():
        for tier in tiers:
            remaining = sum(1 for p in tier.players if p.player_id not in drafted_ids)
            if remaining > threshold:
                continue
            if remaining == 0:
                continue  # tier is gone — useful trivia but not actionable
            severity = "imminent" if remaining == 1 else "watch"
            alerts.append(
                TierCliffAlert(
                    position=pos,
                    tier_number=tier.tier_number,
                    remaining=remaining,
                    severity=severity,
                )
            )
    return alerts
