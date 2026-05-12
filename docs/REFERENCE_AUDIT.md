# Sleeper Cell — Reference Repo Audit

**Date:** 2026-05-11
**Purpose:** Before writing a line of Sleeper Cell, study three existing projects that solve overlapping problems. Learn what to mimic, what to skip, and where each one is load-bearing for our MVP.

The three repos audited:
1. `references/sleeper-dashboard/` — [rawarne2/sleeper-dashboard](https://github.com/rawarne2/sleeper-dashboard) — React/TS dashboard with KTC valuation
2. `references/SleeperTierSite/` — [brownjf2027/SleeperTierSite](https://github.com/brownjf2027/SleeperTierSite) — Python/Flask live draft tracker with tier logic
3. `references/dynastyprocess-data/` — [dynastyprocess/data](https://github.com/dynastyprocess/data) — Open dynasty data repo (CSVs, weekly refresh)

---

## 1. Tech stack of each repo

| Repo | Stack | Verdict for Sleeper Cell |
|---|---|---|
| `sleeper-dashboard` | React 19, TypeScript, Vite, Tailwind, IndexedDB (`idb`), Recharts, Vitest. Calls its own backend (NOT Sleeper API directly). | **Wrong stack** for us. Concepts only — caching, error envelope, tier-chip UI. |
| `SleeperTierSite` | Python 3.10+, Flask 3.0, Flask-SQLAlchemy, requests, pandas, APScheduler, gunicorn. Talks to Sleeper API directly. | **Closest match.** Lift `data.py` patterns and the `DraftState` model. Skip Flask shell. |
| `dynastyprocess-data` | No runtime code. CSVs + GitHub Actions cron. Updated weekly Fridays. | **Our data layer.** Use directly. |

## 2. Key files worth studying

**From `SleeperTierSite` (primary blueprint):**
- `references/SleeperTierSite/data.py` — single-module Sleeper API client. All endpoints + caching + projection joins in ~600 lines.
- `references/SleeperTierSite/services/draft_service.py` — clean separation of draft data processing. Live-vs-manual mode handling, CSV tier ingestion, **cache-busted live fetch** (lines 172–194).
- `references/SleeperTierSite/services/player_service.py` — tier-aware sorting, position filtering.
- `references/SleeperTierSite/models/draft_state.py` — consolidated `DraftState` class replacing globals; methods `reset()`, `get_my_players_by_position(pos)`, `to_dict()`.
- `references/SleeperTierSite/templates/draft_board.html` (lines 737–978) — `DraftPoller` JS class with adaptive polling (8s drafting / 20s paused / 60s pre-draft) and 30-min idle kill-switch.

**From `sleeper-dashboard` (concepts only):**
- `references/sleeper-dashboard/src/dashboardBundleCache.ts` — cache key construction pattern: `leagueId|season|format|redraft|tep`.
- `references/sleeper-dashboard/src/LeagueContext.tsx` — stale-while-revalidate + cancellation pattern. Translates to Streamlit as `@st.cache_data` + session-state in-flight guard.
- `references/sleeper-dashboard/src/utils/teamStats.ts` — small reusable PF/PA/PPG formulas and ownership-tier color thresholds.

**From `dynastyprocess-data` (data files, not code):**
- `files/db_playerids.csv` — 12,440 rows, 34 ID columns. **`sleeper_id` column = the canonical cross-walk.**
- `files/values.csv` — 775 rows, dynasty value scores (1QB + Superflex variants in same row).
- `files/values-picks.csv` — 85 rows. Rookie pick values for 2026, 2027, 2028.
- `files/db_fpecr_latest.csv` — 4,742 rows. FantasyPros expert consensus rankings with mean, SD, best, worst — material for tier-cliff detection.
- `files/missing_ids.json` — sparse fixup table for players missing from primary cross-walk.

## 3. Data files worth using

**Pin from `dynastyprocess-data`:**

| File | Why | Refresh strategy |
|---|---|---|
| `db_playerids.csv` | Sleeper ID → FantasyPros / KTC / Yahoo / ESPN / NFL bridge. Cleanest is `sleeper_id` (int-as-string in Sleeper API) → cast to int → join on `db_playerids.sleeper_id`. | Mirror locally; refresh weekly on Friday. |
| `values.csv` | Dynasty values, 1QB + 2QB (Superflex) in same row. Top score ~10000 (Chase). Join key to `db_playerids` is `fp_id` ↔ `fantasypros_id`. | Mirror locally; refresh weekly. Optionally re-fetch on app boot if local stale. |
| `values-picks.csv` | Rookie pick values, e.g. `"2026 Pick 1.01"`, `"2028 1st"`. ECR-based, not 0–10000 scale — convert by interpolating the player value curve at the same ECR. | Mirror locally; refresh weekly. |
| `db_fpecr_latest.csv` | ECR mean + standard deviation + best/worst per player. **Best raw input for statistical tier-cliff detection** (SD jumps between consecutive ranks). | Mirror locally; daily refresh in-season (Sep–Dec). |

**Coverage gaps to plug elsewhere:** No projections, no IDP, no ADP, rookies pre-NFL-draft have `sleeper_id=NA`. For projections, plan to either scrape FantasyPros or use the undocumented `api.sleeper.com/projections/nfl/player/{id}` endpoint (which `SleeperTierSite` does in `data.py`).

**From `SleeperTierSite`:**
- `top_players.json` and `trending.json` are useful as one-shot dev fixtures (no live API needed when iterating UI).
- `draft.json` + `draft_picks.json` snapshot a finished real draft — perfect test fixture for our Sleeper Cell unit tests.

## 4. Sleeper API patterns worth copying conceptually

**Endpoints (`SleeperTierSite/data.py:8-12`):**
```
PLAYERS_URL       = https://api.sleeper.app/v1/players/nfl
DRAFT_URL         = https://api.sleeper.app/v1/draft/{draft_id}
USER_URL          = https://api.sleeper.app/v1/user/{user_or_id}
LEAGUES_BY_USER   = https://api.sleeper.app/v1/user/{user_id}/leagues/nfl/{season}
DRAFTS_BY_USER    = https://api.sleeper.app/v1/user/{user_id}/drafts/nfl/{season}
```
Also worth noting: `/v1/state/nfl`, `/v1/players/nfl/trending/{add|drop}?lookback_hours=`, `/v1/league/{id}/rosters`, `/v1/league/{id}/users`, `/v1/league/{id}/traded_picks`, `/v1/draft/{id}/picks`, `/v1/draft/{id}/traded_picks`. Undocumented: `api.sleeper.com/projections/nfl/player/{id}`.

**Cache-busted fresh fetch** (`draft_service.py:172-194`):
```python
timestamp = int(time.time() * 1000)
url = f"https://api.sleeper.app/v1/draft/{draft_id}?t={timestamp}&_cb={timestamp}"
headers = {'Cache-Control': 'no-cache, no-store, must-revalidate, max-age=0',
           'Pragma': 'no-cache', 'Expires': '0', 'If-None-Match': '*',
           'If-Modified-Since': 'Thu, 01 Jan 1970 00:00:00 GMT', 'Connection': 'close'}
```
**Take this idea**, not this exact technique. We should use `requests.Session()` with `Cache-Control: no-cache` and unique timestamps. Aggressive header stuffing is fine but most of it is cargo-cult — the `?t=<ms>` query parameter alone defeats most caches.

**Adaptive polling cadence** (`draft_board.html:919-938`):
- 8s while `status == "drafting"`
- 20s while `paused`
- 60s while `pre_draft`
- Plus 15s minimum-reload throttle, 30-min idle kill-switch

For Streamlit: use `streamlit-autorefresh` with state-driven interval. The kill-switch matters — drafts run hours.

**Lightweight check-for-update endpoint** (`main.py:1891`): only returns `{update_available, total_picks, draft_status}` via a composite state key `f"{len(picks)}_{status}"`. Our Streamlit equivalent: cache `(num_picks, status)` in `st.session_state` and only call full refresh when it changes.

**Typed error funnel** (`sleeper-dashboard/LeagueContext.tsx:178-205`): single try/catch wraps fetch, parses error body defensively (`.catch(() => ({}))`), validates shape (`Array.isArray(data.rosters)`) before trusting. Good template for our `sleeper_client.py`.

## 5. Draft tracking ideas worth adopting

Build a `DraftState` dataclass modeled on `SleeperTierSite/models/draft_state.py`:
- `draft_status: str`, `picks: list[dict]`, `slot_to_roster: dict`, `pick_owner: dict[int, int]` (overall pick → current owner roster_id, after applying traded picks).
- Per-position dicts: `available_by_pos: dict[str, list[Player]]`, `drafted_by_pos: dict[str, list[Player]]`, `my_team_by_pos: dict[str, list[Player]]`.
- Methods: `reset()`, `apply_traded_picks(traded: list[dict])`, `apply_picks_made(picks: list[dict])`, `to_view_dict()` for the Streamlit layer.
- **Live vs manual draft** handled by a single field-mapping step at ingest, not branched code paths. In live mode, player position is at `pick['metadata']['position']`; in manual mode it's `pick['position']`. Normalize once.
- **User's team resolution** by draft slot, not by user_id alone — picks have `roster_id` but slot mapping comes from `draft['slot_to_roster_id']`. See `draft_service.py:142-159`.

**Handle traded picks carefully.** Sleeper's `/picks` endpoint returns picks as-made (with current owner). For *upcoming* picks, you have to compute pick-ownership from `slot_to_roster_id` + `/draft/{id}/traded_picks`. We already implemented this for the war-room artifact — port that logic.

## 6. Dynasty value / player ID mapping ideas worth adopting

**Canonical bridge path:**
```
Sleeper player_id (string of digits)
    → cast to int
    → join on db_playerids.csv [sleeper_id]
    → carry fantasypros_id
    → join on values.csv [fp_id]
    → get value_1qb and value_2qb (Superflex)
    → ALSO join db_fpecr_latest.csv [id] for ECR / SD / best / worst
```

Implementation rules:
- Cast Sleeper's string `player_id` to `int` before join — `db_playerids.sleeper_id` is an integer column.
- Many rows in `db_playerids` have `sleeper_id=NA` (rookies pre-NFL-draft, deep CFB prospects, retired players). For those, fall back to fuzzy `merge_name + position + team` match. `merge_name` is already lowercased and punctuation-stripped — use it.
- Last resort: `missing_ids.json` (~2,694 sparse records with whatever IDs are known). Only consult this when a Sleeper ID has no row in `db_playerids`.
- Cache the resolved cross-walk in `data/players_index.parquet` with TTL of 1 week. Don't recompute every Streamlit run.

**Value scale awareness:**
- `values.csv` values are 0–10000-ish, two variants per row: `value_1qb` (1QB league) and `value_2qb` (Superflex league). For Trade Whores we use `value_2qb`.
- `values-picks.csv` values are **ECR (rank), not 0–10000 score**. To compare a "2027 Pick 1.05" to a player, look up the ECR, then find the player at the same ECR in `values.csv` and use that score.
- Trade Whores–specific adjustment: TE Premium and 6pt pass TDs aren't in DynastyProcess's scoring assumptions. A small post-processing bump for TEs (+5–10%) and elite QBs (+5%) closes the gap.

## 7. Tier logic ideas worth adopting

`SleeperTierSite` takes a **manual** approach: users upload a CSV with tier numbers per player. The tier-cliff visualization is just a Jinja `tier-divider` row inserted whenever `currentTier != previousTier`. Sort key is `(tier, -projection)` (`draft_service.py:237-243`).

**For Sleeper Cell, we should go one level beyond:**
- Default tiers derived algorithmically from `db_fpecr_latest.csv` using ECR + SD:
  - Group players by position.
  - Iterate in rank order. Open a new tier when `ecr_jump > k * mean_within_tier_sd` (k ≈ 1.0–1.5, tunable).
  - Default tier numbers; let user override via CSV upload (mimicking `SleeperTierSite/csv_upload.json` schema).
- **Tier-cliff alert**: when a tier has ≤ N players remaining (default 2), flag it red in the position card. This is the "if you don't take a tier-2 RB *this* pick, you fall to tier 3" signal.
- **ADP color rule** (`draft_board.html:296-304`): red when player's ADP round ≤ current pick round (they will be gone), yellow when within 1 round, default otherwise. Port verbatim.

## 8. What NOT to copy

**From `sleeper-dashboard`:**
- React/Tailwind class machinery, IndexedDB persistence, hash-based tab routing, the two-tier frontend-plus-backend architecture, the LLM trade analyzer. None translate.
- **No LICENSE file** in this repo (verified by ls). Default copyright applies — do not copy snippets verbatim. Concepts only.

**From `SleeperTierSite`:**
- Flask session-cookie state (`save_draft_state` keyed by `draft_state_{draft_id}`) — `st.session_state` makes this unnecessary.
- Disk-writing inside API getters (`data.py:53-54, 69-70`) — `get_draft()` writes `draft.json` on every call. Brittle, no path config.
- `Test.py` and `scratch.json` — dead/scratch code.
- Two parallel modules (`data.py` and `espn_data.py`) doing nearly the same thing.
- `csv_upload.json` is checked in with real user data — don't ship user samples.
- Hard-coded `TOP_X_PLAYERS = 1500` and 3-tier ceiling in `player_service.py:73-86`.
- Flask-Login / WTForms / SQLite feedback DB — all web-framework cruft.
- Full-page reload (`window.location.reload(true)`) on update — use `st.rerun()`.

**From `dynastyprocess-data`:**
- Don't bundle the raw CSVs in distributable artifacts (GPL-3 redistribution concerns — see §9).
- `database.csv` — body says "This file is deprecated".
- `archives/` — historical snapshots, big, irrelevant.

## 9. License considerations

| Repo | License | Implications |
|---|---|---|
| `sleeper-dashboard` | **No LICENSE file** | Default copyright. Reference patterns only; no verbatim copying. |
| `SleeperTierSite` | **BSD-3-Clause** (Copyright 2024 Jasen Brown) | Permissive. Can adapt code freely. If we copy non-trivial chunks (e.g. `DraftPoller` adaptive logic, `DraftState` model), preserve copyright notice + reproduce in NOTICE file. Cannot use author's name to endorse. |
| `dynastyprocess-data` | **GNU GPL v3** | Copyleft. Reading the CSVs at runtime is fine. **Do NOT bundle the raw CSVs in a distributed artifact** — fetch from `raw.githubusercontent.com/dynastyprocess/data/master/files/...` at runtime to avoid copyleft propagation. Courtesy attribution to "DynastyProcess.com (Tan Ho)" in any UI surface that displays values. |

**Action items:**
- Create `THIRD_PARTY_NOTICES.md` at repo root with attributions for `SleeperTierSite` (BSD) and `dynastyprocess/data` (GPL-3 + attribution request).
- `references/` folder is reference-only — add to `.gitignore` so it doesn't ship in commits or in the deployed app.
- Sleeper Cell itself can be MIT or Apache-2 (TBD by Ian).

## 10. Recommended architecture for Sleeper Cell

```
sleepercell/
├── app.py                          # Streamlit entrypoint, thin presentation only
├── src/
│   ├── __init__.py
│   ├── sleeper_client.py           # Sleeper API wrapper. Cache-busted live calls, typed errors.
│   ├── data_layer.py               # DynastyProcess CSV loader, refresh-on-stale, fuzzy fallback
│   ├── player_index.py             # Sleeper ID ↔ FP ID ↔ values cross-walk
│   ├── draft_state.py              # DraftState dataclass; applies traded picks; per-position views
│   ├── valuation.py                # Pure value/score functions (1QB vs SF, TE-premium adj.)
│   ├── tier_engine.py              # Algorithmic tier detection from ECR+SD; cliff alerts
│   ├── team_analysis.py            # Roster construction, positional needs, holes
│   ├── trade_analysis.py           # Phase 2 — 2027-for-current math
│   ├── recommendation_engine.py    # Phase 2 — what/why/risk/alternative formatter
│   ├── message_generator.py        # Phase 2 — ≤280-char Sleeper-chat templates, never auto-sent
│   └── ui_helpers.py               # Position chips, color rules, formatting (no Streamlit imports
│                                   # in pure modules above; helpers may use streamlit)
├── data/                           # Local cache of DynastyProcess CSVs, players_index, snapshots
│   ├── .gitkeep
│   └── (gitignored CSV downloads at runtime)
├── docs/
│   ├── REFERENCE_AUDIT.md          # this file
│   └── (future: ARCHITECTURE.md, DATA_FLOWS.md)
├── scripts/
│   └── refresh_data.py             # Manual / cron refresh of DynastyProcess CSVs
├── tests/
│   ├── fixtures/                   # snapshots from SleeperTierSite/draft.json + our own
│   └── test_*.py                   # pytest unit tests for valuation, tier, player-index
├── references/                     # gitignored; the three audited repos live here
├── requirements.txt
├── .streamlit/
│   └── secrets.toml.example
├── .gitignore
├── README.md
└── THIRD_PARTY_NOTICES.md
```

**Design rules carried from the operating-rules block:**
1. `app.py` is presentation only. Every module in `src/` is pure-Python — **no Streamlit imports** in modules `sleeper_client.py` → `recommendation_engine.py`. This lets us wrap them with FastAPI later without rewriting.
2. `@st.cache_data` with TTLs: players 6h, league/users 1h, draft picks 5s.
3. League ID / Draft ID via Streamlit sidebar input, optionally from `.streamlit/secrets.toml`.
4. Trade Whores–specific rules (3RR, TEP) live in a single `LeagueConfig` dataclass loaded from a `leagues.yaml` file at boot. Hardcoding our league into the app would block reusing it for other leagues later.
5. Recommendation engine **never** auto-submits trades or sends messages. It only generates copy-friendly text.
6. Pytest unit tests for `valuation.py`, `tier_engine.py`, and `player_index.py` are required to pass before Phase 1 is declared done.

---

**Status:** Audit complete. Awaiting MVP-plan approval before writing code.
