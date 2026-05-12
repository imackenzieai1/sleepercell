"""Tests for src/dynasty_curve.py."""
from __future__ import annotations

from src.dynasty_curve import AgeCurve, DEFAULT_CURVES, discounted_dynasty_value


def test_rb_age_curve_peak_and_cliff() -> None:
    rb = DEFAULT_CURVES["RB"]
    assert rb.multiplier(23) == 1.0  # peak start
    assert rb.multiplier(26) == 1.0  # peak end
    # Past 28 should drop fast (RB cliff)
    cliff_year = rb.multiplier(30)
    assert cliff_year < 0.6


def test_compete_vs_rebuild_weight_distribution() -> None:
    """A 30yo RB (declining) should be more valuable to a compete team than a rebuild team."""
    compete = discounted_dynasty_value(season_points=200, position="RB", age=30, strategy="compete", horizon_years=4)
    rebuild = discounted_dynasty_value(season_points=200, position="RB", age=30, strategy="rebuild", horizon_years=4)
    assert compete > rebuild


def test_young_qb_rebuild_premium() -> None:
    """A young QB (24, on the rise) should be MORE valuable to a rebuild team than a compete team."""
    compete = discounted_dynasty_value(season_points=300, position="QB", age=24, strategy="compete", horizon_years=6)
    rebuild = discounted_dynasty_value(season_points=300, position="QB", age=24, strategy="rebuild", horizon_years=6)
    assert rebuild > compete


def test_unknown_age_treated_as_peak() -> None:
    """If age is unknown, the curve returns 1.0 — we don't penalize a player for missing data."""
    v = discounted_dynasty_value(season_points=100, position="WR", age=None, strategy="balanced", horizon_years=4)
    assert v > 200  # at least 100 * weights[0..3] summed
