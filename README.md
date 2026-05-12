# Sleeper Cell

Live dynasty draft intelligence for Sleeper fantasy football leagues.

Most ranking and trade tools assume generic Superflex PPR scoring. Sleeper Cell starts from your league's exact `scoring_settings` and rebuilds every value from raw per-stat projections. In a TE-premium, 6-point-pass-TD, completion-bonus league, that delta can be **+90% on a top QB's season projection** — value generic sources cannot see.

**Status:** Phase 1 complete. 24 passing tests. Used live during a 28-round Superflex dynasty startup.

Read-only and human-in-the-loop. Surfaces information and writes copy-paste trade messages. Never sends a trade.

## What it does

**League-specific player values.** Pulls Sleeper's per-stat projections and applies your `scoring_settings` to compute league-adjusted season points, then runs them through a multi-year dynasty curve with position-specific age decay (the RB cliff at 28, the QB plateau through 33, etc.). Every ranking is fully auditable on the **Why?** tab.

**Live draft board with autorefresh** — recent picks, on-the-clock, and a 20-pick forward simulator that predicts what the league *will* do (using Sleeper SF dynasty ADP) versus what they *should* do (using your value system). Gaps between the two are mispricings to exploit.

**Trade recommender.** Scans every partner and every plausible asset combination. Surfaces only offers where **both sides win by their own model** — your TW gain × their consensus gain. Six patterns: startup trade-up, startup trade-down, picks-for-player, future-for-now, and two more. Weighted by each partner's actual trade activity in this league's transaction history.

**Trade history.** Pulls Sleeper's transaction log and shows every completed trade — who got what, when. The recommender uses these counts as evidence of "willingness to deal."

**Buy-low / sell-high watchlist.** Players where your value disagrees most with consensus ADP — the alpha picks for the rest of the draft.

**Tier engine.** Algorithmic per-position tier detection from value gaps, with cliff alerts when a tier has only a few players remaining.

## Why it exists

I built this because every off-the-shelf dynasty tool assumes a standard scoring config. My league runs Superflex + TE Premium + 6pt pass TDs + completion bonus + first-down bonuses on pass/rush/rec + workhorse rush bonus. That cocktail makes elite volume QBs worth roughly +90% more than generic rankings show, and the rest of the league wasn't going to figure it out mid-draft.

The exploit isn't a secret formula — it's recomputing the rankings from the actual rule book. Anyone with a few hours and the same data can do it. The tool just makes it routine.

## Setup

Sleeper Cell is `uv`-native. One install, two commands, and you're up.

```bash
brew install uv                       # one-time, bundles its own Python

git clone https://github.com/imackenzieai1/sleepercell.git
cd sleepercell
bash scripts/setup.sh                 # syncs deps from pyproject.toml, copies secrets template
uv run streamlit run app.py
```

`uv run` auto-syncs dependencies, picks the right Python (3.12 or 3.13 from `pyproject.toml`'s `requires-python`), and runs inside the project venv without activating it.

### Optional: pre-warm caches

The first run pulls ~13 MB across Sleeper's player catalog and DynastyProcess CSVs. To fetch them upfront:

```bash
uv run python scripts/refresh_data.py
```

### Pre-fill league/draft IDs

`bash scripts/setup.sh` copies `.streamlit/secrets.toml.example` → `.streamlit/secrets.toml`. The file is gitignored — edit it for your league.

### Without uv (fallback)

The same setup script detects a missing `uv` and falls back to system Python + venv + `get-pip.py`. On recent macOS releases the Homebrew Python ↔ system `libexpat` clash can break the fallback path; `uv` sidesteps that by bundling its own Python.

### What you'll need from Sleeper

- League ID — in the URL when you're inside the league: `sleeper.com/leagues/{LEAGUE_ID}/...`
- Draft ID — in the URL when you're in the draft room: `sleeper.com/draft/nfl/{DRAFT_ID}`

Both Sleeper API endpoints are public and read-only. No authentication required.

## Architecture

```
app.py                          Streamlit, presentation only
src/
  ├── league_config.py          LeagueConfig: Sleeper scoring + bylaws overlay
  ├── sleeper_client.py         API wrapper, cache-busted live fetches
  ├── data_layer.py             DynastyProcess CSV loader
  ├── player_index.py           Sleeper ID ↔ FP ID cross-walk (fuzzy fallback)
  ├── projections.py            Per-stat × scoring → league-adjusted season points
  ├── dynasty_curve.py          Per-position age curves; strategy-weighted horizon
  ├── valuation.py              Compose: projections + curve + replacement-level VBD
  ├── tier_engine.py            Algorithmic tier detection + cliff alerts
  ├── draft_state.py            3RR-aware schedule; pick ownership after trades
  ├── team_analysis.py          Roster construction, positional needs
  ├── vbd.py                    Dynamic VBD: value above likely-next-available
  ├── simulator.py              Forward simulator (consensus vs optimal)
  ├── future_pick_value.py      2027/2028 pick valuation
  ├── trade_analysis.py         Dual-valuation trade calculator
  ├── trade_recommender.py      Auto-generated offers
  └── transaction_history.py    Parse Sleeper's trade log
```

Every module under `src/` is pure-Python with no Streamlit imports. The Streamlit layer (`app.py`) is the only place the framework is touched. This is load-bearing: the same modules can be wrapped by FastAPI later without rewriting.

## What it doesn't do (yet)

Phase 1 ships everything above. Open work that would meaningfully improve the tool:

- Multi-leg trade chains (A→B, then B→C)
- KTC values as a fallback / override for pick valuation
- Player photos in tables (Sleeper hosts them — small visual polish)
- CSV export buttons on every table
- In-app banner notifications when a tier is about to cliff
- Streamlit Cloud deploy (so it runs from your phone)
- FastAPI backend wrapping the `src/` modules
- Multi-league switcher

## Reference repos

This repo carries three reference projects under `references/` for inspection, all gitignored. The audit doc at `docs/REFERENCE_AUDIT.md` covers what each one is, what we learned, and how we chose what to mimic vs. skip.

- [rawarne2/sleeper-dashboard](https://github.com/rawarne2/sleeper-dashboard) — React/TS dashboard with KTC values (no LICENSE — reference only)
- [brownjf2027/SleeperTierSite](https://github.com/brownjf2027/SleeperTierSite) — Flask draft tracker with tier logic (BSD-3-Clause)
- [dynastyprocess/data](https://github.com/dynastyprocess/data) — Open dynasty CSVs, our primary data layer (GPL-3, fetched at runtime — never bundled)

See `THIRD_PARTY_NOTICES.md` for license terms and attribution.

## License

MIT. See `LICENSE`.
