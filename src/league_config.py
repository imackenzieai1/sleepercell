"""LeagueConfig — single source of truth for one league's rules.

Sleeper's `/v1/league/{id}` response gives us scoring_settings and roster_positions
verbatim. That covers everything inside Sleeper's UI. But dynasty leagues
typically have rules that LIVE OUTSIDE SLEEPER — third-round reversal,
custom keeper economies, etc. Those go into the `bylaws` overlay.

The point of this module: load the Sleeper league, layer the bylaws on top, hand
the result to every other module so they don't have to re-derive anything.

Conventions:
- All scoring is loaded keyed by Sleeper's stat names (pass_yd, pass_td, ...). This
  matches both the league response AND the projection endpoint, so applying scoring
  to projections is a single dot-product.
- `format` is the league type for value-curve switching: "superflex" or "1qb".
- `te_premium` is a boolean derived from `bonus_rec_te > 0`, with the raw bonus
  preserved in scoring_settings for actual math.
- `strategy` is the per-user knob (Compete/Balanced/Rebuild) that affects how dynasty
  values weight age curves. NOT a league rule — but it's the user's lens on the league
  and travels with the config.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from pydantic import BaseModel, Field, field_validator


Strategy = str  # "compete" | "balanced" | "rebuild"


class LeagueConfig(BaseModel):
    """Everything one league's logic needs in a single object.

    Loaded once at app boot via `from_sleeper_league()`, then passed everywhere.
    """

    # Identity ---------------------------------------------------------------
    league_id: str
    draft_id: str | None = None
    name: str
    season: str

    # Structure --------------------------------------------------------------
    teams: int = 12
    roster_positions: list[str] = Field(default_factory=list)
    """As returned by Sleeper: ['QB','RB','RB','WR','WR','WR','TE','FLEX','FLEX','FLEX','FLEX','SUPER_FLEX','BN',...]"""

    # Scoring ----------------------------------------------------------------
    scoring: dict[str, float] = Field(default_factory=dict)
    """Full scoring_settings dict from Sleeper. Keys match stat names from projections endpoint."""

    # Format flags (derived for fast lookup) ---------------------------------
    superflex: bool = False
    te_premium: bool = False
    best_ball: bool = False
    no_k_dst: bool = True
    pass_td_value: float = 4.0       # convenience for callers; equals scoring['pass_td']
    completion_bonus: float = 0.0    # equals scoring['pass_cmp']

    # Bylaws (outside Sleeper) -----------------------------------------------
    third_round_reversal: bool = False
    """3RR — round 3 mirrors round 2 (reverse) instead of snaking back."""

    # Strategy lens (per-user, not a league rule) ----------------------------
    strategy: Strategy = "balanced"
    age_horizon_years: int = 4
    """How many seasons to project forward for dynasty value.
    Compete builds discount older horizons less; rebuilds discount the near term."""

    # Raw artifacts (kept for debugging / future modules) --------------------
    raw_league: dict[str, Any] | None = None
    raw_draft: dict[str, Any] | None = None

    @field_validator("strategy")
    @classmethod
    def _norm_strategy(cls, v: str) -> str:
        v = (v or "balanced").lower()
        if v not in {"compete", "balanced", "rebuild"}:
            raise ValueError(f"strategy must be compete|balanced|rebuild, got {v!r}")
        return v

    # ---------------------------------------------------------------- factory

    @classmethod
    def from_sleeper_league(
        cls,
        league: dict[str, Any],
        *,
        draft: dict[str, Any] | None = None,
        bylaws: dict[str, Any] | None = None,
        strategy: Strategy = "balanced",
    ) -> "LeagueConfig":
        """Build a LeagueConfig from the raw Sleeper league response.

        Args:
            league: response from /v1/league/{id}.
            draft: optional response from /v1/draft/{id}.
            bylaws: optional dict of out-of-Sleeper rules. Keys recognized:
                'third_round_reversal' (bool).
            strategy: per-user lens.
        """
        scoring = dict(league.get("scoring_settings") or {})
        rp = list(league.get("roster_positions") or [])
        bylaws = bylaws or {}

        return cls(
            league_id=str(league["league_id"]),
            draft_id=str(draft["draft_id"]) if draft else None,
            name=league.get("name") or "Unnamed League",
            season=str(league.get("season") or ""),
            teams=int(league.get("total_rosters") or 12),
            roster_positions=rp,
            scoring=scoring,
            superflex="SUPER_FLEX" in rp,
            te_premium=float(scoring.get("bonus_rec_te") or 0) > 0,
            best_ball=bool((league.get("settings") or {}).get("best_ball")),
            no_k_dst=not any(p in rp for p in ("K", "DEF")),
            pass_td_value=float(scoring.get("pass_td") or 4.0),
            completion_bonus=float(scoring.get("pass_cmp") or 0.0),
            third_round_reversal=bool(bylaws.get("third_round_reversal", False)),
            strategy=strategy,
            raw_league=league,
            raw_draft=draft,
        )

    # ------------------------------------------------------------- helpers

    def starters_by_position(self) -> dict[str, int]:
        """Count REQUIRED starters per position (excluding flex/SF/bench)."""
        out: dict[str, int] = {}
        for slot in self.roster_positions:
            if slot in {"BN", "TAXI", "IR", "FLEX", "SUPER_FLEX", "WRRB_FLEX", "REC_FLEX"}:
                continue
            out[slot] = out.get(slot, 0) + 1
        return out

    def flex_count(self) -> int:
        return sum(1 for s in self.roster_positions if s in {"FLEX", "WRRB_FLEX", "REC_FLEX"})

    def superflex_count(self) -> int:
        return sum(1 for s in self.roster_positions if s == "SUPER_FLEX")

    def bench_count(self) -> int:
        return sum(1 for s in self.roster_positions if s == "BN")
