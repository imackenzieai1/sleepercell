"""DynastyProcess data loader — read-on-demand from raw.githubusercontent.com.

The DynastyProcess repo is GPL-3, refreshes every Friday via GitHub Actions, and
ships a dozen CSVs we care about. We don't redistribute these files (GPL hygiene)
— we fetch them at runtime and cache locally with a TTL.

Files we depend on:
  • db_playerids.csv      — the cross-walk (sleeper_id, fantasypros_id, ktc_id, ...)
  • values.csv            — current dynasty values, 1QB + Superflex variants
  • values-picks.csv      — rookie pick values
  • db_fpecr_latest.csv   — FantasyPros ECR with SD/best/worst per player

The other CSVs in the repo are deprecated or low-value for our use case.

Cache strategy:
  • TTL = 1 week (matches refresh cadence). Override per call for testing.
  • Files cached in data/dp/<filename>. The data/ folder is gitignored.
  • If the cache file exists and is fresh, return it without a network call.
  • If the network call fails, fall back to whatever cache exists (any age) so the
    app still runs offline. Log a warning.
"""
from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests


DP_RAW_BASE = "https://raw.githubusercontent.com/dynastyprocess/data/master/files"
DEFAULT_TTL = 7 * 24 * 3600  # 1 week

LOGGER = logging.getLogger("sleeper_cell.data_layer")

DP_FILES = {
    "player_ids": "db_playerids.csv",
    "values": "values.csv",
    "values_picks": "values-picks.csv",
    "fpecr_latest": "db_fpecr_latest.csv",
}


@dataclass
class DataLayer:
    cache_dir: Path
    """Where to mirror DP files. Typically <repo>/data/dp."""
    ttl_seconds: int = DEFAULT_TTL
    session: requests.Session | None = None
    local_files_dir: Path | None = None
    """Optional: a local directory containing the same CSV filenames (e.g. references/dynastyprocess-data/files/).
    If provided, we try local first before going to the network. Useful for offline use or when the user
    has already cloned the DP repo as a reference."""

    def __post_init__(self) -> None:
        self.cache_dir = Path(self.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if self.local_files_dir is not None:
            self.local_files_dir = Path(self.local_files_dir)
        if self.session is None:
            self.session = requests.Session()
            self.session.headers.update({"User-Agent": "sleeper-cell/0.1"})

    # ------------------------------------------------------------ internals

    def _fetch_csv(self, name: str) -> pd.DataFrame:
        filename = DP_FILES[name]
        path = self.cache_dir / filename

        # 1) Fresh disk cache wins
        fresh = path.exists() and (time.time() - path.stat().st_mtime) < self.ttl_seconds
        if fresh:
            return pd.read_csv(path)

        # 2) Try the network
        url = f"{DP_RAW_BASE}/{filename}"
        try:
            r = self.session.get(url, timeout=20)  # type: ignore[union-attr]
            r.raise_for_status()
            path.write_bytes(r.content)
            return pd.read_csv(io.BytesIO(r.content))
        except requests.RequestException as e:
            LOGGER.warning("DP fetch %s failed (%s); falling back to local/cache.", url, e)

        # 3) Local cached file (any age) — at least we have something
        if path.exists():
            return pd.read_csv(path)

        # 4) User-provided local mirror (e.g. references/dynastyprocess-data/files/)
        if self.local_files_dir is not None:
            local_path = self.local_files_dir / filename
            if local_path.exists():
                LOGGER.info("Using local DP mirror at %s.", local_path)
                return pd.read_csv(local_path)

        raise FileNotFoundError(
            f"Could not load DynastyProcess file {filename!r}: network failed, no cache, "
            f"no local fallback at {self.local_files_dir}."
        )

    # ---------------------------------------------------------------- API

    def player_ids(self) -> pd.DataFrame:
        """34 columns of cross-platform IDs. The sleeper_id column is our bridge."""
        df = self._fetch_csv("player_ids")
        # Normalize sleeper_id to a string for joining. DP stores it as integer-or-NA.
        df["sleeper_id"] = df["sleeper_id"].astype("Int64").astype(str).replace("<NA>", "")
        return df

    def values(self) -> pd.DataFrame:
        """Player + pick dynasty values, both 1QB (value_1qb) and Superflex (value_2qb)."""
        return self._fetch_csv("values")

    def values_picks(self) -> pd.DataFrame:
        """Rookie pick values only — keyed by `pick` ordinal and a string 'player' like
        '2027 1st' or '2026 Pick 1.01'."""
        return self._fetch_csv("values_picks")

    def fpecr(self) -> pd.DataFrame:
        """FantasyPros expert consensus rankings with mean ECR, sd, best, worst.

        Columns of interest:
          • ecr_type — 'dp' (dynasty positional), 'dsf' (dynasty superflex), 'drk' (rookie),
            'do' (overall), 'rp' (redraft positional), etc.
          • id — FantasyPros id (join via player_ids.fantasypros_id)
          • ecr, sd, best, worst — used by tier_engine.
        """
        return self._fetch_csv("fpecr_latest")
