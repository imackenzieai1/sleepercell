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
