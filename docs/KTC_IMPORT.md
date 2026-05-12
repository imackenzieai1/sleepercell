# Importing KeepTradeCut (KTC) values

Sleeper Cell can overlay community KTC Superflex dynasty values on top of its own scoring-adjusted rankings. The math:

```
Adjusted KTC = KTC value  ×  league-fit multiplier
                                 │
                                 └── per-position-normalized:
                                     (your TW points / Sleeper's default PPR) ÷ position median
```

If a player scores 30% above the position median in YOUR scoring system, their KTC value gets a +30% boost. The KTC value stays the trusted base; we just tilt it for the league's specific rule set.

## Step 1 — Get the data

KTC's free site doesn't have an API or CSV export, so this is manual. The shortest path:

1. Go to [keeptradecut.com/dynasty-rankings](https://keeptradecut.com/dynasty-rankings)
2. **Toggle to Superflex** (top right) — values are dramatically different from 1QB
3. Toggle on **TE Premium** if your league has it
4. Select the rankings table (Cmd-A inside the table works well)
5. Paste into a Google Sheet or Excel
6. Trim to just the **player name** and **value** columns (delete trade-up/trade-down, tier, etc. if present)
7. Save / download as CSV

Alternatively, several community projects scrape KTC daily and publish CSVs on GitHub — search `keeptradecut csv site:github.com`. Use at your own risk; values can be stale.

## Step 2 — File format

Accepted column headers (case-insensitive):

| Column type | Accepted names |
|---|---|
| Player name (required) | `Player`, `Name`, `Player Name`, `player_name`, `full_name` |
| Value (required) | `Value`, `KTC`, `KTC Value`, `Trade Value`, `Value SF`, `Superflex`, `KTC SF` |
| Position (optional, improves matching) | `Pos`, `Position`, `Fantasy Position` |

### Minimal example

```csv
Player,Value
Ja'Marr Chase,9985
Bijan Robinson,9874
Josh Allen,9806
Justin Jefferson,9871
Patrick Mahomes,8902
```

### Recommended (with position for safer matching)

```csv
Player,Pos,Value
Ja'Marr Chase,WR,9985
Bijan Robinson,RB,9874
Josh Allen,QB,9806
Justin Jefferson,WR,9871
Patrick Mahomes,QB,8902
```

Position is optional but helps disambiguate common names. `WR1`, `RB12`, etc. are also accepted — only the first two letters matter.

## Step 3 — Load it into Sleeper Cell

Two ways:

1. **Upload via the sidebar** (any environment, including the deployed cloud app):
   - In the running app, look for **KTC overlay** in the sidebar
   - Click "Upload KTC CSV" and pick your file
   - The app fuzzy-matches names to Sleeper player IDs and reports the result

2. **Drop a file at `data/ktc.csv`** (local-only — the `data/` folder is gitignored):
   - Save your CSV as `data/ktc.csv` in the repo
   - The app auto-loads it on startup
   - Convenient when you don't want to re-upload every session

## Step 4 — Verify the match

After loading, the sidebar shows:

- Green success: `KTC overlay: 187 matched (172 via pos+name, 15 via name-only), 13 unmatched of 200 rows`
- Yellow warning if no rows matched (usually means wrong column headers)

Expand "Unmatched names" to see what couldn't be resolved — typically rookies whose IDs haven't synced yet, or typo'd names. A 90%+ match rate is normal for a top-200 export.

## What changes once KTC is active

The **Best Available** tab gets three new columns next to Dynasty value:

| Column | Meaning |
|---|---|
| `KTC` | Raw KTC value (community consensus) |
| `Fit` | Per-position league-fit multiplier (1.00 = position-median; 1.30 = +30% TW advantage) |
| `Adj KTC` | `KTC × Fit` — KTC value re-tilted for your scoring system |

The Why? tab shows the league-fit calculation per player. Recommended trade offers and trade calculator still use your own valuation as the primary scale; KTC is shown as a sanity check.

## When to refresh

KTC values move slowly during the offseason (a few percent per month) but jump around the NFL draft and during the season. Re-export and re-upload monthly is plenty for dynasty. For a startup draft you're running now, one fresh export at the start is fine.
