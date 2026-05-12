"""Tests for src/tier_engine.py."""
from __future__ import annotations

from src.tier_engine import detect_tiers, tier_cliff_alerts
from src.valuation import ValuedPlayer


def _vp(pid: str, pos: str, value: float, rank: int = 0) -> ValuedPlayer:
    return ValuedPlayer(
        player_id=pid, name=f"Player {pid}", position=pos, team=None, age=25,
        season_points=value, dynasty_value=value, overall_rank=rank, position_rank=rank,
    )


def test_tiers_form_on_gaps() -> None:
    """A clear cliff between two clusters should produce two tiers."""
    players = [
        _vp("a", "RB", 1000),
        _vp("b", "RB", 950),
        _vp("c", "RB", 920),
        _vp("d", "RB", 600),  # 320-pt cliff
        _vp("e", "RB", 580),
        _vp("f", "RB", 550),
    ]
    tiers = detect_tiers(players, gap_multiplier=1.5)
    rb_tiers = tiers["RB"]
    assert len(rb_tiers) == 2
    assert {p.player_id for p in rb_tiers[0].players} == {"a", "b", "c"}
    assert {p.player_id for p in rb_tiers[1].players} == {"d", "e", "f"}


def test_no_tier_split_on_smooth_values() -> None:
    """Smoothly declining values shouldn't produce many tiers."""
    players = [_vp(f"p{i}", "QB", 1000 - i * 10) for i in range(15)]
    tiers = detect_tiers(players, gap_multiplier=1.5)
    # All gaps are equal, so no tier breaks fire
    assert len(tiers["QB"]) == 1


def test_cliff_alert_imminent() -> None:
    players = [
        _vp("a", "WR", 800), _vp("b", "WR", 780),  # tier 1 (2 players)
        _vp("c", "WR", 400), _vp("d", "WR", 380),  # tier 2 (2 players, big gap)
    ]
    tiers = detect_tiers(players, gap_multiplier=1.5)
    # Mark one player from tier 1 drafted → 1 left → imminent alert
    alerts = tier_cliff_alerts(tiers, drafted_ids={"a"}, threshold=2)
    imminents = [a for a in alerts if a.severity == "imminent" and a.position == "WR"]
    assert len(imminents) == 1
    assert imminents[0].remaining == 1
