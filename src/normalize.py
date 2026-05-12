"""Common-size player metrics onto a single 0.000–1.000 scale.

The Sleeper Cell engine produces values on at least five different scales:
  - Dynasty value           ~ 1000–1500 (TW points × age curve × strategy)
  - Community value         ~ 0–10000   (KTC / FantasyCalc market scale)
  - Adjusted community      ~ 0–10000+  (community × league-fit)
  - VBD (over replacement)  ~ -300 to +500
  - Dyn VBD                 ~ -200 to +400

Comparing across scales requires mental gymnastics. This module normalizes each
metric to a 0.000–1.000 percentile-rank score, displayed with 3 decimals:

  Allen on Dynasty value:       1317 → 1.000 (top of pool)
  Allen on Community value:    9806 → .999  (essentially top)
  Dart on Dynasty value:        1157 → .980

1.000 = top of pool · .500 = median · .000 = bottom. Same number means the same
thing across every metric and every tab.

Tie handling
- Ties get the AVERAGE rank. Three players tied at value=100 in a 10-player pool
  would each get score 0.5000 (the average of the three positions they share).
- Players with None or value≤0 are excluded from the rank entirely (they're
  not "ranked at zero"; they're not in the data).
"""
from __future__ import annotations

from typing import Mapping


def percentile_rank(values: Mapping[str, float | None]) -> dict[str, float]:
    """Convert raw values to batting-average-style 0.000–1.000 score within the valid population.

    Args:
        values: dict mapping any-id → numeric value (None / ≤0 excluded).

    Returns:
        dict mapping id → score (0.000–1.000, 3 decimal places).

    Example:
        >>> percentile_rank({"a": 100, "b": 50, "c": 75})
        {"a": 1.0, "b": 0.0, "c": 0.5}
    """
    valid = [(k, v) for k, v in values.items() if v is not None and float(v) > 0]
    if not valid:
        return {}
    if len(valid) == 1:
        return {valid[0][0]: 1.0}

    valid.sort(key=lambda kv: kv[1])

    # Group ties together: each group gets the AVERAGE rank position.
    n = len(valid)
    out: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and valid[j + 1][1] == valid[i][1]:
            j += 1
        # Ranks i..j inclusive share the same value
        avg_rank = (i + j) / 2.0
        score = round(avg_rank / (n - 1), 3)
        for k in range(i, j + 1):
            out[valid[k][0]] = score
        i = j + 1

    return out


def fmt_ba(score: float | None) -> str:
    """Format a 0-1 score with 3 decimals: .350, 1.000, .000.

    The leading zero is dropped (.985 not 0.985) — read like a batting average.
    None or invalid → em-dash.
    """
    if score is None:
        return "—"
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "—"
    if s >= 1.0:
        return "1.000"
    if s <= 0.0:
        return ".000"
    return f"{s:.3f}".lstrip("0")  # 0.985 → .985


def attach_percentiles(records: list, *, attr_pairs: list[tuple[str, str]]) -> None:
    """Mutate `records` (e.g. list of ValuedPlayer) — for each (source, target) pair,
    read `getattr(r, source)`, compute percentile rank across the list, and write
    the result to `setattr(r, target, ...)`.

    Example:
        attach_percentiles(
            valued_all,
            attr_pairs=[
                ("dynasty_value", "pct_dynasty"),
                ("ktc_value", "pct_community"),
                ("replacement_delta", "pct_vbd"),
            ],
        )
    """
    for source_attr, target_attr in attr_pairs:
        values = {
            getattr(r, "player_id", id(r)): getattr(r, source_attr, None)
            for r in records
        }
        pcts = percentile_rank(values)
        for r in records:
            pid = getattr(r, "player_id", id(r))
            setattr(r, target_attr, pcts.get(pid))
