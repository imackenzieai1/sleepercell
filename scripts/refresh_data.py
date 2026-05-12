"""Pre-warm Sleeper Cell's data caches.

Run this once after `pip install -r requirements.txt` to pull:
  • Sleeper players catalog (~5MB)
  • Sleeper season projections (~8MB)
  • DynastyProcess CSVs (db_playerids, values, values-picks, fpecr_latest)

After that, app.py boots instantly because everything is cached on disk.

Usage:
    python scripts/refresh_data.py
    python scripts/refresh_data.py --season 2026
    python scripts/refresh_data.py --dp-local references/dynastyprocess-data/files
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Make this script runnable without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data_layer import DataLayer
from src.sleeper_client import SleeperClient


def main() -> int:
    ap = argparse.ArgumentParser(description="Pre-warm Sleeper Cell data caches.")
    ap.add_argument("--season", default="2026", help="NFL season for projections.")
    ap.add_argument("--cache-dir", default="data", help="Repo's data/ directory.")
    ap.add_argument("--dp-local", default=None, help="Optional local DP mirror (references/dynastyprocess-data/files).")
    args = ap.parse_args()

    cache = Path(args.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)

    t = time.time()
    print("Sleeper Cell — data refresh")
    print(f"Cache directory: {cache.resolve()}\n")

    client = SleeperClient(cache_dir=cache / "sleeper")

    print("→ Sleeper players_nfl (~5MB) ...", end=" ", flush=True)
    players = client.get_players_nfl(ttl_seconds=0)  # force refresh
    print(f"{len(players)} players")

    print(f"→ Sleeper season projections {args.season} (~8MB) ...", end=" ", flush=True)
    projs = client.get_season_projections(args.season, ttl_seconds=0)
    print(f"{len(projs)} entries")

    print("→ DynastyProcess CSVs ...")
    dl = DataLayer(cache_dir=cache / "dp", ttl_seconds=0, local_files_dir=Path(args.dp_local) if args.dp_local else None)
    for name in ("player_ids", "values", "values_picks", "fpecr"):
        try:
            df = getattr(dl, name)()
            print(f"   ✓ {name}: {len(df):,} rows")
        except Exception as e:
            print(f"   ✗ {name}: {e}")

    print(f"\nDone in {time.time()-t:.1f}s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
