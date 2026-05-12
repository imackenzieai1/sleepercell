"""Tests for src/league_config.py."""
from __future__ import annotations

import pytest

from src.league_config import LeagueConfig


def _trade_whores_league() -> dict:
    return {
        "league_id": "1354245085549592576",
        "name": "Trade Whores",
        "season": "2026",
        "total_rosters": 12,
        "roster_positions": [
            "QB", "RB", "RB", "WR", "WR", "WR", "TE",
            "FLEX", "FLEX", "FLEX", "FLEX", "SUPER_FLEX",
            *["BN"] * 16,
        ],
        "scoring_settings": {
            "pass_td": 6.0,
            "pass_yd": 0.04,
            "pass_cmp": 0.25,
            "pass_fd": 0.25,
            "rush_yd": 0.1,
            "rush_td": 6.0,
            "rush_att": 0.1,
            "rush_fd": 0.25,
            "rec": 1.0,
            "rec_yd": 0.1,
            "rec_td": 6.0,
            "rec_fd": 0.25,
            "bonus_rec_te": 1.0,
            "bonus_rec_rb": 0.25,
            "fum_lost": -1.0,
            "pass_int": -1.0,
        },
        "settings": {"best_ball": 1},
    }


def test_from_sleeper_league_basic() -> None:
    cfg = LeagueConfig.from_sleeper_league(_trade_whores_league())
    assert cfg.league_id == "1354245085549592576"
    assert cfg.teams == 12
    assert cfg.superflex is True
    assert cfg.te_premium is True
    assert cfg.best_ball is True
    assert cfg.pass_td_value == 6.0
    assert cfg.completion_bonus == 0.25
    assert cfg.scoring["pass_fd"] == 0.25


def test_starter_counts() -> None:
    cfg = LeagueConfig.from_sleeper_league(_trade_whores_league())
    starters = cfg.starters_by_position()
    assert starters == {"QB": 1, "RB": 2, "WR": 3, "TE": 1}
    assert cfg.flex_count() == 4
    assert cfg.superflex_count() == 1
    assert cfg.bench_count() == 16


def test_bylaws_overlay() -> None:
    cfg = LeagueConfig.from_sleeper_league(
        _trade_whores_league(),
        bylaws={"third_round_reversal": True},
    )
    assert cfg.third_round_reversal is True


def test_invalid_strategy_rejected() -> None:
    with pytest.raises(Exception):
        LeagueConfig.from_sleeper_league(_trade_whores_league(), strategy="hodl")
