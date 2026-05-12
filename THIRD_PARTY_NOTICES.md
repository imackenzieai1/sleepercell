# Third-Party Notices

Sleeper Cell is released under the MIT License (see `LICENSE`).

This file acknowledges the open-source projects whose ideas, patterns, or data we draw on. Each is listed with its license and the nature of our use.

---

## SleeperTierSite

- **Source:** https://github.com/brownjf2027/SleeperTierSite
- **License:** BSD-3-Clause, Copyright (c) 2024 Jasen Brown
- **Our use:** Patterns adopted (not verbatim copy): adaptive draft polling cadence, cache-busted Sleeper API fetches, the `DraftState` consolidation model, tier-divider sorting/display.
- **Attribution requirement:** If we redistribute source containing copied code, we must (a) preserve the BSD-3-Clause copyright notice, (b) reproduce the notice in our documentation if shipping binaries, and (c) not use the author's name to endorse Sleeper Cell.

## DynastyProcess Data

- **Source:** https://github.com/dynastyprocess/data
- **License:** GNU GPL v3.0
- **Our use:** Runtime consumption of CSV data files (`db_playerids.csv`, `values.csv`, `values-picks.csv`, `db_fpecr_latest.csv`). Files are fetched on demand from `raw.githubusercontent.com/dynastyprocess/data/master/files/` and cached locally; they are **not bundled** in Sleeper Cell's distributable artifact.
- **Attribution:** Sleeper Cell credits "Data from DynastyProcess.com (Tan Ho)" in any UI surface that displays dynasty values or pick values, per the project's request.

## sleeper-dashboard

- **Source:** https://github.com/rawarne2/sleeper-dashboard
- **License:** No LICENSE file present (default copyright)
- **Our use:** Conceptual reference only. No code copied. Architectural patterns reviewed (stale-while-revalidate cache, error envelope, tier-chip UI). See `docs/REFERENCE_AUDIT.md` §1, §3, §5.
