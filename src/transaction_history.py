"""Parse Sleeper transaction history into structured trade records.

Sleeper's `/transactions/{week}` endpoint returns raw events. Pre-season and
draft trades land in week 1 for most leagues. Each trade has:
  - roster_ids — who was involved
  - draft_picks — bundled picks moved (with originator + previous + current owner)
  - adds / drops — players moved (sparse for pick-only trades)
  - created — millis timestamp
  - status — usually 'complete'

This module groups those into clean "give-X / get-Y per side" records that the
UI and recommender can both use.

Why use this over `draft_traded_picks`:
  - Real trade groupings (which picks moved together as one deal)
  - Timestamps (recency)
  - Player-for-pick trades (not just pick-for-pick)
  - Counts of trade activity per manager → "willingness to deal" signal
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable


@dataclass
class TradedPick:
    season: str
    round: int
    originator_rid: int   # whose pick this originally was
    label: str = ""       # e.g. "2026 R2 (own)" or "2027 R3 (via JimMack)"


@dataclass
class TradeRecord:
    transaction_id: str
    status: str
    created_ms: int
    roster_ids: list[int]

    # Per-side bundles. Key = roster_id who RECEIVED these.
    # picks_received_by[rid] = list of picks rid got in this trade.
    picks_received_by: dict[int, list[TradedPick]] = field(default_factory=lambda: defaultdict(list))
    players_received_by: dict[int, list[str]] = field(default_factory=lambda: defaultdict(list))
    faab_received_by: dict[int, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def created_dt(self) -> datetime:
        return datetime.fromtimestamp((self.created_ms or 0) / 1000)


# ---------------------------------------------------------------------------


def parse_trades(
    transactions: Iterable[dict],
    *,
    roster_name_fn=None,
) -> list[TradeRecord]:
    """Filter to trade-type transactions and convert to TradeRecord objects."""
    rname = roster_name_fn or (lambda rid: f"roster{rid}")
    out: list[TradeRecord] = []
    for t in transactions:
        if t.get("type") != "trade":
            continue
        rec = TradeRecord(
            transaction_id=str(t.get("transaction_id") or ""),
            status=str(t.get("status") or ""),
            created_ms=int(t.get("created") or 0),
            roster_ids=list(t.get("roster_ids") or []),
        )
        # Picks
        for p in t.get("draft_picks") or []:
            orig = int(p.get("roster_id") or 0)
            owner = int(p.get("owner_id") or 0)
            season = str(p.get("season") or "")
            rnd = int(p.get("round") or 0)
            label = f"{season} R{rnd}"
            if orig and orig not in rec.roster_ids:
                label += f" (via {rname(orig)})"
            elif orig == owner:
                # The owner now is also the originator → reclaim or self-route?
                # Treat as "own" so the picker can see it cleanly.
                pass
            rec.picks_received_by[owner].append(TradedPick(
                season=season, round=rnd, originator_rid=orig, label=label,
            ))
        # Players (adds = received, drops = given on the other side; we mirror adds)
        for player_id, rid in (t.get("adds") or {}).items():
            rec.players_received_by[int(rid)].append(str(player_id))
        # FAAB
        for entry in t.get("waiver_budget") or []:
            sender_rid = int(entry.get("sender") or 0)
            receiver_rid = int(entry.get("receiver") or 0)
            amount = int(entry.get("amount") or 0)
            if receiver_rid:
                rec.faab_received_by[receiver_rid] += amount
        out.append(rec)
    return sorted(out, key=lambda r: -(r.created_ms or 0))


def activity_per_roster(trades: list[TradeRecord]) -> dict[int, int]:
    """How many trades has each roster participated in? Higher = more dealmaker."""
    counts: dict[int, int] = defaultdict(int)
    for t in trades:
        for rid in t.roster_ids:
            counts[rid] += 1
    return dict(counts)


def to_display_rows(
    trades: list[TradeRecord],
    roster_name_fn,
    *,
    player_name_fn=lambda pid: f"player {pid}",
) -> list[dict]:
    """Flatten trades into a UI-friendly table of rows.

    Each row = one trade, with each side's give/get summarized as a single string.
    """
    rows: list[dict] = []
    for t in trades:
        # For a 2-roster trade, side A gives what side B receives, and vice versa.
        sides = list(t.roster_ids)
        if len(sides) != 2:
            # multi-team trades are rare; flatten as one row but mark
            sides_label = "+".join(roster_name_fn(s) for s in sides)
            rows.append({
                "When": t.created_dt.strftime("%b %d %H:%M"),
                "Sides": sides_label,
                "Assets moved": _flatten_multiparty(t, roster_name_fn, player_name_fn),
                "Status": t.status,
            })
            continue
        a, b = sides[0], sides[1]
        a_received = _side_str(t, a, player_name_fn)
        b_received = _side_str(t, b, player_name_fn)
        rows.append({
            "When": t.created_dt.strftime("%b %d %H:%M"),
            roster_name_fn(a) + " got": a_received,
            roster_name_fn(b) + " got": b_received,
            "Status": t.status,
        })
    return rows


def to_normalized_rows(
    trades: list[TradeRecord],
    roster_name_fn,
    *,
    player_name_fn=lambda pid: f"player {pid}",
) -> list[dict]:
    """Normalized table for general use (column names are stable regardless of partners)."""
    rows: list[dict] = []
    for t in trades:
        sides = list(t.roster_ids)
        if len(sides) != 2:
            rows.append({
                "When": t.created_dt.strftime("%b %d"),
                "Side A": "+".join(roster_name_fn(s) for s in sides),
                "Side A got": _flatten_multiparty(t, roster_name_fn, player_name_fn),
                "Side B": "—",
                "Side B got": "—",
            })
            continue
        a, b = sides[0], sides[1]
        rows.append({
            "When": t.created_dt.strftime("%b %d"),
            "Side A": roster_name_fn(a),
            "Side A got": _side_str(t, a, player_name_fn),
            "Side B": roster_name_fn(b),
            "Side B got": _side_str(t, b, player_name_fn),
        })
    return rows


def _side_str(t: TradeRecord, rid: int, player_name_fn) -> str:
    bits: list[str] = []
    for pick in t.picks_received_by.get(rid, []):
        bits.append(pick.label)
    for pid in t.players_received_by.get(rid, []):
        bits.append(player_name_fn(pid))
    faab = t.faab_received_by.get(rid, 0)
    if faab:
        bits.append(f"${faab} FAAB")
    return ", ".join(bits) if bits else "(nothing)"


def _flatten_multiparty(t: TradeRecord, roster_name_fn, player_name_fn) -> str:
    parts = []
    for rid in t.roster_ids:
        parts.append(f"{roster_name_fn(rid)}: {_side_str(t, rid, player_name_fn)}")
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Trade grading — retroactive valuation of every completed trade


@dataclass
class TradeGrade:
    """Per-side grade for one completed trade."""
    roster_id: int
    received_value: float    # community-equivalent value the side received
    given_value: float       # community-equivalent value the side gave up
    delta: float             # received − given (positive = won the trade)
    grade: str               # "A+", "A", "B", "C", "D", "F"


# Grade thresholds, in community-value units. SF dynasty scale where elite player ≈ 9000.
GRADE_THRESHOLDS: list[tuple[float, str]] = [
    (1500, "A+"),
    (800,  "A"),
    (250,  "B"),
    (-250, "C"),
    (-800, "D"),
    (-1500, "F"),
]


def _grade_from_delta(delta: float) -> str:
    for threshold, label in GRADE_THRESHOLDS:
        if delta >= threshold:
            return label
    return "F"


def _player_community_value(player_id: str, valued_by_id: dict) -> float:
    """Best-available community value for a player. Same fallback chain as trade math."""
    v = valued_by_id.get(str(player_id))
    if not v:
        return 0.0
    return float(
        getattr(v, "adjusted_ktc", None)
        or getattr(v, "ktc_value", None)
        or getattr(v, "dp_value_2qb", None)
        or getattr(v, "dp_value_1qb", None)
        or 0.0
    )


def _pick_community_value(season: str, round_no: int, originator_rid: int, *,
                          pick_model, state, valued_by_id) -> float:
    """Best-available community value for a traded pick.

    Resolution chain:
      1. Future-year picks (2027, 2028+): pick_model lookup by (season, round).
      2. Current-year (this draft) MADE picks: use the player who was drafted at that slot.
      3. Current-year UNMADE picks: take the N-th best UNDRAFTED player by community value,
         where N = pick_no − picks_already_made. Monotonic by construction.
    """
    season = str(season)
    if pick_model is not None and season != "2026":
        pv = pick_model.get(season, int(round_no))
        return float(pv.consensus_value) if pv else 0.0

    # Current draft pick — find the schedule slot
    slot = state.roster_to_slot.get(int(originator_rid)) if state else None
    if state is None or slot is None:
        if pick_model:
            pv = pick_model.get(season, int(round_no))
            return float(pv.consensus_value) if pv else 0.0
        return 0.0

    pick_no = state.pick_no_for_slot(int(round_no), int(slot))
    sp = next((s for s in state.schedule if s.pick_no == pick_no), None)
    if sp is None:
        return 0.0
    if sp.made and sp.player_id:
        return _player_community_value(sp.player_id, valued_by_id)

    # Unmade current-draft pick — value by the N-th best undrafted player by community value.
    # This matches what the trade recommender uses for unmade picks and enforces monotonicity
    # (earlier pick ≥ later pick).
    def _market(v):
        return float(
            getattr(v, "adjusted_ktc", None)
            or getattr(v, "ktc_value", None)
            or getattr(v, "dp_value_2qb", None)
            or getattr(v, "dp_value_1qb", None)
            or 0.0
        )
    drafted_ids = state.drafted_ids()
    picks_already_made = len(state.picks)
    undrafted = sorted(
        [v for v in valued_by_id.values() if getattr(v, "player_id", None)
         and v.player_id not in drafted_ids and _market(v) > 0],
        key=lambda v: -_market(v),
    )
    if not undrafted:
        # Pick-model fallback if for some reason we have no undrafted with values
        if pick_model:
            pv = pick_model.get(season, int(round_no))
            return float(pv.consensus_value) if pv else 0.0
        return 0.0
    idx = max(0, min(pick_no - picks_already_made - 1, len(undrafted) - 1))
    return _market(undrafted[idx])


def grade_trades(
    trades: list[TradeRecord],
    *,
    valued: list,
    pick_model=None,
    state=None,
) -> dict[str, dict[int, TradeGrade]]:
    """For each trade, compute per-side grades.

    Returns: {transaction_id: {roster_id: TradeGrade}}

    Asset values are in COMMUNITY-equivalent units (adjusted_ktc when loaded, else
    ktc_value, else dp_value_2qb). Picks are valued by:
      - the drafted player if the pick has already been used
      - the pick_model (community-calibrated defaults) otherwise
    """
    valued_by_id = {getattr(v, "player_id", None): v for v in valued}
    out: dict[str, dict[int, TradeGrade]] = {}

    for t in trades:
        sides: dict[int, TradeGrade] = {}
        # First pass: compute "received" value for each side
        per_rid_received: dict[int, float] = {}
        for rid in t.roster_ids:
            received = 0.0
            for pid in t.players_received_by.get(rid, []):
                received += _player_community_value(pid, valued_by_id)
            for tp in t.picks_received_by.get(rid, []):
                received += _pick_community_value(
                    tp.season, tp.round, tp.originator_rid,
                    pick_model=pick_model, state=state, valued_by_id=valued_by_id,
                )
            per_rid_received[rid] = received

        total_received = sum(per_rid_received.values())
        # For each side: given = total_received - what they received
        for rid in t.roster_ids:
            received = per_rid_received[rid]
            given = total_received - received
            delta = received - given
            sides[rid] = TradeGrade(
                roster_id=rid,
                received_value=round(received, 1),
                given_value=round(given, 1),
                delta=round(delta, 1),
                grade=_grade_from_delta(delta),
            )
        out[t.transaction_id] = sides

    return out


def to_graded_rows(
    trades: list[TradeRecord],
    roster_name_fn,
    grades: dict[str, dict[int, TradeGrade]],
    *,
    player_name_fn=lambda pid: f"player {pid}",
) -> list[dict]:
    """Build a UI-ready rows list including per-side grades.

    Columns: When · Side A · Side A got · A grade · A Δ · Side B · Side B got · B grade · B Δ
    Multi-party trades collapse to a single-row summary.
    """
    rows: list[dict] = []
    for t in trades:
        sides = list(t.roster_ids)
        side_grades = grades.get(t.transaction_id, {})
        if len(sides) != 2:
            # Multi-team summary — show all sides' grades joined
            sides_label = " | ".join(
                f"{roster_name_fn(s)} ({side_grades.get(s).grade if side_grades.get(s) else '?'})"
                for s in sides
            )
            rows.append({
                "When": t.created_dt.strftime("%b %d"),
                "Side A": sides_label,
                "Side A got": _flatten_multiparty(t, roster_name_fn, player_name_fn),
                "A grade": "—", "A Δ": "—",
                "Side B": "—", "Side B got": "—",
                "B grade": "—", "B Δ": "—",
            })
            continue
        a, b = sides[0], sides[1]
        ga = side_grades.get(a)
        gb = side_grades.get(b)
        rows.append({
            "When": t.created_dt.strftime("%b %d"),
            "Side A": roster_name_fn(a),
            "Side A got": _side_str(t, a, player_name_fn),
            "A grade": ga.grade if ga else "?",
            "A Δ": round(ga.delta) if ga else "—",
            "Side B": roster_name_fn(b),
            "Side B got": _side_str(t, b, player_name_fn),
            "B grade": gb.grade if gb else "?",
            "B Δ": round(gb.delta) if gb else "—",
        })
    return rows
