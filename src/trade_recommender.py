"""Auto-generated trade offers.

Goal: for every other manager in your league, generate the trade offers that
maximize **mutual win** — you gain TW value AND they think they gain consensus
value. Both sides' models say "yes."

Trade direction for compete mode: YOU give multiple assets to acquire one high-tier
player. The pattern that consistently fails the human-logic test is "you give 1
asset, you get 2 assets back" — no manager parts with a drafted player AND a pick
for a single pick of equal or lower draft position.

Algorithm
---------
For each partner:
  Gather assets on both sides:
    - my drafted players, their drafted players
    - my future rookie picks, theirs
    - my unmade startup-draft picks, theirs (valued via "(N - picks_made)th best
      undrafted by consensus" — realistic for the player you'd actually land)
  Apply DRAFT-CAPITAL FLOOR to drafted players: a player's consensus value is
  the MAX of (DP value, consensus value of the pick they were drafted at). This
  prevents the model from undervaluing recently-drafted players whose DP value
  hasn't caught up to draft capital.

  Generate candidate trades in 5 patterns. The first three are STARTUP-PICK SWAPS
  (the primary focus); the last two are future-pick-for-player exploits.

  Startup-pick swaps:
    SP-UP)   2 of my unmade picks ↔ 1 of their earlier unmade picks
             (trade up to grab a specific target)
    SP-DOWN) 1 of my earlier unmade picks ↔ 2 of their later unmade picks
             (sell pick capital for volume)
    SP-PLAYER) 2 of my unmade picks ↔ their player + their later pick
             (acquire established production by climbing the board)

  Future-for-now (still useful for the QB exploit):
    A) one of my future rookie picks ↔ one of their players
    B) my future pick + my unmade startup pick ↔ one of their players

  Past-trade-activity weighting: partners who've already completed trades in this
  league get a small score multiplier — they're proven dealmakers.

For each candidate, evaluate via trade_analysis.evaluate_trade. Filter to:
  - my TW delta >= MIN_TW_GAIN
  - their consensus response in {accept, counter}
Score by mutual-win product:
  score = max(0, my_tw_delta) × (1 + max(0, their_consensus_delta_for_them) / 1000)

Take top N offers per partner (default 2) and top M overall (default 12).

Caveats
- We don't know each manager's literal preferences. Stance is inferred from age
  of drafted players (compete-mode bias on veteran rosters, etc.) but it's only
  a tiebreaker — we don't filter to "they'll definitely accept."
- The score function rewards big-gap trades. Sweetening the value gap above
  ~+500 doesn't help much (saturating).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .draft_state import DraftState
from .future_pick_value import PickValueModel
from .league_config import LeagueConfig
from .projections import PlayerProjection
from .simulator import build_consensus_index
from .team_analysis import team_summary
from .trade_analysis import (
    Asset,
    PickAsset,
    PlayerAsset,
    TradeEval,
    evaluate_trade,
    pick_asset,
    player_asset,
)
from .valuation import ValuedPlayer


# Tuning knobs ----------------------------------------------------------------
MIN_TW_GAIN = 60.0          # minimum TW delta to even consider an offer
MAX_PER_PARTNER = 2          # how many offers to keep per partner
MAX_TOTAL_OFFERS = 12        # global cap
TOP_THEIR_PLAYERS = 6        # how deep into their roster to consider acquiring
TOP_SELL_TARGETS = 3         # which of my drafted players are sell-candidates


@dataclass
class TradeOffer:
    partner_rid: int
    partner_name: str
    give: list[Asset]
    get: list[Asset]
    eval: TradeEval
    rationale: str
    score: float
    summary_line: str   # short one-line summary for headers


# ---------------------------------------------------------------------------
# Asset enumeration


def _value_unmade_pick(
    pick_no: int,
    valued_pool: list[ValuedPlayer],
    consensus_idx: dict[str, float],
    drafted_ids: set[str],
    picks_already_made: int,
) -> tuple[float, float]:
    """Approximate (tw_value, consensus_value) of an unmade startup-draft pick.

    Realistic model: rank undrafted players by consensus ADP ascending. The player
    you'll actually land at pick N is roughly the (N - picks_already_made)-th best
    undrafted player — not "the player closest to ADP N" (the prior approach,
    which inflated picks by latching onto stragglers with low ADP).
    """
    undrafted = sorted(
        [v for v in valued_pool if v.player_id not in drafted_ids],
        key=lambda v: consensus_idx.get(v.player_id, 9999.0),
    )
    if not undrafted:
        return 0.0, 0.0
    idx = max(0, min(pick_no - picks_already_made - 1, len(undrafted) - 1))
    target = undrafted[idx]
    return target.dynasty_value, float(target.dp_value_2qb or target.dp_value_1qb or 0.0)


def _gather_assets(
    *,
    state: DraftState,
    roster_id: int,
    valued_by_id: dict[str, ValuedPlayer],
    valued_pool: list[ValuedPlayer],
    consensus_idx: dict[str, float],
    pick_model: PickValueModel,
    is_me: bool,
    drafted_ids: set[str],
    pick_no_to_consensus_value: dict[int, float],
    picks_already_made: int,
) -> tuple[list[PlayerAsset], list[PickAsset], list[PickAsset]]:
    """Return (drafted_players, future_picks, unmade_startup_picks) for a roster.

    Applies the **draft-capital floor** to drafted players: each player's effective
    consensus value is `max(DP value, consensus value of pick they were drafted at)`.
    This prevents the recommender from underpricing players whose DP value lags
    their draft cost (most commonly: TEs in SF, recently-drafted rookies).
    """
    # Map player_id → pick_no at which they were drafted (for the floor calc)
    pick_no_for_player: dict[str, int] = {}
    for p in state.picks:
        pid = str(p.get("player_id") or "")
        if pid:
            pick_no_for_player[pid] = int(p.get("pick_no") or 0)

    # Drafted players (with draft-capital floor applied)
    drafted_pids = [
        str(p["player_id"]) for p in state.picks
        if int(p.get("roster_id") or 0) == roster_id and p.get("player_id")
    ]
    players: list[PlayerAsset] = []
    for pid in drafted_pids:
        v = valued_by_id.get(pid)
        if not v:
            continue
        raw_consensus = float(v.dp_value_2qb or v.dp_value_1qb or 0.0)
        # Floor: value of the pick they were drafted at
        pno = pick_no_for_player.get(pid)
        floor = pick_no_to_consensus_value.get(pno, 0.0) if pno else 0.0
        effective_consensus = max(raw_consensus, floor)
        a = PlayerAsset(
            player_id=v.player_id, name=v.name, position=v.position,
            age=v.age, tw_value=v.dynasty_value,
            consensus_value=effective_consensus,
        )
        players.append(a)

    # Future rookie picks (2027 + 2028)
    futures: list[PickAsset] = []
    for season in ("2027", "2028"):
        for p in state.future_pick_inventory(season).get(roster_id, []):
            a = pick_asset(
                season, p["round"], pick_model,
                original_roster_id=p["orig_roster_id"],
                label=f"{season} R{p['round']}",
            )
            if a:
                futures.append(a)

    # Unmade startup-draft picks (realistic valuation by index-after-picks-made)
    unmade: list[PickAsset] = []
    upcoming = [sp for sp in state.schedule if not sp.made and sp.owner_roster_id == roster_id]
    for sp in upcoming[:6]:
        tw, cons = _value_unmade_pick(
            sp.pick_no, valued_pool, consensus_idx, drafted_ids, picks_already_made,
        )
        unmade.append(PickAsset(
            season="startup",
            round=sp.round,
            tw_value=tw,
            consensus_value=cons,
            original_roster_id=roster_id,
            label=f"pick {sp.pick_no} (R{sp.round}.{sp.slot_pos})",
        ))

    return players, futures, unmade


def _build_pick_no_consensus_floor(
    state: DraftState,
    valued_pool: list[ValuedPlayer],
    consensus_idx: dict[str, float],
) -> dict[int, float]:
    """For each pick_no that's been made, return the consensus value implied by that slot.

    Used as the floor for drafted players' consensus value. If you spent a R2 pick on
    McBride, his trade floor is roughly the consensus value of that R2 slot.
    """
    # Sort all valued players by consensus rank ascending. The Nth-ranked player's
    # DP value is roughly what pick N "is worth" in the eyes of the consensus market.
    by_consensus = sorted(
        valued_pool,
        key=lambda v: consensus_idx.get(v.player_id, 9999.0),
    )
    out: dict[int, float] = {}
    for p in state.picks:
        pno = int(p.get("pick_no") or 0)
        if not pno:
            continue
        idx = min(pno - 1, len(by_consensus) - 1)
        v = by_consensus[idx]
        out[pno] = float(v.dp_value_2qb or v.dp_value_1qb or 0.0)
    return out


# ---------------------------------------------------------------------------
# Candidate generation per partner


def _surplus_positions(summary) -> set[str]:
    """Positions where I'm at or above depth target."""
    return {pos for pos, n in summary.counts.items() if n >= summary.depth_targets.get(pos, 1)}


def _their_likely_want(their_summary) -> str | None:
    """Position the partner most needs."""
    if not their_summary.needs:
        return None
    return max(their_summary.needs, key=their_summary.needs.get)


def _score_offer(ev: TradeEval) -> float:
    """Higher = better. Rewards mutual-win trades; saturates above huge gaps."""
    my_gain = max(0.0, ev.tw_delta)
    if my_gain < MIN_TW_GAIN:
        return 0.0
    # Reward when partner also "wins" by their model. Their gain is positive when
    # their consensus_delta_for_them >= 0.
    their_gain = max(0.0, ev.consensus_delta_for_them)
    # Score: my gain × (1 + sigmoid-like bonus from their gain)
    return my_gain * (1.0 + min(their_gain, 1500.0) / 1000.0)


def _is_acceptable(ev: TradeEval) -> bool:
    if ev.tw_delta < MIN_TW_GAIN:
        return False
    return ev.their_response in ("accept", "counter")


def _build_rationale(
    ev: TradeEval,
    give: list[Asset],
    get: list[Asset],
    my_summary,
    partner_summary,
) -> str:
    bits: list[str] = []
    # Roster impact (my side)
    got_positions = [a.position for a in get if isinstance(a, PlayerAsset)]
    for pos in got_positions:
        need = my_summary.needs.get(pos, 0.0)
        if need >= 0.5:
            bits.append(
                f"fills your {pos} hole ({my_summary.counts.get(pos,0)}/{my_summary.depth_targets.get(pos,0)})"
            )
        elif need > 0:
            bits.append(f"adds {pos} depth")
    # Roster impact (their side)
    gave_positions = [a.position for a in give if isinstance(a, PlayerAsset)]
    for pos in gave_positions:
        need = partner_summary.needs.get(pos, 0.0)
        if need >= 0.4:
            bits.append(f"plugs their {pos} need")
    # Value framing
    if ev.tw_delta >= 200:
        bits.append(f"you gain {ev.tw_delta:+.0f} TW")
    elif ev.tw_delta >= MIN_TW_GAIN:
        bits.append(f"you net {ev.tw_delta:+.0f} TW")
    if ev.consensus_delta_for_them > 200:
        bits.append(f"they think they win {ev.consensus_delta_for_them:+.0f} consensus")
    return "; ".join(bits) or "value-aligned swap"


def _short_asset(a: Asset) -> str:
    if isinstance(a, PlayerAsset):
        return f"{a.position} {a.name}"
    if isinstance(a, PickAsset):
        return a.label or f"{a.season} R{a.round}"
    return "?"


def _summary_line(give: list[Asset], get: list[Asset]) -> str:
    g = ", ".join(_short_asset(a) for a in give)
    r = ", ".join(_short_asset(a) for a in get)
    return f"give {g} → get {r}"


# ---------------------------------------------------------------------------
# Public API


def recommend_offers(
    state: DraftState,
    valued_all: list[ValuedPlayer],
    projections: dict[str, PlayerProjection],
    pick_model: PickValueModel,
    cfg: LeagueConfig,
    my_roster_id: int,
    *,
    max_per_partner: int = MAX_PER_PARTNER,
    max_total: int = MAX_TOTAL_OFFERS,
    roster_name_fn=None,
    activity_per_roster: dict[int, int] | None = None,
) -> list[TradeOffer]:
    """Generate top auto-recommended trade offers."""
    roster_name_fn = roster_name_fn or (lambda rid: f"roster {rid}")
    valued_by_id = {v.player_id: v for v in valued_all}
    consensus_idx = build_consensus_index(projections, superflex=cfg.superflex)
    drafted_ids = state.drafted_ids()
    picks_already_made = len(state.picks)
    activity_per_roster = activity_per_roster or {}

    # Build the per-pick consensus floor map (used for the draft-capital floor)
    pick_no_consensus = _build_pick_no_consensus_floor(state, valued_all, consensus_idx)

    my_summary = team_summary(state, my_roster_id, projections)
    my_players, my_futures, my_unmade = _gather_assets(
        state=state, roster_id=my_roster_id,
        valued_by_id=valued_by_id, valued_pool=valued_all,
        consensus_idx=consensus_idx, pick_model=pick_model,
        is_me=True, drafted_ids=drafted_ids,
        pick_no_to_consensus_value=pick_no_consensus,
        picks_already_made=picks_already_made,
    )
    # Cap "sell-candidates" — only consider trading my top-N players as the giver
    sell_candidates = sorted(my_players, key=lambda a: -a.tw_value)[:TOP_SELL_TARGETS]

    all_offers: list[TradeOffer] = []

    for partner_rid in sorted(set(state.slot_to_roster.values())):
        if partner_rid == my_roster_id:
            continue
        their_summary = team_summary(state, partner_rid, projections)
        their_players, their_futures, their_unmade = _gather_assets(
            state=state, roster_id=partner_rid,
            valued_by_id=valued_by_id, valued_pool=valued_all,
            consensus_idx=consensus_idx, pick_model=pick_model,
            is_me=False, drafted_ids=drafted_ids,
            pick_no_to_consensus_value=pick_no_consensus,
            picks_already_made=picks_already_made,
        )
        # Sort their players by TW desc — we want to acquire their best
        their_top = sorted(their_players, key=lambda a: -a.tw_value)[:TOP_THEIR_PLAYERS]

        partner_offers: list[TradeOffer] = []
        # Activity multiplier: more historical trades = more open to dealing
        activity_mult = 1.0 + min(activity_per_roster.get(partner_rid, 0), 5) * 0.10

        # =================================================================
        # STARTUP-PICK SWAPS (the primary patterns for an active draft)
        # =================================================================

        # SP-UP: 2 of my unmade picks → 1 of their earlier unmade picks (trade up)
        for tu in their_unmade[:5]:
            for i, mu1 in enumerate(my_unmade[:5]):
                for mu2 in my_unmade[i + 1:6]:
                    # Target pick must be more valuable than each of mine (earlier in board)
                    if tu.tw_value <= max(mu1.tw_value, mu2.tw_value):
                        continue
                    give = [mu1, mu2]
                    get = [tu]
                    ev = evaluate_trade(give=give, get=get, cfg=cfg, my_roster_id=my_roster_id)
                    if not _is_acceptable(ev):
                        continue
                    partner_offers.append(TradeOffer(
                        partner_rid=partner_rid,
                        partner_name=roster_name_fn(partner_rid),
                        give=give, get=get, eval=ev,
                        rationale=_build_rationale(ev, give, get, my_summary, their_summary)
                                  + "; startup trade-up",
                        score=_score_offer(ev) * activity_mult,
                        summary_line=_summary_line(give, get),
                    ))

        # SP-DOWN: 1 of my earlier unmade picks → 2 of their later unmade picks (trade down)
        for mu in my_unmade[:4]:
            for i, tu1 in enumerate(their_unmade[:5]):
                for tu2 in their_unmade[i + 1:6]:
                    if mu.tw_value <= max(tu1.tw_value, tu2.tw_value):
                        continue
                    give = [mu]
                    get = [tu1, tu2]
                    ev = evaluate_trade(give=give, get=get, cfg=cfg, my_roster_id=my_roster_id)
                    if not _is_acceptable(ev):
                        continue
                    partner_offers.append(TradeOffer(
                        partner_rid=partner_rid,
                        partner_name=roster_name_fn(partner_rid),
                        give=give, get=get, eval=ev,
                        rationale=_build_rationale(ev, give, get, my_summary, their_summary)
                                  + "; startup trade-down",
                        score=_score_offer(ev) * activity_mult,
                        summary_line=_summary_line(give, get),
                    ))

        # SP-PLAYER: 2 of my unmade picks → their player + their later pick
        # (acquire established production by giving up draft capital)
        for tp in their_top:
            for tu in their_unmade[:5]:
                for i, mu1 in enumerate(my_unmade[:4]):
                    for mu2 in my_unmade[i + 1:5]:
                        give = [mu1, mu2]
                        get = [tp, tu]
                        ev = evaluate_trade(give=give, get=get, cfg=cfg, my_roster_id=my_roster_id)
                        if not _is_acceptable(ev):
                            continue
                        partner_offers.append(TradeOffer(
                            partner_rid=partner_rid,
                            partner_name=roster_name_fn(partner_rid),
                            give=give, get=get, eval=ev,
                            rationale=_build_rationale(ev, give, get, my_summary, their_summary),
                            score=_score_offer(ev) * activity_mult,
                            summary_line=_summary_line(give, get),
                        ))

        # =================================================================
        # FUTURE-FOR-NOW (still useful for the QB exploit)
        # =================================================================

        # ---------------------------------------------------------------
        # Pattern A: my future pick ↔ their player (1-for-1)
        # ---------------------------------------------------------------
        for tp in their_top:
            for mp in my_futures:
                ev = evaluate_trade(give=[mp], get=[tp], cfg=cfg, my_roster_id=my_roster_id)
                if not _is_acceptable(ev):
                    continue
                partner_offers.append(TradeOffer(
                    partner_rid=partner_rid,
                    partner_name=roster_name_fn(partner_rid),
                    give=[mp], get=[tp], eval=ev,
                    rationale=_build_rationale(ev, [mp], [tp], my_summary, their_summary),
                    score=_score_offer(ev) * activity_mult,
                    summary_line=_summary_line([mp], [tp]),
                ))

        # ---------------------------------------------------------------
        # Pattern B: my future pick + my unmade startup pick ↔ their player (2-for-1, trade up)
        # ---------------------------------------------------------------
        for tp in their_top:
            for mp in my_futures:
                for mu in my_unmade[:4]:
                    give = [mp, mu]
                    get = [tp]
                    ev = evaluate_trade(give=give, get=get, cfg=cfg, my_roster_id=my_roster_id)
                    if not _is_acceptable(ev):
                        continue
                    partner_offers.append(TradeOffer(
                        partner_rid=partner_rid,
                        partner_name=roster_name_fn(partner_rid),
                        give=give, get=get, eval=ev,
                        rationale=_build_rationale(ev, give, get, my_summary, their_summary),
                        score=_score_offer(ev) * activity_mult,
                        summary_line=_summary_line(give, get),
                    ))

        # ---------------------------------------------------------------
        # Pattern C: my future pick + a surplus-position drafted player ↔ their player
        # (sell-high while buying the position I actually need)
        # ---------------------------------------------------------------
        surplus = _surplus_positions(my_summary)
        for tp in their_top:
            for mp in my_futures:
                for sp_player in sell_candidates:
                    if sp_player.position not in surplus:
                        continue
                    if sp_player.tw_value > tp.tw_value * 0.85:
                        continue
                    give = [mp, sp_player]
                    get = [tp]
                    ev = evaluate_trade(give=give, get=get, cfg=cfg, my_roster_id=my_roster_id)
                    if not _is_acceptable(ev):
                        continue
                    partner_offers.append(TradeOffer(
                        partner_rid=partner_rid,
                        partner_name=roster_name_fn(partner_rid),
                        give=give, get=get, eval=ev,
                        rationale=_build_rationale(ev, give, get, my_summary, their_summary),
                        score=_score_offer(ev) * activity_mult,
                        summary_line=_summary_line(give, get),
                    ))

        # Dedupe (same give+get) and keep top N per partner
        seen: set[tuple] = set()
        dedup: list[TradeOffer] = []
        for o in sorted(partner_offers, key=lambda o: -o.score):
            key = (
                tuple(sorted((a.player_id if hasattr(a, "player_id") else f"{a.season}-R{a.round}") for a in o.give)),
                tuple(sorted((a.player_id if hasattr(a, "player_id") else f"{a.season}-R{a.round}") for a in o.get)),
            )
            if key in seen:
                continue
            seen.add(key)
            dedup.append(o)
        all_offers.extend(dedup[:max_per_partner])

    all_offers.sort(key=lambda o: -o.score)
    return all_offers[:max_total]
