"""Tests for the deterministic scoring engine."""

from datetime import date, timedelta

import pytest

from app.config import get_settings, load_hubs
from app.scoring.engine import load_scoring_config, rank, score_all, score_hub, tier_for
from app.scoring.metrics import compute_metric, count_days, scale
from app.scoring.stats import weather_stat
from app.storage import db

HUB = load_hubs()[0]


def make_rows(n: int, **cols) -> list[dict]:
    """n daily rows from 2025-01-01, using constant column values."""
    base = {"snowfall_cm": 0.0, "precip_mm": 0.0, "tmax_c": 20.0, "tmin_c": 10.0, "gust_kmh": 20.0}
    base.update(cols)
    return [{"date": (date(2025, 1, 1) + timedelta(i)).isoformat(), **base} for i in range(n)]


# ------------------------------------------------------------------ pure maths


def test_scale_is_clamped_and_linear():
    assert scale(None, 0, 10) == 0
    assert scale(-5, 0, 10) == 0
    assert scale(5, 0, 10) == 50
    assert scale(50, 0, 10) == 100
    assert scale(105, 60, 150) == 50


def test_count_days_ignores_missing_values():
    rows = [{"x": 1.0}, {"x": None}, {"x": 3.0}]
    assert count_days(rows, "x", ">=", 2) == (1, 2)


def test_days_per_year_normalises_partial_years():
    # Half a year with snow every other day -> about 182 days/yr either way.
    rows = make_rows(182)
    for r in rows[::2]:
        r["snowfall_cm"] = 2.0
    spec = {"column": "snowfall_cm", "op": ">=", "threshold": 1.0, "agg": "days_per_year"}
    assert compute_metric(rows, spec) == pytest.approx(182.6, abs=0.1)


# ---------------------------------------------------------------------- config


def test_scoring_config_is_valid():
    cfg = load_scoring_config()  # validate_config runs inside and asserts weights sum to 1
    assert set(cfg["hazards"]) == {"winter", "hurricane_wind", "flood", "severe_storm", "heat"}


def test_tiers():
    assert tier_for(75) == "High"
    assert tier_for(45) == "Elevated"
    assert tier_for(20) == "Moderate"
    assert tier_for(3) == "Low"


# ---------------------------------------------------------------- score_hub


def test_score_hub_arithmetic_is_traceable():
    cfg = load_scoring_config()
    rows = make_rows(365, snowfall_cm=6.0, tmin_c=-20.0)  # >=5 cm: heavy snow daily
    nri = {
        "hazards": {
            "winter_weather": {"annual_freq": 40, "rating": "Very High"},
            "hurricane": {"annual_freq": None, "rating": "Not Applicable"},
        }
    }
    risk = score_hub(HUB, rows, nri, cfg)

    winter = risk.hazards["winter"]
    # snow, heavy snow, freeze and FEMA winter are saturated; ice storm missing -> 0.
    assert winter.score == pytest.approx(85.0)
    # Each hazard score equals the sum of its component contributions.
    for hz in risk.hazards.values():
        assert hz.score == pytest.approx(sum(c.contribution for c in hz.components), abs=0.1)
    # Composite equals the sum of hazard contributions.
    assert risk.composite_score == pytest.approx(
        sum(h.contribution for h in risk.hazards.values()), abs=0.1
    )
    # FEMA "Not Applicable" is treated as zero, not as missing.
    hurricane = next(
        c for c in risk.hazards["hurricane_wind"].components if c.metric == "hurricane"
    )
    assert hurricane.raw_value == 0.0
    assert risk.top_hazards[0] == "winter"


def test_score_hub_flags_missing_data():
    risk = score_hub(HUB, [], None, load_scoring_config())
    assert risk.composite_score == 0
    assert len(risk.data_quality) == 2


# ------------------------------------------------------------------- ranking


def test_rank_is_deterministic_on_ties():
    cfg = load_scoring_config()
    hubs = load_hubs()[:3]
    scores = {h.id: score_hub(h, [], None, cfg) for h in hubs}  # all zero -> full tie
    order = [e.hub_id for e in rank(scores)]
    assert order == sorted(order)  # ties broken by hub id
    assert rank(scores) == rank(scores)


def test_rank_rejects_unknown_hazard():
    with pytest.raises(ValueError, match="unknown hazard"):
        rank({}, hazard="volcano")


# ------------------------------------------ integration (uses the ingested DB)

needs_db = pytest.mark.skipif(
    not get_settings().database_path.exists(),
    reason="run `python -m app.data_sources.ingest` first",
)


@needs_db
def test_real_data_rankings_are_plausible():
    scores = score_all(db.connect())
    assert len(scores) == len(load_hubs())
    midwest_winter = [e.hub_id for e in rank(scores, "winter", "Midwest", limit=3)]
    assert "minneapolis" in midwest_winter
    assert rank(scores, "heat", limit=1)[0].hub_id == "phoenix"
    assert scores["miami"].hazards["winter"].score < 5
    assert {s.composite_rank for s in scores.values()} == set(range(1, len(scores) + 1))


@needs_db
def test_weather_stat_denver_snow():
    st = weather_stat(db.connect(), "Denver", "snowfall", year=2025)
    assert st.total_days == 365
    assert 0 < st.percent < st.any_amount_percent < 30
    assert st.condition == "snowfall_cm >= 1.0 cm"


@needs_db
def test_weather_stat_errors():
    conn = db.connect()
    with pytest.raises(ValueError, match="unknown hub"):
        weather_stat(conn, "gotham", "snowfall")
    with pytest.raises(ValueError, match="no data"):
        weather_stat(conn, "denver", "snowfall", year=1990)
