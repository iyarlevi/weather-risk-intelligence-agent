"""Offline tests for the data layer: parsers, storage round-trip, config."""

from datetime import date

import pytest

from app.config import get_hub, get_settings, load_hubs, regions
from app.data_sources import fema_nri, nws, open_meteo
from app.data_sources.ingest import history_window
from app.storage import db


def test_hubs_config_is_valid():
    hubs = load_hubs()
    ids = [h.id for h in hubs]
    assert len(ids) == len(set(ids)), "hub ids must be unique"
    assert all(len(h.county_fips) == 5 and h.county_fips.isdigit() for h in hubs)
    assert all(-125 < h.lon < -66 and 24 < h.lat < 50 for h in hubs), "hubs are in CONUS"
    assert {"Midwest", "South", "West", "Northeast"} <= set(regions())


def test_get_hub_accepts_names_and_ids():
    assert get_hub("denver").name == "Denver"
    assert get_hub("Salt Lake City").id == "salt_lake"
    assert get_hub("New Orleans").id == "new_orleans"
    assert get_hub("atlantis") is None


def test_history_window_is_last_full_years():
    assert history_window(date(2026, 11, 23), 5) == (date(2021, 1, 1), date(2025, 12, 31))


def test_open_meteo_parse_daily():
    payload = {
        "daily": {
            "time": ["2025-01-01", "2025-01-02"],
            "snowfall_sum": [0.0, 2.1],
            "precipitation_sum": [0.0, 3.0],
            "temperature_2m_max": [3.5, -1.0],
            "temperature_2m_min": [-7.5, -12.0],
            "wind_gusts_10m_max": [20.0, 55.0],
        }
    }
    rows = open_meteo.parse_daily(payload)
    assert rows[1] == {
        "date": "2025-01-02",
        "snowfall_cm": 2.1,
        "precip_mm": 3.0,
        "tmax_c": -1.0,
        "tmin_c": -12.0,
        "gust_kmh": 55.0,
    }


def test_fema_parse_handles_not_applicable():
    attrs = {
        "STCOFIPS": "08031",
        "COUNTY": "Denver",
        "STATEABBRV": "CO",
        "NRI_VER": "test",
        "RISK_SCORE": 95.2,
        "RISK_RATNG": "Relatively High",
        "HRCN_RISKS": None,
        "HRCN_RISKR": "Not Applicable",
        "HRCN_AFREQ": None,
        "WNTW_RISKS": 91.9,
        "WNTW_RISKR": "Relatively High",
        "WNTW_AFREQ": 13.7,
    }
    rec = fema_nri.parse_county(attrs)
    assert rec["hazards"]["hurricane"] == {
        "risk_score": None,
        "rating": "Not Applicable",
        "annual_freq": None,
    }
    assert rec["hazards"]["winter_weather"]["risk_score"] == 91.9
    assert rec["hazards"]["overall"]["rating"] == "Relatively High"
    assert set(fema_nri.HAZARDS.values()) <= set(rec["hazards"])


def test_nws_parse_alerts():
    payload = {
        "features": [{"properties": {"event": "Winter Storm Warning", "severity": "Severe"}}]
    }
    alerts = nws.parse_alerts(payload)
    assert alerts[0]["event"] == "Winter Storm Warning"
    assert alerts[0]["severity"] == "Severe"


def test_storage_round_trip(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    rows = [
        {
            "date": "2025-01-01",
            "snowfall_cm": 0.0,
            "precip_mm": 1,
            "tmax_c": 1,
            "tmin_c": -1,
            "gust_kmh": 10,
        },
        {
            "date": "2025-01-02",
            "snowfall_cm": 3.0,
            "precip_mm": 4,
            "tmax_c": 0,
            "tmin_c": -9,
            "gust_kmh": 40,
        },
    ]
    assert db.upsert_weather(conn, "denver", rows) == 2
    assert db.upsert_weather(conn, "denver", rows) == 2  # idempotent upsert
    assert db.weather_coverage(conn, "denver") == ("2025-01-01", "2025-01-02", 2)
    got = db.get_weather(conn, "denver", start=date(2025, 1, 2))
    assert len(got) == 1 and got[0]["snowfall_cm"] == 3.0

    db.upsert_nri(
        conn,
        {
            "county_fips": "08031",
            "county": "Denver",
            "state": "CO",
            "nri_version": "v",
            "hazards": {"hail": {"risk_score": 99.0}},
        },
    )
    assert db.get_nri(conn, "08031")["hazards"]["hail"]["risk_score"] == 99.0
    assert db.get_nri(conn, "00000") is None


def test_app_database_starts_from_seed(tmp_path, monkeypatch):
    # No database yet (fresh clone / new container): connect() copies the seed.
    if not db.SEED_DB_PATH.exists():
        pytest.skip("no seed database")

    settings = get_settings().model_copy(update={"database_path": tmp_path / "app.db"})
    monkeypatch.setattr(db, "get_settings", lambda: settings)
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM weather_daily").fetchone()[0] > 30_000
    assert db.latest_snapshot(conn) is not None  # the baseline for alerts ships with it
