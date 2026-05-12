"""Trade evaluation — dual-valuation calculator.

Every trade is computed twice:
  • **TW value** — using OUR league-adjusted dynasty values for players + scaled pick
    values for futures. This is the truth as we see it.
  • **Consensus value** — using DynastyProcess value_2qb for players + DP pick values.
    This is what the market sees.

The gap between the two is the exploit. The cleanest possible trade looks like:
  TW_delta > 0  AND  consensus_delta_from_their_view > 0
  → "Both sides win by their own math." Trade gets accepted; you win.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .future_pick_value import PickValueModel
from .league_config import LeagueConfig
from .valuation import ValuedPlayer


@dataclass(frozen=True)
class PlayerAsset:
    """A player asset on one side of a trade."""

    player_id: str
    name: str
    position: str
    age: int | None
    tw_value: float            # our dynasty_value
    consensus_value: float     # DP value_2qb (or 1qb for non-SF), 0 if unmapped


@dataclass(frozen=True)
class PickAsset:
    """A future rookie pick (or unmade draft pick) on one side of a trade."""

    season: str
    round: int
    tw_value: float
    consensus_value: float
    original_roster_id: int | None = None
    label: str = ""               # e.g. "2027 R2 (via JimMack)"


Asset = PlayerAsset | PickAsset


@dataclass
class TradeEval:
    # Inputs
    give: list[Asset]
    get: list[Asset]
    # TW (your model)
    give_tw_total: float
    get_tw_total: float
    tw_delta: float
    # Consensus (their model)
    give_consensus_total: float
    get_consensus_total: float
    consensus_delta_for_you: float        # positive = good for you in consensus
    consensus_delta_for_them: float       # = -consensus_delta_for_you, surfaced for clarity
    # Verdict (two axes)
    verdict: str = "fair"                 # 'steal' | 'win' | 'fair' | 'loss' | 'pass'
    their_response: str = "neutral"       # 'accept' | 'counter' | 'reject'
    combined: str = "fair"                # human-readable combined verdict
    notes: list[str] = field(default_factory=list)


# Thresholds (in TW-value units, calibrated against ~1300 = top-tier player).
THRESH_STEAL = 200.0   # +200 TW = steal
THRESH_WIN = 80.0      # +80 to +200 = clean win
THRESH_LOSS = -80.0    # -80 to -200 = loss
THRESH_PASS = -200.0   # worse than -200 = hard pass

# Their-side consensus thresholds (DP units, ~10000 = top player).
# A real manager rarely accepts even a "break-even" trade — sunk-cost bias on already-drafted
# players means they want to clearly gain to part with someone they targeted. Tightened from
# the original -300 to -100 to reflect that.
THEIR_ACCEPT = -100.0     # consensus delta for them ≥ -100 = they'll likely accept
THEIR_COUNTER = -1000.0   # -1000 to -100 = they'll counter
# Anything below THEIR_COUNTER = reject


def evaluate_trade(
    *,
    give: list[Asset],
    get: list[Asset],
    cfg: LeagueConfig,
    my_roster_id: int,
) -> TradeEval:
    """Score a proposed trade from the perspective of `my_roster_id` (the giver of `give`)."""
    notes: list[str] = []

    # Totals
    give_tw = sum(a.tw_value for a in give)
    get_tw = sum(a.tw_value for a in get)
    give_cons = sum(a.consensus_value for a in give)
    get_cons = sum(a.consensus_value for a in get)

    tw_delta = get_tw - give_tw
    cons_delta_for_you = get_cons - give_cons
    cons_delta_for_them = -cons_delta_for_you

    # Two-axis verdict: how it scores for you (TW) AND how the other side will read it (consensus).
    verdict = _your_verdict(tw_delta)
    their_response = _their_response(cons_delta_for_them)
    combined = _combine(verdict, their_response)

    if verdict == "steal" and their_response == "accept":
        notes.append("Steal — you win in TW and they think they win in consensus. Send it.")
    elif verdict in ("win", "steal") and their_response == "accept":
        notes.append("Likely accepted. Both sides 'win' by their own model — that's the exploit.")
    elif verdict in ("win", "steal") and their_response == "counter":
        notes.append("Strong for you but they'll likely counter — be ready to sweeten by ~$1-2 of consensus value.")
    elif verdict in ("win", "steal") and their_response == "reject":
        notes.append("Looks great for you but their consensus loss is too steep — they'll reject.")
    elif verdict == "fair" and their_response == "accept":
        notes.append("Value-aligned swap. Worth offering for roster-fit reasons.")
    elif verdict == "fair":
        notes.append("Roughly even in TW; depends on whether they see it the same way.")
    elif verdict in ("loss", "pass"):
        notes.append("You lose value in TW. Only accept if there's a specific roster need.")

    return TradeEval(
        give=give,
        get=get,
        give_tw_total=round(give_tw, 1),
        get_tw_total=round(get_tw, 1),
        tw_delta=round(tw_delta, 1),
        give_consensus_total=round(give_cons, 1),
        get_consensus_total=round(get_cons, 1),
        consensus_delta_for_you=round(cons_delta_for_you, 1),
        consensus_delta_for_them=round(cons_delta_for_them, 1),
        verdict=verdict,
        their_response=their_response,
        combined=combined,
        notes=notes,
    )


def _your_verdict(tw_delta: float) -> str:
    if tw_delta >= THRESH_STEAL: return "steal"
    if tw_delta >= THRESH_WIN: return "win"
    if abs(tw_delta) < THRESH_WIN: return "fair"
    if tw_delta > THRESH_PASS: return "loss"
    return "pass"


def _their_response(cons_delta_for_them: float) -> str:
    if cons_delta_for_them >= THEIR_ACCEPT: return "accept"
    if cons_delta_for_them >= THEIR_COUNTER: return "counter"
    return "reject"


def _combine(your: str, theirs: str) -> str:
    """Human-readable combined verdict."""
    if your in ("steal", "win") and theirs == "accept":
        return "SEND IT" if your == "steal" else "SEND"
    if your in ("steal", "win") and theirs == "counter":
        return "OFFER (expect counter)"
    if your in ("steal", "win") and theirs == "reject":
        return "TOO RICH — they'll reject"
    if your == "fair" and theirs == "accept":
        return "FAIR SWAP"
    if your == "fair":
        return "FAIR (for you)"
    if your in ("loss", "pass"):
        return "PASS"
    return "?"


# ---------------------------------------------------------------------------
# Asset builders — convenience for the UI


def player_asset(player_id: str, valued: list[ValuedPlayer]) -> PlayerAsset | None:
    v = next((x for x in valued if x.player_id == player_id), None)
    if v is None:
        return None
    return PlayerAsset(
        player_id=v.player_id,
        name=v.name,
        position=v.position,
        age=v.age,
        tw_value=v.dynasty_value,
        consensus_value=float(v.dp_value_2qb or v.dp_value_1qb or 0.0),
    )


def pick_asset(
    season: str,
    round_no: int,
    pick_model: PickValueModel,
    *,
    original_roster_id: int | None = None,
    label: str | None = None,
) -> PickAsset | None:
    pv = pick_model.get(season, round_no)
    if pv is None:
        return None
    return PickAsset(
        season=season,
        round=round_no,
        tw_value=pv.tw_value,
        consensus_value=pv.consensus_value,
        original_roster_id=original_roster_id,
        label=label or f"{season} R{round_no}",
    )


# ---------------------------------------------------------------------------
# Sleeper-chat message generator


def build_offer_message(
    eval_result: TradeEval,
    your_handle: str,
    their_handle: str,
    *,
    max_chars: int = 280,
) -> str:
    """Plain-text Sleeper-chat-ready offer message.

    Never auto-sent. Caller is responsible for copying it into Sleeper.
    """
    give_short = ", ".join(_short(a) for a in eval_result.give)
    get_short = ", ".join(_short(a) for a in eval_result.get)
    msg = (
        f"@{their_handle} — proposed trade:\n"
        f"  You get: {give_short}\n"
        f"  I get:   {get_short}\n"
        "Lmk what you think."
    )
    if len(msg) > max_chars:
        # trim
        msg = msg[: max_chars - 1] + "…"
    return msg


def _short(a: Asset) -> str:
    if isinstance(a, PlayerAsset):
        return f"{a.name} ({a.position})"
    if isinstance(a, PickAsset):
        return a.label or f"{a.season} R{a.round}"
    return "?"
