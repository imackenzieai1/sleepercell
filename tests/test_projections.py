"""Tests for src/projections.py.

The headline assertion: applying Trade Whores scoring to Joe Burrow's projected stats
should produce ~577 points (we verified this against the live API). Off-by-a-few is
fine; off-by-100 means we broke the formula.
"""
from __future__ import annotations

from src.league_config import LeagueConfig
from src.projections import build_projection_index, score_stats, explain_scoring_components


def _trade_whores_cfg() -> LeagueConfig:
    return LeagueConfig.from_sleeper_league({
        "league_id": "L", "name": "TW", "season": "2026", "total_rosters": 12,
        "roster_positions": ["QB","RB","RB","WR","WR","WR","TE","FLEX","FLEX","FLEX","FLEX","SUPER_FLEX"],
        "scoring_settings": {
            "pass_td": 6.0, "pass_yd": 0.04, "pass_cmp": 0.25, "pass_fd": 0.25,
            "rush_yd": 0.1, "rush_td": 6.0, "rush_att": 0.1, "rush_fd": 0.25,
            "rec": 1.0, "rec_yd": 0.1, "rec_td": 6.0, "rec_fd": 0.25,
            "bonus_rec_te": 1.0, "bonus_rec_rb": 0.25,
            "fum_lost": -1.0, "pass_int": -1.0,
        },
        "settings": {"best_ball": 1},
    })


def test_burrow_trade_whores_scoring() -> None:
    # Stat line pulled from api.sleeper.app/projections/nfl/player/6770 (season=2026).
    stats = {
        "pass_yd": 4008.0, "pass_td": 33.0, "pass_cmp": 377.0, "pass_att": 549.0,
        "pass_fd": 400.8, "pass_int": 10.0,
        "rush_yd": 138.0, "rush_td": 2.0, "rush_att": 46.0, "rush_fd": 13.8,
        "fum_lost": 3.0,
    }
    cfg = _trade_whores_cfg()
    pts = score_stats(stats, cfg)
    # Hand calc: 4008*.04 + 33*6 + 377*.25 + 549? no, pass_att not scored.
    # 4008*.04=160.32 + 33*6=198 + 377*.25=94.25 + 400.8*.25=100.2 - 10 + 138*.1=13.8 + 2*6=12 + 46*.1=4.6 + 13.8*.25=3.45 - 3
    # = 160.32+198+94.25+100.2-10+13.8+12+4.6+3.45-3 = 573.62
    assert 570 <= pts <= 580, f"expected ~574, got {pts}"


def test_te_premium_inflation() -> None:
    """A TE with 80 catches should get +80 from bonus_rec_te in this league."""
    cfg = _trade_whores_cfg()
    base_stats = {"rec": 80.0, "rec_yd": 800.0, "rec_td": 5.0, "rec_fd": 30.0}
    # Without TE premium: 80 + 80 + 30 + 7.5 = 197.5
    # With TE premium (+1/rec on top): 197.5 + 80 = 277.5
    pts = score_stats({**base_stats, "bonus_rec_te": 80.0}, cfg)
    pts_no_tep = score_stats({**base_stats, "bonus_rec_te": 0.0}, cfg)
    assert pts - pts_no_tep == 80.0


def test_build_projection_index_filters_positions() -> None:
    raw = [
        {"player_id": "1", "player": {"position": "QB", "first_name": "A", "last_name": "B"}, "stats": {"pass_yd": 1000, "pts_ppr": 100}},
        {"player_id": "2", "player": {"position": "K", "first_name": "X", "last_name": "Y"}, "stats": {"fgm": 20, "pts_ppr": 80}},
        {"player_id": "3", "player": {"position": "WR", "first_name": "C", "last_name": "D"}, "stats": {"rec": 50, "pts_ppr": 120}},
    ]
    idx = build_projection_index(raw, _trade_whores_cfg())
    assert set(idx.keys()) == {"1", "3"}
    assert idx["1"].position == "QB"
    assert idx["1"].league_points > 0


def test_explain_scoring_components_sorted_by_magnitude() -> None:
    stats = {"pass_yd": 4000.0, "pass_td": 30.0, "pass_int": 12.0, "pass_cmp": 350.0, "pass_fd": 400.0}
    comps = explain_scoring_components(stats, _trade_whores_cfg(), top_n=5)
    # First contribution should be the largest by absolute magnitude
    assert abs(comps[0][1]) >= abs(comps[1][1]) >= abs(comps[2][1])
