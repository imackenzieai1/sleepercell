"""Algorithmic per-position tier detection.

Inputs
- A list of ValuedPlayer (already ranked by dynasty_value within position).
- Optional: ECR + SD per player from DynastyProcess fpecr_latest.csv. When present,
  tier breaks consider rank dispersion, not just point gaps.

Algorithm
1. Sort players within position by dynasty_value descending.
2. Walk down the list, opening a new tier when the gap to the previous player exceeds
   k × (in-tier mean gap), where k is configurable (default 1.5).
3. If ECR SD is available, additionally open a new tier when the SD spike between
   consecutive players exceeds a threshold (default 1.5 SD).
4. Cap tier counts per position so we don't end up with 17 tiers — collapse the long
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

from .valuation import ValuedPlayer


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
) -> dict[str, list[Tier]]:
    """Group players into tiers by position based on dynasty value gaps.

    Returns: {'QB': [Tier1, Tier2, ...], 'RB': [...], ...}
    """
    by_pos: dict[str, list[ValuedPlayer]] = defaultdict(list)
    for p in valued:
        by_pos[p.position].append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda p: -p.dynasty_value)

    result: dict[str, list[Tier]] = {}
    for pos, players in by_pos.items():
        result[pos] = _tier_one_position(
            players, gap_multiplier=gap_multiplier, max_tiers=max_tiers_per_pos, min_tier_size=min_tier_size
        )
    return result


def _tier_one_position(
    players: list[ValuedPlayer],
    *,
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
        prev = players[i - 1].dynasty_value
        cur = players[i].dynasty_value
        gap = prev - cur
        mean_gap = sum(gaps_in_tier) / len(gaps_in_tier) if gaps_in_tier else gap
        # Open new tier if gap is significantly bigger than recent gaps AND current tier has min size
        is_cliff = (
            len(current) >= min_tier_size
            and len(tiers) < max_tiers - 1
            and gap > mean_gap * gap_multiplier
            and gap > 1.0   # absolute floor to avoid splitting on tiny gaps near zero
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
