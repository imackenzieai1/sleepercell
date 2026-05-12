"""Forward-looking pick simulator.

For each of the next N unmade picks, predict who the picking team will take. We
support two prediction modes — both run in parallel so the UI can surface gaps:

  ranking_source='consensus'  (default, more realistic)
      Uses Sleeper's adp_dynasty_2qb (Superflex dynasty ADP) — the same number
      most drafters are looking at on Sleeper's draft board. This predicts what
      the rest of the league WILL do, not what they SHOULD do.

  ranking_source='optimal'
      Uses OUR league-adjusted dynasty value (Trade Whores scoring × age curve).
      Predicts what the rest of the league SHOULD do given their actual roster.
      Almost no one in your league is running this model.

The gap between the two is where the alpha lives. If consensus says Drake Maye at
pick 21 but optimal says Saquon Barkley, the league is mispricing the RB. You
know Saquon will likely still be there at YOUR pick 25.

Common model (both modes)
-------------------------
For each upcoming pick:
1. Look at the picking team's current roster (real picks + simulated picks earlier
   in this run). Count by position.
2. Compute positional need = max(0, (target_depth - have) / target_depth).
3. Infer a rough team stance by mean drafted age:
     mean_age <= 24      → rebuild-leaning  (young-player bonus)
     25 <= mean_age <= 27 → balanced
     mean_age >= 28      → compete-leaning  (veteran bonus)
4. Score each undrafted player by the chosen base ranking, then multiply by
   (1 + need × NEED_WEIGHT) and a small stance-age tiebreaker.
5. Pick the top-scoring undrafted player. Cascade picks so we don't repeat.

Every prediction carries a `why` field plus the numeric inputs that drove the
choice, so the UI can show the user exactly what we considered.

This is read-only and side-effect free — call it as many times as you like.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .draft_state import DraftState, ScheduledPick
from .league_config import LeagueConfig
from .projections import PlayerProjection
from .valuation import ValuedPlayer


# How much a 100%-need position should boost a player's effective score.
# 0.4 means "if you have zero RBs and need 4, RBs get +40% effective value vs BPA."
NEED_WEIGHT = 0.4


# Default positional depth targets in a 12-team SF dynasty.
# Caller can override by passing depth_targets= to simulate_next_picks.
DEFAULT_DEPTH_TARGETS = {"QB": 2, "RB": 4, "WR": 5, "TE": 2}


@dataclass
class PredictedPick:
    pick_no: int
    round: int
    slot_pos: int
    roster_id: int
    player_id: str
    player_name: str
    position: str
    team: str | None
    age: int | None
    dynasty_value: float
    overall_rank: int
    consensus_rank: float | None
    why: str
    # The "other" engine's choice, if it differs. None = same as primary.
    alt_player_id: str | None = None
    alt_player_name: str | None = None
    alt_position: str | None = None
    alt_rank: int | None = None
    alt_source: str | None = None  # 'consensus' or 'optimal' — whichever is NOT the primary
    # Inputs that drove the decision (for the UI's expandable details)
    pos_counts_before: dict[str, int] = field(default_factory=dict)
    pos_needs: dict[str, float] = field(default_factory=dict)
    inferred_stance: str = "balanced"
    mean_age_drafted: float | None = None


def build_consensus_index(
    projections: dict[str, PlayerProjection],
    *,
    superflex: bool,
) -> dict[str, float]:
    """Map player_id → consensus rank (lower = picked earlier, "better").

    Uses Sleeper's `adp_dynasty_2qb` for Superflex leagues (the closest signal to
    what your league is actually seeing on Sleeper's draft board) or `adp_dynasty_ppr`
    for 1QB. Missing/sentinel ADPs (999.0) fall back to a deep number.

    Tiebreaker for players with no ADP: use the Sleeper-default `pts_ppr` to derive
    a fallback rank (better player ≈ lower fallback rank). This keeps relatively
    valuable but un-ADPed players above pure depth fodder.
    """
    primary_key = "adp_dynasty_2qb" if superflex else "adp_dynasty_ppr"
    secondary_key = "adp_dynasty"   # generic fallback
    out: dict[str, float] = {}

    # First pass: pull ADP values
    for pid, proj in projections.items():
        stats = proj.stats or {}
        adp = stats.get(primary_key)
        if adp is None or adp >= 900:
            adp = stats.get(secondary_key)
        if adp is None or adp >= 900:
            adp = None  # no signal
        out[pid] = float(adp) if adp is not None else 9999.0

    # Second pass: for players with no ADP signal, use Sleeper pts_ppr as a tiebreaker.
    # We don't override real ADP — just rank the un-ADPed players among themselves.
    no_adp = [(pid, proj) for pid, proj in projections.items() if out[pid] >= 9999.0]
    no_adp.sort(key=lambda x: -(x[1].sleeper_pts_ppr or 0))
    for i, (pid, _) in enumerate(no_adp):
        # Park them after pick #500-ish, ordered by pts_ppr.
        out[pid] = 500 + i

    return out


def simulate_next_picks(
    state: DraftState,
    valued_all: list[ValuedPlayer],
    projections: dict[str, PlayerProjection],
    cfg: LeagueConfig,
    *,
    n: int = 20,
    depth_targets: dict[str, int] | None = None,
    need_weight: float = NEED_WEIGHT,
    candidate_pool_size: int = 80,
    ranking_source: str = "consensus",  # 'consensus' | 'optimal'
    consensus_index: dict[str, float] | None = None,
    include_alt_choice: bool = True,
) -> list[PredictedPick]:
    """Predict the next n unmade picks.

    Args:
        state: a built DraftState.
        valued_all: list of ValuedPlayer (output of value_population), sorted desc by dynasty_value.
        projections: player_id -> PlayerProjection.
        cfg: LeagueConfig (for SF / TEP awareness in depth targets).
        n: how many picks to look ahead.
        depth_targets: override per-position depth targets.
        need_weight: how strongly to weight positional needs (0.0 = pure BPA).
        candidate_pool_size: cap on number of candidates considered per pick (perf knob).
        ranking_source:
            'consensus' (default) — score by Sleeper SF/Dynasty ADP. This predicts
                what the rest of the league will actually do.
            'optimal' — score by our league-adjusted dynasty value. Predicts what
                the rest of the league SHOULD do given correct math.
        consensus_index: optional pre-built {player_id: adp} map. If None and
            ranking_source='consensus', it's built from `projections`.
        include_alt_choice: if True, each prediction also includes the OTHER
            engine's pick for the same slot when they differ.

    Returns:
        List of PredictedPick in pick-order.
    """
    targets = dict(depth_targets or DEFAULT_DEPTH_TARGETS)
    if cfg.superflex:
        targets["QB"] = max(targets["QB"], 2)
    else:
        targets["QB"] = min(targets["QB"], 1)
    if cfg.te_premium:
        targets["TE"] = max(targets["TE"], 2)

    consensus_index = consensus_index or build_consensus_index(projections, superflex=cfg.superflex)

    # Pre-index valued for fast id lookup
    by_id = {v.player_id: v for v in valued_all}

    # Two PARALLEL cascading drafted sets — one for the primary engine, one for the alt.
    # Each engine effectively runs its own simulation of the same draft, so the "alt"
    # column doesn't repeat the same player across multiple upcoming picks (which it
    # used to do when both engines shared a single drafted set).
    primary_drafted = set(state.drafted_ids())
    alt_drafted = set(state.drafted_ids()) if include_alt_choice else None

    # Pre-build each roster's existing draft picks (player_ids)
    real_picks_by_roster: dict[int, list[str]] = {}
    for pk in state.picks:
        rid = int(pk.get("roster_id") or 0)
        pid = str(pk.get("player_id") or "")
        if rid and pid:
            real_picks_by_roster.setdefault(rid, []).append(pid)

    simulated_picks_by_roster: dict[int, list[str]] = {}

    upcoming: list[ScheduledPick] = [sp for sp in state.schedule if not sp.made][:n]
    predictions: list[PredictedPick] = []

    for sp in upcoming:
        owner_rid = sp.owner_roster_id
        all_pids = real_picks_by_roster.get(owner_rid, []) + simulated_picks_by_roster.get(owner_rid, [])

        counts: dict[str, int] = {p: 0 for p in targets}
        ages: list[int] = []
        for pid in all_pids:
            v = by_id.get(pid)
            if v and v.position in counts:
                counts[v.position] += 1
                if v.age is not None:
                    ages.append(int(v.age))
        needs = {pos: max(0.0, (targets[pos] - counts[pos]) / max(targets[pos], 1)) for pos in targets}
        mean_age = sum(ages) / len(ages) if ages else None
        stance = _infer_stance(mean_age)

        # Primary engine: scored against its own cascading drafted set
        primary_pick = _best_candidate(
            valued_all=valued_all,
            by_id=by_id,
            simulated_drafted=primary_drafted,
            consensus_index=consensus_index,
            needs=needs,
            stance=stance,
            need_weight=need_weight,
            candidate_pool_size=candidate_pool_size,
            source=ranking_source,
        )
        if primary_pick is None:
            continue

        # Alt engine: scored against ITS OWN cascading drafted set
        alt_pick = None
        if include_alt_choice and alt_drafted is not None:
            alt_source = "optimal" if ranking_source == "consensus" else "consensus"
            alt_candidate = _best_candidate(
                valued_all=valued_all,
                by_id=by_id,
                simulated_drafted=alt_drafted,
                consensus_index=consensus_index,
                needs=needs,
                stance=stance,
                need_weight=need_weight,
                candidate_pool_size=candidate_pool_size,
                source=alt_source,
            )
            if alt_candidate and alt_candidate.player_id != primary_pick.player_id:
                # Real divergence — surface it
                alt_pick = alt_candidate
                alt_drafted.add(alt_candidate.player_id)
            else:
                # Both engines converge OR no alt found. Advance alt cascade with primary's pick
                # so the alt engine continues to track the actual draft state.
                alt_drafted.add(primary_pick.player_id)

        why = _build_why(primary_pick, counts, needs, targets, stance, ranking_source=ranking_source)

        predictions.append(PredictedPick(
            pick_no=sp.pick_no,
            round=sp.round,
            slot_pos=sp.slot_pos,
            roster_id=owner_rid,
            player_id=primary_pick.player_id,
            player_name=primary_pick.name,
            position=primary_pick.position,
            team=primary_pick.team,
            age=primary_pick.age,
            dynasty_value=primary_pick.dynasty_value,
            overall_rank=primary_pick.overall_rank,
            consensus_rank=consensus_index.get(primary_pick.player_id),
            why=why,
            alt_player_id=(alt_pick.player_id if alt_pick else None),
            alt_player_name=(alt_pick.name if alt_pick else None),
            alt_position=(alt_pick.position if alt_pick else None),
            alt_rank=(alt_pick.overall_rank if alt_pick else None),
            alt_source=("optimal" if ranking_source == "consensus" else "consensus") if alt_pick else None,
            pos_counts_before=dict(counts),
            pos_needs={k: round(v, 2) for k, v in needs.items()},
            inferred_stance=stance,
            mean_age_drafted=round(mean_age, 1) if mean_age is not None else None,
        ))
        primary_drafted.add(primary_pick.player_id)
        simulated_picks_by_roster.setdefault(owner_rid, []).append(primary_pick.player_id)

    return predictions


def _best_candidate(
    *,
    valued_all: list[ValuedPlayer],
    by_id: dict[str, ValuedPlayer],
    simulated_drafted: set[str],
    consensus_index: dict[str, float],
    needs: dict[str, float],
    stance: str,
    need_weight: float,
    candidate_pool_size: int,
    source: str,
) -> ValuedPlayer | None:
    """Return the highest-scoring undrafted player for the chosen ranking source."""
    if source == "consensus":
        # Sort the candidate pool by consensus rank ascending, then score with need/stance.
        # We take a wider pool here because consensus-bad players are still relevant if values are weird.
        sorted_by_consensus = sorted(
            (v for v in valued_all if v.player_id not in simulated_drafted),
            key=lambda v: consensus_index.get(v.player_id, 9999.0),
        )[: candidate_pool_size]
        best_score = float("inf")
        best: ValuedPlayer | None = None
        for v in sorted_by_consensus:
            rank = consensus_index.get(v.player_id, 9999.0)
            need_boost = 1.0 + needs.get(v.position, 0.0) * need_weight
            age_mult = _stance_age_multiplier(v.age, stance)
            # Lower is better for consensus: divide by boosts.
            score = rank / (need_boost * age_mult)
            if score < best_score:
                best_score, best = score, v
        return best

    # 'optimal' — score by our dynasty value (descending is better)
    best_score = -float("inf")
    best = None
    for v in valued_all[:candidate_pool_size]:
        if v.player_id in simulated_drafted:
            continue
        need_boost = 1.0 + needs.get(v.position, 0.0) * need_weight
        age_mult = _stance_age_multiplier(v.age, stance)
        score = v.dynasty_value * need_boost * age_mult
        if score > best_score:
            best_score, best = score, v
    return best


# --------------------------------------------------------------- internals


def _infer_stance(mean_age: float | None) -> str:
    if mean_age is None:
        return "balanced"
    if mean_age <= 24.0:
        return "rebuild"
    if mean_age >= 28.0:
        return "compete"
    return "balanced"


def _stance_age_multiplier(age: int | None, stance: str) -> float:
    """A small (-/+5–10%) bias on a player's score based on age vs inferred team stance.

    Intentionally gentle — overpowering this would override dynasty value, which is
    already age-curve-adjusted. We just nudge the tiebreakers.
    """
    if age is None:
        return 1.0
    if stance == "rebuild":
        if age <= 24: return 1.10
        if age >= 29: return 0.90
    elif stance == "compete":
        if age <= 23: return 0.93
        if age >= 28: return 1.05
    return 1.0


def _build_why(
    picked: ValuedPlayer,
    counts: dict[str, int],
    needs: dict[str, float],
    targets: dict[str, int],
    stance: str,
    *,
    ranking_source: str = "consensus",
) -> str:
    bits: list[str] = []
    pos_need = needs.get(picked.position, 0.0)
    have = counts.get(picked.position, 0)
    want = targets.get(picked.position, 1)
    if pos_need >= 0.5:
        bits.append(f"big {picked.position} hole ({have}/{want})")
    elif pos_need > 0:
        bits.append(f"{picked.position} depth ({have}/{want})")
    else:
        bits.append(f"BPA — {picked.position} pool stays deep ({have}/{want})")

    # Source-aware framing of the value bit
    if ranking_source == "consensus":
        bits.append("near top of consensus board")
    else:
        if picked.overall_rank <= 12:
            bits.append("elite-tier value (our model)")
        elif picked.overall_rank <= 36:
            bits.append("top-3rd-round value (our model)")

    if picked.age is not None:
        if stance == "rebuild" and picked.age <= 24:
            bits.append(f"rebuild fit ({picked.age}y)")
        elif stance == "compete" and picked.age >= 28:
            bits.append(f"win-now vet ({picked.age}y)")

    return "; ".join(bits)
