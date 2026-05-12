"""Thin wrapper around the Sleeper API.

Design notes
- One `SleeperClient` instance holds a `requests.Session` and on-disk caches.
- Cache busting via `?t=<ms>` query param for endpoints that change during a draft
  (draft, picks, traded_picks). Static endpoints (players index) are cached on disk.
- Errors funnel through a single `SleeperError` so the caller never sees raw
  `requests` exceptions.
- ZERO Streamlit imports here. All caching/TTL is plain disk + dict, not @st.cache_data.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


API_BASE = "https://api.sleeper.app"
USER_AGENT = "sleeper-cell/0.1 (https://github.com/imackenzieai1/sleepercell)"


class SleeperError(RuntimeError):
    """Anything that goes wrong talking to Sleeper."""

    def __init__(self, message: str, *, url: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status = status


@dataclass
class SleeperClient:
    cache_dir: Path
    """Where to persist big static payloads (players index, season projections)."""

    timeout: float = 15.0
    session: requests.Session | None = None

    def __post_init__(self) -> None:
        self.cache_dir = Path(self.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if self.session is None:
            self.session = requests.Session()
            self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    # ----------------------------------------------------------- internals

    def _get_json(self, url: str, *, cache_bust: bool = False) -> Any:
        if cache_bust:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}t={int(time.time() * 1000)}"
        try:
            r = self.session.get(url, timeout=self.timeout)  # type: ignore[union-attr]
        except requests.RequestException as e:
            raise SleeperError(f"network error: {e}", url=url) from e
        if r.status_code == 404:
            return None
        if not r.ok:
            raise SleeperError(f"HTTP {r.status_code}", url=url, status=r.status_code)
        try:
            return r.json()
        except ValueError as e:
            raise SleeperError(f"invalid JSON: {e}", url=url) from e

    def _cached_get_json(self, url: str, cache_file: str, ttl_seconds: int) -> Any:
        """Disk-cached GET for big static payloads (players index, season projections)."""
        path = self.cache_dir / cache_file
        if path.exists() and (time.time() - path.stat().st_mtime) < ttl_seconds:
            return json.loads(path.read_text())
        data = self._get_json(url)
        path.write_text(json.dumps(data, separators=(",", ":")))
        return data

    # -------------------------------------------------------------- public

    # --- League / users / rosters / drafts list ---

    def get_league(self, league_id: str) -> dict[str, Any] | None:
        return self._get_json(f"{API_BASE}/v1/league/{league_id}")

    def get_users(self, league_id: str) -> list[dict[str, Any]]:
        return self._get_json(f"{API_BASE}/v1/league/{league_id}/users") or []

    def get_rosters(self, league_id: str) -> list[dict[str, Any]]:
        return self._get_json(f"{API_BASE}/v1/league/{league_id}/rosters") or []

    def get_league_traded_picks(self, league_id: str) -> list[dict[str, Any]]:
        return self._get_json(f"{API_BASE}/v1/league/{league_id}/traded_picks") or []

    def get_transactions(self, league_id: str, week: int) -> list[dict[str, Any]]:
        """Trades, waivers, FAs for one week. Pre-season + draft trades land in week 1."""
        return self._get_json(f"{API_BASE}/v1/league/{league_id}/transactions/{week}") or []

    def get_all_transactions(self, league_id: str, *, max_weeks: int = 19) -> list[dict[str, Any]]:
        """Pull transactions across all weeks (0-18). Returns flat list, deduped by transaction_id.

        Per-week errors are swallowed so one flaky week doesn't kill the whole pull.
        """
        seen_ids: set[str] = set()
        out: list[dict[str, Any]] = []
        for week in range(0, max_weeks + 1):
            try:
                week_txns = self.get_transactions(league_id, week)
            except SleeperError:
                continue
            for t in week_txns or []:
                tid = str(t.get("transaction_id") or "")
                if tid and tid in seen_ids:
                    continue
                if tid:
                    seen_ids.add(tid)
                out.append(t)
        return out

    def get_user(self, username_or_id: str) -> dict[str, Any] | None:
        return self._get_json(f"{API_BASE}/v1/user/{username_or_id}")

    def get_user_leagues(self, user_id: str, season: str) -> list[dict[str, Any]]:
        return self._get_json(f"{API_BASE}/v1/user/{user_id}/leagues/nfl/{season}") or []

    # --- Draft ---

    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        return self._get_json(f"{API_BASE}/v1/draft/{draft_id}", cache_bust=True)

    def get_draft_picks(self, draft_id: str) -> list[dict[str, Any]]:
        return self._get_json(f"{API_BASE}/v1/draft/{draft_id}/picks", cache_bust=True) or []

    def get_draft_traded_picks(self, draft_id: str) -> list[dict[str, Any]]:
        return self._get_json(f"{API_BASE}/v1/draft/{draft_id}/traded_picks", cache_bust=True) or []

    # --- Static catalogs ---

    def get_state(self) -> dict[str, Any]:
        return self._get_json(f"{API_BASE}/v1/state/nfl") or {}

    def get_players_nfl(self, ttl_seconds: int = 6 * 3600) -> dict[str, Any]:
        """~5–14MB. 6h TTL on disk by default."""
        return self._cached_get_json(
            f"{API_BASE}/v1/players/nfl",
            cache_file="players_nfl.json",
            ttl_seconds=ttl_seconds,
        )

    # --- Projections (undocumented but stable) ---

    def get_season_projections(
        self,
        season: str,
        *,
        season_type: str = "regular",
        ttl_seconds: int = 6 * 3600,
    ) -> list[dict[str, Any]]:
        """Bulk per-stat projections for an entire season — ~8MB, 9000+ rows.

        Returns a list of {player_id, player, stats, ...} objects. The grouping=season
        param flattens to a single record per player (no per-week breakdown).
        """
        url = f"{API_BASE}/projections/nfl/{season}?season_type={season_type}&grouping=season"
        cache_file = f"projections_{season}_{season_type}.json"
        data = self._cached_get_json(url, cache_file=cache_file, ttl_seconds=ttl_seconds)
        return data if isinstance(data, list) else []

    def get_player_projection(
        self,
        player_id: str,
        season: str,
        *,
        season_type: str = "regular",
    ) -> dict[str, Any] | None:
        """Single-player season projection. Used as a fallback / sanity check —
        normally we use get_season_projections() and index by player_id."""
        url = f"{API_BASE}/projections/nfl/player/{player_id}?season_type={season_type}&season={season}"
        return self._get_json(url)
