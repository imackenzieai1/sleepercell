"""Tests for src/valuation.py — composite player valuation."""
from __future__ import annotations

from dataclasses import dataclass

from src.league_config import LeagueConfig
from src.projections import PlayerProjection
from src.valuation import value_population


@dataclass
class _StubIndex:
    """Minimal stand-in for PlayerIndex that just returns None records.

    The valuation module only uses index.resolve() for age + DP cross-walk.
    Tests don't need real DP data — we pass age via PlayerProjection directly.
    """

    def resolve(self, pid: str):
        return None


def _cfg(strategy: str = "balanced", sf: bool = True) -> LeagueConfig:
    return LeagueConfig.from_sleeper_league(
        {
            "league_id": "L", "name": "T", "season": "2026", "total_rosters": 12,
            "roster_positions": (["QB", "RB", "RB", "WR", "WR", "WR", "TE",
                                  "FLEX", "FLEX", "FLEX", "FLEX"]
                                 + (["SUPER_FLEX"] if sf else [])),
            "scoring_settings": {"pass_td": 6.0, "rush_td": 6.0, "rec_td": 6.0, "rec": 1.0, "rec_yd": 0.1},
        },
        strategy=strategy,
    )


def _proj(pid: str, pos: str, pts: float, age: int) -> PlayerProjection:
    return PlayerProjection(
        player_id=pid, name=f"P{pid}", position=pos, team=None,
        age=age, years_exp=2, stats={}, league_points=pts, sleeper_pts_ppr=pts,
        last_modified_ms=None, company=None,
    )


def test_ranks_are_assigned_correctly() -> None:
    projs = {
        "1": _proj("1", "QB", 400, age=25),
        "2": _proj("2", "QB", 350, age=27),
        "3": _proj("3", "RB", 250, age=24),
    }
    valued = value_population(projs, _StubIndex(), _cfg())
    assert valued[0].player_id == "1"
    assert valued[0].overall_rank == 1
    assert valued[0].position_rank == 1
    assert valued[1].player_id == "2"
    assert valued[1].overall_rank == 2
    assert valued[1].position_rank == 2
    assert valued[2].player_id == "3"
    assert valued[2].position_rank == 1  # first RB


def test_strategy_widens_old_vs_young_gap() -> None:
    """Same season points, old vs young RB: rebuild should value young more heavily than compete does.

    Note: the RB age cliff is so steep that BOTH strategies prefer young; we're testing that
    the *gap* widens under rebuild (which is what the strategy weights are supposed to do).
    """
    projs = {
        "old_rb":   _proj("old_rb",   "RB", 220, age=30),
        "young_rb": _proj("young_rb", "RB", 220, age=23),
    }
    compete = value_population(projs, _StubIndex(), _cfg(strategy="compete"))
    rebuild = value_population(projs, _StubIndex(), _cfg(strategy="rebuild"))
    old_c   = next(v for v in compete if v.player_id == "old_rb")
    young_c = next(v for v in compete if v.player_id == "young_rb")
    old_r   = next(v for v in rebuild if v.player_id == "old_rb")
    young_r = next(v for v in rebuild if v.player_id == "young_rb")
    gap_compete = young_c.dynasty_value - old_c.dynasty_value
    gap_rebuild = young_r.dynasty_value - old_r.dynasty_value
    assert gap_rebuild > gap_compete


def test_replacement_delta_computed() -> None:
    projs = {f"q{i}": _proj(f"q{i}", "QB", 400 - i * 10, age=25) for i in range(30)}
    valued = value_population(projs, _StubIndex(), _cfg(sf=True))
    # QB24 is the replacement in SF (default). QB1's replacement_delta should be positive.
    qb1 = next(v for v in valued if v.player_id == "q0")
    assert qb1.replacement_delta > 0
    # The very last QB's delta should be near zero or negative
    qb_last = next(v for v in valued if v.player_id == "q29")
    assert qb_last.replacement_delta <= qb1.replacement_delta
