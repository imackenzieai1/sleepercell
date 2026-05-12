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
MAX_PER_PARTNER = 3          # how many offers to keep per partner
MAX_TOTAL_OFFERS = 15        # global cap
TOP_THEIR_PLAYERS = 6        # how deep into their roster to consider acquiring
TOP_SELL_TARGETS = 3         # which of my drafted players are sell-candidates

# Search-depth presets — let the UI toggle "how many candidates do we even consider".
# Lower min_tw_gain + higher top_their_players = more variety, more noise.
SEARCH_DEPTHS: dict[str, dict[str, float]] = {
    "strict": {
        "min_tw_gain": 60.0,
        "max_per_partner": 3,
        "max_total": 15,
        "top_their_players": 6,
    },
    "normal": {
        "min_tw_gain": 30.0,
        "max_per_partner": 4,
        "max_total": 25,
        "top_their_players": 8,
    },
    "aggressive": {
        "min_tw_gain": 0.0,
        "max_per_partner": 5,
        "max_total": 40,
        "top_their_players": 10,
    },
}


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


@dataclass
class AcquisitionOption:
    """One way to acquire the target asset(s) — what to give, and what it costs."""
    give: list[Asset]
    get: list[Asset]
    eval: TradeEval
    summary_line: str


# ---------------------------------------------------------------------------
# Asset enumeration


def _market_value(v: ValuedPlayer) -> float:
    """Best-available community/market value for a player.
    Same chain used everywhere in trade math (adjusted_ktc → ktc → DP)."""
    return float(
        getattr(v, "adjusted_ktc", None)
        or getattr(v, "ktc_value", None)
        or getattr(v, "dp_value_2qb", None)
        or getattr(v, "dp_value_1qb", None)
        or 0.0
    )


def _value_unmade_pick(
    pick_no: int,
    valued_pool: list[ValuedPlayer],
    consensus_idx: dict[str, float],
    drafted_ids: set[str],
    picks_already_made: int,
) -> tuple[float, float]:
    """Approximate (tw_value, consensus_value) of an unmade startup-draft pick.

    **Monotonicity guarantee**: pick N's value is always ≥ pick (N+1)'s value.
    We achieve this by sorting the undrafted pool by COMMUNITY VALUE descending
    (highest-market-value player first) and taking the N-th by community value.
    The prior approach sorted by ADP, then took the player at that ADP rank — but
    individual players have different (ADP, community-value) profiles, so the
    resulting pick values could invert (pick 27 worth more than pick 25). This
    fix anchors the pick's value to its market-rank position in the pool, which
    is what matters for trade math anyway.

    TW value is taken from the same target player as the community value, so
    both halves of the returned tuple stay internally consistent.
    """
    undrafted = sorted(
        [v for v in valued_pool if v.player_id not in drafted_ids and _market_value(v) > 0],
        key=lambda v: -_market_value(v),
    )
    if not undrafted:
        return 0.0, 0.0
    idx = max(0, min(pick_no - picks_already_made - 1, len(undrafted) - 1))
    target = undrafted[idx]
    return float(target.dynasty_value or 0.0), _market_value(target)


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
        # Prefer adjusted-community (community × league-fit) → community → DP fallback.
        # This makes the recommender's market values consistent with what the user sees
        # in Best Available, AND reflects league-specific scoring premiums (e.g., elite
        # volume QBs lift higher in a TEP/6pt-TD/completion-bonus league than raw KTC shows).
        raw_consensus = float(
            v.adjusted_ktc
            or v.ktc_value
            or v.dp_value_2qb
            or v.dp_value_1qb
            or 0.0
        )
        # Draft-capital floor: a player drafted at pick N is worth AT LEAST the
        # community value of pick N, regardless of what the model thinks.
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


def _score_offer(ev: TradeEval, min_tw_gain: float = MIN_TW_GAIN) -> float:
    """Higher = better. Rewards mutual-win trades; saturates above huge gaps."""
    my_gain = max(0.0, ev.tw_delta)
    if my_gain < min_tw_gain:
        return 0.0
    their_gain = max(0.0, ev.consensus_delta_for_them)
    return my_gain * (1.0 + min(their_gain, 1500.0) / 1000.0)


def _is_acceptable(ev: TradeEval, min_tw_gain: float = MIN_TW_GAIN) -> bool:
    if ev.tw_delta < min_tw_gain:
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
    min_tw_gain: float = MIN_TW_GAIN,
    top_their_players: int = TOP_THEIR_PLAYERS,
    partner_filter: int | None = None,
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

    # Local helpers that respect the min_tw_gain override
    _acc = lambda ev: _is_acceptable(ev, min_tw_gain)
    _sco = lambda ev: _score_offer(ev, min_tw_gain)

    for partner_rid in sorted(set(state.slot_to_roster.values())):
        if partner_rid == my_roster_id:
            continue
        if partner_filter is not None and partner_rid != partner_filter:
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
        their_top = sorted(their_players, key=lambda a: -a.tw_value)[:top_their_players]

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
                    if not _acc(ev):
                        continue
                    partner_offers.append(TradeOffer(
                        partner_rid=partner_rid,
                        partner_name=roster_name_fn(partner_rid),
                        give=give, get=get, eval=ev,
                        rationale=_build_rationale(ev, give, get, my_summary, their_summary)
                                  + "; startup trade-up",
                        score=_sco(ev) * activity_mult,
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
                    if not _acc(ev):
                        continue
                    partner_offers.append(TradeOffer(
                        partner_rid=partner_rid,
                        partner_name=roster_name_fn(partner_rid),
                        give=give, get=get, eval=ev,
                        rationale=_build_rationale(ev, give, get, my_summary, their_summary)
                                  + "; startup trade-down",
                        score=_sco(ev) * activity_mult,
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
                        if not _acc(ev):
                            continue
                        partner_offers.append(TradeOffer(
                            partner_rid=partner_rid,
                            partner_name=roster_name_fn(partner_rid),
                            give=give, get=get, eval=ev,
                            rationale=_build_rationale(ev, give, get, my_summary, their_summary),
                            score=_sco(ev) * activity_mult,
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
                if not _acc(ev):
                    continue
                partner_offers.append(TradeOffer(
                    partner_rid=partner_rid,
                    partner_name=roster_name_fn(partner_rid),
                    give=[mp], get=[tp], eval=ev,
                    rationale=_build_rationale(ev, [mp], [tp], my_summary, their_summary),
                    score=_sco(ev) * activity_mult,
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
                    if not _acc(ev):
                        continue
                    partner_offers.append(TradeOffer(
                        partner_rid=partner_rid,
                        partner_name=roster_name_fn(partner_rid),
                        give=give, get=get, eval=ev,
                        rationale=_build_rationale(ev, give, get, my_summary, their_summary),
                        score=_sco(ev) * activity_mult,
                        summary_line=_summary_line(give, get),
                    ))

        # ---------------------------------------------------------------
        # Pattern MEGA-A: 3 of my unmade picks → their top player + their unmade pick
        # (big trade-up: I give up a lot of draft capital to land a tier-1 player + bump)
        # ---------------------------------------------------------------
        for tp in their_top[:4]:
            for tu in their_unmade[:4]:
                for i, mu1 in enumerate(my_unmade[:4]):
                    for j, mu2 in enumerate(my_unmade[i + 1:5], start=i + 1):
                        for mu3 in my_unmade[j + 1:6]:
                            give = [mu1, mu2, mu3]
                            get = [tp, tu]
                            ev = evaluate_trade(give=give, get=get, cfg=cfg, my_roster_id=my_roster_id)
                            if not _acc(ev):
                                continue
                            partner_offers.append(TradeOffer(
                                partner_rid=partner_rid,
                                partner_name=roster_name_fn(partner_rid),
                                give=give, get=get, eval=ev,
                                rationale=_build_rationale(ev, give, get, my_summary, their_summary)
                                          + "; big package",
                                score=_sco(ev) * activity_mult,
                                summary_line=_summary_line(give, get),
                            ))

        # ---------------------------------------------------------------
        # Pattern MEGA-XL: 4 of my picks (any mix) → their top player + their unmade pick.
        # Surfaces the kind of 4-for-2 swap-up trades that actually happen in active SF
        # drafts (e.g., JimMack/Stahov 4-pick-for-2-pick deal). Restricted to top-K assets
        # to keep search bounded — C(10,4)=210 per partner is fast.
        # ---------------------------------------------------------------
        my_pool_all = sorted(
            list(my_futures) + list(my_unmade),
            key=lambda a: -a.consensus_value,
        )[:10]
        from itertools import combinations as _combos
        for tp in their_top[:4]:
            for tu in their_unmade[:4]:
                for combo in _combos(my_pool_all, 4):
                    give = list(combo)
                    get = [tp, tu]
                    ev = evaluate_trade(give=give, get=get, cfg=cfg, my_roster_id=my_roster_id)
                    if not _acc(ev):
                        continue
                    partner_offers.append(TradeOffer(
                        partner_rid=partner_rid,
                        partner_name=roster_name_fn(partner_rid),
                        give=give, get=get, eval=ev,
                        rationale=_build_rationale(ev, give, get, my_summary, their_summary)
                                  + "; big package (4-for-2)",
                        score=_sco(ev) * activity_mult,
                        summary_line=_summary_line(give, get),
                    ))

        # ---------------------------------------------------------------
        # Pattern MEGA-B: 2 of my future picks + 1 unmade startup pick → their top player
        # (heavy future-for-now overpay for an elite QB exploit)
        # ---------------------------------------------------------------
        for tp in their_top[:4]:
            for i, fp1 in enumerate(my_futures[:4]):
                for fp2 in my_futures[i + 1:5]:
                    for mu in my_unmade[:3]:
                        give = [fp1, fp2, mu]
                        get = [tp]
                        ev = evaluate_trade(give=give, get=get, cfg=cfg, my_roster_id=my_roster_id)
                        if not _acc(ev):
                            continue
                        partner_offers.append(TradeOffer(
                            partner_rid=partner_rid,
                            partner_name=roster_name_fn(partner_rid),
                            give=give, get=get, eval=ev,
                            rationale=_build_rationale(ev, give, get, my_summary, their_summary)
                                      + "; big future-for-now",
                            score=_sco(ev) * activity_mult,
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
                    if not _acc(ev):
                        continue
                    partner_offers.append(TradeOffer(
                        partner_rid=partner_rid,
                        partner_name=roster_name_fn(partner_rid),
                        give=give, get=get, eval=ev,
                        rationale=_build_rationale(ev, give, get, my_summary, their_summary),
                        score=_sco(ev) * activity_mult,
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


# ---------------------------------------------------------------------------
# Reverse trade planner — "I want X, what do I give up?"


def plan_acquisition(
    state: DraftState,
    valued_all: list[ValuedPlayer],
    projections: dict[str, PlayerProjection],
    pick_model: PickValueModel,
    cfg: LeagueConfig,
    my_roster_id: int,
    partner_rid: int,
    target_player_ids: list[str] | None = None,
    target_picks: list[tuple[str, int, int]] | None = None,   # [(season, round, originator_rid), ...]
    target_startup_picks: list[int] | None = None,            # [pick_no, ...] — their unmade startup picks
    *,
    max_combo_size: int = 5,
    max_results: int = 10,
    accept_threshold: float | None = None,
    pool_cap_for_large_combos: int = 12,
    roster_name_fn=None,
) -> list[AcquisitionOption]:
    """Reverse planner: given what you WANT from a partner, find cheapest packages to offer.

    Returns AcquisitionOption list sorted by YOUR TW gain (best for you first), filtered
    to trades the partner would plausibly accept (consensus_delta_for_them >= -100).

    Args:
        max_combo_size: largest "give" package to consider (1..max_combo_size assets).
            Up to 5 supported. For sizes ≥ 4 the search restricts to the top
            `pool_cap_for_large_combos` of your assets (by community value) to keep
            combinatorics bounded — C(30,5) is too many to enumerate, C(12,5) is 792.
        pool_cap_for_large_combos: how many of your top assets to consider when
            searching combos of size ≥ 4. Lower = faster but may miss creative packages.
    """
    from itertools import combinations

    roster_name_fn = roster_name_fn or (lambda rid: f"roster {rid}")
    valued_by_id = {v.player_id: v for v in valued_all}
    consensus_idx = build_consensus_index(projections, superflex=cfg.superflex)
    drafted_ids = state.drafted_ids()
    picks_already_made = len(state.picks)
    pick_no_consensus = _build_pick_no_consensus_floor(state, valued_all, consensus_idx)

    # 1. Build target asset list (what you want to GET)
    get_assets: list[Asset] = []
    for pid in (target_player_ids or []):
        a = player_asset(pid, valued_all)
        if a:
            get_assets.append(a)
    for season, rnd, orig in (target_picks or []):
        label = f"{season} R{rnd}"
        if orig != partner_rid:
            label += f" (via {roster_name_fn(orig)})"
        a = pick_asset(season, rnd, pick_model, original_roster_id=orig, label=label)
        if a:
            get_assets.append(a)
    if target_startup_picks:
        for sp in state.schedule:
            if sp.made or sp.owner_roster_id != partner_rid:
                continue
            if sp.pick_no not in target_startup_picks:
                continue
            tw, cons = _value_unmade_pick(sp.pick_no, valued_all, consensus_idx, drafted_ids, picks_already_made)
            get_assets.append(PickAsset(
                season="startup", round=sp.round, tw_value=tw, consensus_value=cons,
                original_roster_id=partner_rid,
                label=f"pick {sp.pick_no} (R{sp.round}.{sp.slot_pos})",
            ))

    if not get_assets:
        return []

    # 2. Build my asset pool (everything I could offer)
    my_players, my_futures, my_unmade = _gather_assets(
        state=state, roster_id=my_roster_id,
        valued_by_id=valued_by_id, valued_pool=valued_all,
        consensus_idx=consensus_idx, pick_model=pick_model,
        is_me=True, drafted_ids=drafted_ids,
        pick_no_to_consensus_value=pick_no_consensus,
        picks_already_made=picks_already_made,
    )
    pool: list[Asset] = list(my_players) + list(my_futures) + list(my_unmade)
    if not pool:
        return []

    threshold = accept_threshold if accept_threshold is not None else -100.0

    # For very large combo sizes (4+) the full enumeration would be ~30^5 = millions.
    # Restrict to the top `pool_cap_for_large_combos` assets by community value when size >= 4.
    pool_full = pool
    pool_capped = sorted(pool, key=lambda a: -a.consensus_value)[:pool_cap_for_large_combos]

    # 3. Enumerate combinations of MY assets
    seen_keys: set[tuple] = set()
    candidates: list[AcquisitionOption] = []
    for size in range(1, max_combo_size + 1):
        search_pool = pool_full if size <= 3 else pool_capped
        for combo in combinations(search_pool, size):
            key = tuple(sorted(
                (a.player_id if isinstance(a, PlayerAsset)
                 else f"pick:{a.season}:{a.round}:{a.original_roster_id}")
                for a in combo
            ))
            if key in seen_keys:
                continue
            seen_keys.add(key)

            ev = evaluate_trade(give=list(combo), get=get_assets, cfg=cfg, my_roster_id=my_roster_id)
            if ev.consensus_delta_for_them < threshold:
                continue

            give_str = ", ".join(
                (a.name if isinstance(a, PlayerAsset) else a.label) for a in combo
            )
            candidates.append(AcquisitionOption(
                give=list(combo),
                get=get_assets,
                eval=ev,
                summary_line=give_str,
            ))

    candidates.sort(key=lambda o: -o.eval.tw_delta)
    return candidates[:max_results]
