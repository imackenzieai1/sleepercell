"""Sleeper player_id → FantasyPros id → DynastyProcess values cross-walk.

Why this module exists
- Sleeper's API returns player_id as a string of digits (e.g. "6770" for Joe Burrow).
- DynastyProcess's db_playerids.csv has a `sleeper_id` column we can join on.
- But ~30% of dynasty-relevant entries have `sleeper_id = NA` (rookies pre-NFL-draft,
  CFB devy prospects, retired players whose IDs never landed in DP's bridge).
- For those misses, we fall back to fuzzy name matching using `merge_name`
  (lowercased, punctuation-stripped) + position + team.

What this module provides
- `PlayerIndex(...)` — built once at app boot from Sleeper players_nfl + DP CSVs.
- `resolve(player_id) -> PlayerRecord` — the canonical bridge call.
- `attach_values(projections_index)` — given a dict of PlayerProjections, return a
  parallel dict of records that include DP value_1qb and value_2qb. Used as a
  cross-check on our league-adjusted projections.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import pandas as pd
from rapidfuzz import fuzz, process

from .data_layer import DataLayer


@dataclass
class PlayerRecord:
    """Resolved record combining Sleeper, DP cross-walk, and DP values."""

    sleeper_id: str
    name: str
    position: str | None
    team: str | None
    age: int | None
    fantasypros_id: int | None = None
    ktc_id: int | None = None
    mfl_id: int | None = None
    yahoo_id: int | None = None
    espn_id: int | None = None
    # Dynasty values (from DP). May be None for unmapped rookies.
    dp_value_1qb: float | None = None
    dp_value_2qb: float | None = None
    dp_ecr_1qb: float | None = None
    dp_ecr_2qb: float | None = None
    # How we matched
    match_method: str = "unmatched"  # "sleeper_id" | "fuzzy_name" | "unmatched"


def _normalize_name(s: str | None) -> str:
    if not s:
        return ""
    return "".join(ch.lower() for ch in s if ch.isalnum())


@dataclass
class PlayerIndex:
    sleeper_players: dict[str, dict]
    """Raw players map from /v1/players/nfl."""
    dp: DataLayer
    """Data layer for DP CSV access."""

    # Optional: cap fuzzy-match work for performance. None = full pass.
    fuzzy_score_cutoff: int = 92
    fuzzy_enabled: bool = True

    # Built on first call
    _records: dict[str, PlayerRecord] = field(default_factory=dict)
    _built: bool = False

    def build(self) -> "PlayerIndex":
        """Resolve every Sleeper skill-position player. Idempotent.

        Fuzzy matching is per-position-bucketed for speed: a Sleeper QB only fuzzy-matches
        against DP QB rows, not against 12,000+ random names. That cuts work ~10× vs naive
        per-name extractOne.
        """
        if self._built:
            return self

        ids_df = self.dp.player_ids()
        values_df = self.dp.values()

        # Join cross-walk -> values on fp_id <-> fantasypros_id.
        ids_df["fantasypros_id"] = pd.to_numeric(ids_df["fantasypros_id"], errors="coerce").astype("Int64")
        values_df["fp_id"] = pd.to_numeric(values_df.get("fp_id"), errors="coerce").astype("Int64")

        merged = ids_df.merge(
            values_df[["fp_id", "value_1qb", "value_2qb", "ecr_1qb", "ecr_2qb"]],
            left_on="fantasypros_id",
            right_on="fp_id",
            how="left",
        )

        # Direct sleeper_id lookup (the vast majority of matches)
        by_sleeper_id: dict[str, pd.Series] = {}
        for _, row in merged.iterrows():
            sid = str(row.get("sleeper_id") or "")
            if sid and sid != "<NA>":
                by_sleeper_id[sid] = row

        # Per-position fuzzy buckets — only DP rows that have a value AND are skill-position.
        # Without value, fuzzy-matching is pointless (no DP data to attach).
        SKILL = {"QB", "RB", "WR", "TE"}
        fuzzy_buckets: dict[str, tuple[list[str], list[pd.Series]]] = {p: ([], []) for p in SKILL}
        if self.fuzzy_enabled:
            for _, row in merged.iterrows():
                pos = row.get("position")
                if pos not in SKILL:
                    continue
                # Keep only rows that actually carry a DP value — otherwise no point in matching.
                if pd.isna(row.get("value_2qb")) and pd.isna(row.get("value_1qb")):
                    continue
                mn = _normalize_name(row.get("merge_name") or row.get("name"))
                if mn:
                    names, rows = fuzzy_buckets[pos]
                    names.append(mn)
                    rows.append(row)

        # Resolve every Sleeper skill-position player.
        for pid, p in self.sleeper_players.items():
            if not isinstance(p, dict):
                continue
            position = p.get("position")
            if position not in SKILL:
                continue
            full_name = p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip()
            rec = PlayerRecord(
                sleeper_id=str(pid),
                name=full_name,
                position=position,
                team=p.get("team"),
                age=p.get("age"),
            )

            # 1) Direct sleeper_id match — covers the vast majority.
            row = by_sleeper_id.get(str(pid))
            if row is not None:
                rec.match_method = "sleeper_id"
                self._attach_row(rec, row)
                self._records[str(pid)] = rec
                continue

            # 2) Fuzzy fallback — only against same-position DP rows with a value.
            if self.fuzzy_enabled:
                names, rows = fuzzy_buckets[position]
                if names:
                    target = _normalize_name(full_name)
                    if target:
                        best = process.extractOne(
                            target, names, scorer=fuzz.WRatio, score_cutoff=self.fuzzy_score_cutoff
                        )
                        if best is not None:
                            _, _score, idx = best
                            rec.match_method = "fuzzy_name"
                            self._attach_row(rec, rows[idx])

            self._records[str(pid)] = rec

        self._built = True
        return self

    @staticmethod
    def _attach_row(rec: PlayerRecord, row: pd.Series) -> None:
        def _int(v) -> int | None:
            try:
                return int(v) if pd.notna(v) else None
            except (TypeError, ValueError):
                return None

        def _float(v) -> float | None:
            try:
                return float(v) if pd.notna(v) else None
            except (TypeError, ValueError):
                return None

        rec.fantasypros_id = _int(row.get("fantasypros_id"))
        rec.ktc_id = _int(row.get("ktc_id"))
        rec.mfl_id = _int(row.get("mfl_id"))
        rec.yahoo_id = _int(row.get("yahoo_id"))
        rec.espn_id = _int(row.get("espn_id"))
        rec.dp_value_1qb = _float(row.get("value_1qb"))
        rec.dp_value_2qb = _float(row.get("value_2qb"))
        rec.dp_ecr_1qb = _float(row.get("ecr_1qb"))
        rec.dp_ecr_2qb = _float(row.get("ecr_2qb"))

    def resolve(self, sleeper_id: str) -> PlayerRecord | None:
        if not self._built:
            self.build()
        return self._records.get(str(sleeper_id))

    def all(self) -> Iterable[PlayerRecord]:
        if not self._built:
            self.build()
        return self._records.values()

    def coverage(self) -> dict[str, int]:
        """Stats for the audit UI: how many players matched by each method."""
        if not self._built:
            self.build()
        counts = {"sleeper_id": 0, "fuzzy_name": 0, "unmatched": 0}
        for r in self._records.values():
            counts[r.match_method] = counts.get(r.match_method, 0) + 1
        return counts
