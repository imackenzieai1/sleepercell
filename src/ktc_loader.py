"""External dynasty-value loaders.

Two paths to overlay community values on top of Sleeper Cell:

1. **FantasyCalc API** (recommended, one-click) — `fetch_fantasycalc()` pulls live
   Superflex dynasty values from api.fantasycalc.com. Each row includes a
   `sleeperId`, so no fuzzy matching is needed. Free, no auth, refreshes nightly.

2. **KTC CSV upload** — `load_ktc()` reads a user-supplied CSV exported/copied from
   KeepTradeCut. Schema-tolerant; fuzzy-matches names to Sleeper IDs.

Both produce the same shape: `{sleeper_player_id: value}`. Either can be used as
the "community baseline" that the league-fit multiplier adjusts on top of.

Accepted KTC CSV schemas (case-insensitive column matching):
    - Player name column:   any of {"player", "name", "player_name", "playername", "full_name"}
    - Value column:         any of {"value", "ktc", "ktc_value", "trade_value", "value_sf", "sf_value", "superflex"}
    - Optional position:    any of {"pos", "position"}  — used to disambiguate fuzzy name matches
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

import pandas as pd
from rapidfuzz import fuzz, process

LOGGER = logging.getLogger("sleeper_cell.ktc_loader")

# Common column-name variants (case-insensitive lookup).
NAME_COLS = ("player", "name", "player_name", "playername", "full_name", "fullname")
VALUE_COLS = ("value", "ktc", "ktc_value", "trade_value", "value_sf", "sf_value",
              "superflex", "superflex_value", "ktc_sf")
POS_COLS = ("pos", "position", "fantasy_position")


@dataclass
class KTCLoadResult:
    """Mapping of sleeper_id → ktc_value, plus diagnostics."""
    by_player_id: dict[str, float] = field(default_factory=dict)
    matched_by_position_name: int = 0
    matched_by_name_only: int = 0
    unmatched: list[str] = field(default_factory=list)
    rows_read: int = 0

    @property
    def total_matched(self) -> int:
        return self.matched_by_position_name + self.matched_by_name_only

    def __bool__(self) -> bool:
        return bool(self.by_player_id)


def _normalize_name(s: str | None) -> str:
    if not s:
        return ""
    return "".join(ch.lower() for ch in s if ch.isalnum())


def _resolve_col(df: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    """Find the first column whose lowercase name matches one of the candidates."""
    lower_to_real = {c.lower().strip(): c for c in df.columns}
    for cand in candidates:
        if cand in lower_to_real:
            return lower_to_real[cand]
    return None


def load_ktc(
    source: str | Path | IO[str] | IO[bytes],
    sleeper_players: dict[str, dict],
    *,
    skill_positions: tuple[str, ...] = ("QB", "RB", "WR", "TE"),
    fuzzy_score_cutoff: int = 88,
) -> KTCLoadResult:
    """Read a KTC CSV and produce a sleeper_id → value map.

    Args:
        source: file path (str/Path) or open file-like (e.g. from Streamlit's
            st.file_uploader). Both text and binary streams are accepted.
        sleeper_players: the players_nfl map from SleeperClient.get_players_nfl.
        skill_positions: positions we care about. Non-skill rows are ignored.
        fuzzy_score_cutoff: minimum rapidfuzz WRatio (0-100) for a name match.

    Returns:
        KTCLoadResult with by_player_id populated and diagnostics for the UI.
    """
    df = _read_dataframe(source)
    result = KTCLoadResult()
    result.rows_read = len(df)
    if df.empty:
        return result

    name_col = _resolve_col(df, NAME_COLS)
    value_col = _resolve_col(df, VALUE_COLS)
    pos_col = _resolve_col(df, POS_COLS)
    if not name_col or not value_col:
        raise ValueError(
            f"KTC CSV must have a player-name column ({NAME_COLS}) "
            f"and a value column ({VALUE_COLS}). Got columns: {list(df.columns)}"
        )

    # Pre-build a per-position fuzzy pool of {normalized_name: sleeper_player_id} for skill positions only.
    per_pos: dict[str, list[tuple[str, str, dict]]] = {p: [] for p in skill_positions}
    for pid, p in sleeper_players.items():
        if not isinstance(p, dict):
            continue
        position = p.get("position")
        if position not in skill_positions:
            continue
        full = p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip()
        per_pos[position].append((_normalize_name(full), str(pid), p))
    all_pool = [tup for lst in per_pos.values() for tup in lst]

    for _, row in df.iterrows():
        name = str(row[name_col]).strip()
        if not name or name.lower() == "nan":
            continue
        try:
            value = float(row[value_col])
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        position = str(row[pos_col]).strip().upper()[:2] if pos_col and pd.notna(row.get(pos_col)) else None
        # Normalize position string like "WR1" → "WR"
        if position and position[0:2] in skill_positions:
            position = position[0:2]
        else:
            position = None if (position not in skill_positions) else position

        target = _normalize_name(name)
        if not target:
            result.unmatched.append(name)
            continue

        matched_pid: str | None = None
        # 1) Try with position bucket first (more precise)
        if position and position in per_pos and per_pos[position]:
            names_only = [t[0] for t in per_pos[position]]
            best = process.extractOne(target, names_only, scorer=fuzz.WRatio, score_cutoff=fuzzy_score_cutoff)
            if best is not None:
                _, _score, idx = best
                matched_pid = per_pos[position][idx][1]
                result.matched_by_position_name += 1

        # 2) Fall back to all skill positions
        if not matched_pid and all_pool:
            names_only = [t[0] for t in all_pool]
            best = process.extractOne(target, names_only, scorer=fuzz.WRatio, score_cutoff=fuzzy_score_cutoff)
            if best is not None:
                _, _score, idx = best
                matched_pid = all_pool[idx][1]
                result.matched_by_name_only += 1

        if matched_pid:
            # If duplicates (rare), keep the higher value.
            existing = result.by_player_id.get(matched_pid)
            if existing is None or value > existing:
                result.by_player_id[matched_pid] = value
        else:
            result.unmatched.append(name)

    return result


def _read_dataframe(source: str | Path | IO[str] | IO[bytes]) -> pd.DataFrame:
    """Read CSV from path, text stream, or bytes stream."""
    if isinstance(source, (str, Path)):
        path = Path(source)
        if not path.exists():
            return pd.DataFrame()
        return pd.read_csv(path)
    # Streamlit's UploadedFile is a bytes-like; pandas reads both.
    try:
        return pd.read_csv(source)
    except UnicodeDecodeError:
        # Some KTC exports come as latin-1
        if hasattr(source, "seek"):
            source.seek(0)
        data = source.read() if hasattr(source, "read") else source
        return pd.read_csv(io.BytesIO(data if isinstance(data, bytes) else data.encode()), encoding="latin-1")


# ---------------------------------------------------------------------------
# FantasyCalc API (recommended path — direct sleeper_id join, no fuzzy needed)


FC_API_URL = "https://api.fantasycalc.com/values/current"


@dataclass
class FantasyCalcResult:
    """Mapping of sleeper_id → fantasy-calc value, plus diagnostics."""
    by_player_id: dict[str, float] = field(default_factory=dict)
    rows_fetched: int = 0
    rows_with_sleeper_id: int = 0
    rows_without_sleeper_id: int = 0
    api_url: str = ""
    error: str | None = None

    @property
    def total_matched(self) -> int:
        return self.rows_with_sleeper_id

    def __bool__(self) -> bool:
        return bool(self.by_player_id)


def fetch_fantasycalc(
    *,
    superflex: bool = True,
    num_teams: int = 12,
    ppr: float = 1.0,
    timeout: float = 15.0,
) -> FantasyCalcResult:
    """Pull current Superflex dynasty values from FantasyCalc's public API.

    Args:
        superflex: True → 2-QB league (Superflex). False → 1-QB.
        num_teams: 8, 10, 12, or 14. Defaults to 12.
        ppr: 1.0, 0.5, or 0.0. Defaults to 1.0 (full PPR).
        timeout: HTTP timeout in seconds.

    Returns:
        FantasyCalcResult with by_player_id populated (sleeper_id → value).
    """
    import requests

    params = {
        "isDynasty": "true",
        "numQbs": "2" if superflex else "1",
        "numTeams": str(num_teams),
        "ppr": str(ppr),
    }
    # Encode URL for diagnostics / display
    url_with_params = FC_API_URL + "?" + "&".join(f"{k}={v}" for k, v in params.items())

    result = FantasyCalcResult(api_url=url_with_params)
    try:
        r = requests.get(FC_API_URL, params=params, timeout=timeout,
                         headers={"User-Agent": "sleeper-cell/0.1"})
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        result.error = f"Network error: {e}"
        return result
    except ValueError as e:
        result.error = f"Bad JSON: {e}"
        return result

    if not isinstance(data, list):
        result.error = f"Unexpected response type: {type(data).__name__}"
        return result

    result.rows_fetched = len(data)
    for item in data:
        if not isinstance(item, dict):
            continue
        player = item.get("player") or {}
        sleeper_id = player.get("sleeperId")
        value = item.get("value")
        if sleeper_id is None or value is None:
            result.rows_without_sleeper_id += 1
            continue
        try:
            result.by_player_id[str(sleeper_id)] = float(value)
            result.rows_with_sleeper_id += 1
        except (TypeError, ValueError):
            result.rows_without_sleeper_id += 1

    return result
