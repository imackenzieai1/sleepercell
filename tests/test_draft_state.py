"""Tests for src/draft_state.py — especially the 3RR snake math."""
from __future__ import annotations

import pytest

from src.draft_state import DraftState
from src.league_config import LeagueConfig


def _cfg(*, third_round_reversal: bool) -> LeagueConfig:
    return LeagueConfig.from_sleeper_league(
        {
            "league_id": "L", "name": "T", "season": "2026", "total_rosters": 12,
            "roster_positions": ["QB", "RB"],
            "scoring_settings": {"pass_td": 4.0},
        },
        bylaws={"third_round_reversal": third_round_reversal},
    )


def _draft(rounds: int = 28) -> dict:
    """Minimal draft response."""
    return {
        "draft_id": "D",
        "status": "drafting",
        "settings": {"rounds": rounds},
        "slot_to_roster_id": {str(i): i for i in range(1, 13)},  # slot N owned by roster N
    }


def test_snake_no_3rr_slot12_picks() -> None:
    state = DraftState(cfg=_cfg(third_round_reversal=False), draft=_draft()).build()
    # Without 3RR for slot 12 (last in R1): picks 12, 13, 36, 37, 60, 61, ...
    picks = [sp.pick_no for sp in state.all_my_picks(12)]
    assert picks[:6] == [12, 13, 36, 37, 60, 61]


def test_snake_3rr_slot12_picks() -> None:
    state = DraftState(cfg=_cfg(third_round_reversal=True), draft=_draft()).build()
    # With 3RR for slot 12: picks 12, 13, 25, 48, 49, 72, 73, ...
    picks = [sp.pick_no for sp in state.all_my_picks(12)]
    assert picks[:7] == [12, 13, 25, 48, 49, 72, 73]


def test_3rr_slot1_picks() -> None:
    state = DraftState(cfg=_cfg(third_round_reversal=True), draft=_draft()).build()
    # Slot 1 with 3RR: 1, 24, 36, 37, 60, 61, 84, ...
    # R1: pick 1
    # R2: pick 24 (last of R2 since reversed)
    # R3: pick 36 (last of R3 since reversed AGAIN, slot 1 picks 12th in reversed order)
    # R4: pick 37 (R4 forward, slot 1 picks first)
    # R5: pick 60 (R5 reverse, slot 1 last)
    picks = [sp.pick_no for sp in state.all_my_picks(1)]
    assert picks[:5] == [1, 24, 36, 37, 60]


def test_picks_made_propagate_to_schedule() -> None:
    state = DraftState(
        cfg=_cfg(third_round_reversal=False),
        draft=_draft(),
        picks=[
            {"pick_no": 1, "roster_id": 1, "player_id": "p1"},
            {"pick_no": 2, "roster_id": 2, "player_id": "p2"},
        ],
    ).build()
    made = [sp for sp in state.schedule if sp.made]
    assert len(made) == 2
    assert state.drafted_ids() == {"p1", "p2"}
    assert state.on_clock().pick_no == 3


def test_traded_pick_changes_ownership() -> None:
    state = DraftState(
        cfg=_cfg(third_round_reversal=False),
        draft=_draft(),
        draft_traded_picks=[
            {"round": 1, "roster_id": 5, "previous_owner_id": 5, "owner_id": 1},
        ],
    ).build()
    # Pick 5 (R1.5, originally roster 5) should now be owned by roster 1
    pick5 = next(sp for sp in state.schedule if sp.pick_no == 5)
    assert pick5.orig_roster_id == 5
    assert pick5.owner_roster_id == 1
    assert pick5.is_traded


def test_future_pick_inventory_basic() -> None:
    state = DraftState(
        cfg=_cfg(third_round_reversal=False),
        draft=_draft(),
        league_traded_picks=[
            {"season": "2027", "round": 1, "roster_id": 5, "previous_owner_id": 5, "owner_id": 1},
        ],
    ).build()
    inv = state.future_pick_inventory("2027", rounds=4)
    # Roster 5 should have lost their R1; roster 1 should have an extra R1
    roster5 = inv.get(5, [])
    assert not any(p["round"] == 1 for p in roster5)
    roster1 = inv[1]
    assert sum(1 for p in roster1 if p["round"] == 1) == 2  # their own + the acquired one
