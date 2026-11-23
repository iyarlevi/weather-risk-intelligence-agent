"""SQLite storage: cached source data (and, in later stages, sessions and score history).

SQLite fits here: the data is small (~40k daily rows), there is one writer, and the
whole store is one file that ships with the app. No database server to run.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path

from app.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS weather_daily (
    hub_id        TEXT NOT NULL,
    date          TEXT NOT NULL,          -- ISO date, local to the hub
    snowfall_cm   REAL,
    precip_mm     REAL,
    tmax_c        REAL,
    tmin_c        REAL,
    gust_kmh      REAL,
    PRIMARY KEY (hub_id, date)
);

CREATE TABLE IF NOT EXISTS nri_county (
    county_fips   TEXT PRIMARY KEY,
    county        TEXT,
    state         TEXT,
    nri_version   TEXT,
    data          TEXT NOT NULL,          -- JSON: {hazard: {risk_score, rating, annual_freq}}
    fetched_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fetch_log (
    source        TEXT NOT NULL,
    key           TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    detail        TEXT,
    PRIMARY KEY (source, key)
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or get_settings().database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------- weather_daily

WEATHER_COLS = ("snowfall_cm", "precip_mm", "tmax_c", "tmin_c", "gust_kmh")


def upsert_weather(conn: sqlite3.Connection, hub_id: str, rows: Iterable[dict]) -> int:
    data = [(hub_id, r["date"], *(r.get(c) for c in WEATHER_COLS)) for r in rows]
    conn.executemany(
        f"INSERT OR REPLACE INTO weather_daily (hub_id, date, {', '.join(WEATHER_COLS)}) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        data,
    )
    conn.commit()
    return len(data)


def get_weather(
    conn: sqlite3.Connection, hub_id: str, start: date | None = None, end: date | None = None
) -> list[dict]:
    sql = "SELECT * FROM weather_daily WHERE hub_id = ?"
    params: list = [hub_id]
    if start:
        sql += " AND date >= ?"
        params.append(start.isoformat())
    if end:
        sql += " AND date <= ?"
        params.append(end.isoformat())
    return [dict(r) for r in conn.execute(sql + " ORDER BY date", params)]


def weather_coverage(conn: sqlite3.Connection, hub_id: str) -> tuple[str | None, str | None, int]:
    row = conn.execute(
        "SELECT MIN(date), MAX(date), COUNT(*) FROM weather_daily WHERE hub_id = ?", (hub_id,)
    ).fetchone()
    return row[0], row[1], row[2]


# ------------------------------------------------------------------- nri_county


def upsert_nri(conn: sqlite3.Connection, record: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO nri_county VALUES (?, ?, ?, ?, ?, ?)",
        (
            record["county_fips"],
            record["county"],
            record["state"],
            record["nri_version"],
            json.dumps(record["hazards"]),
            _now(),
        ),
    )
    conn.commit()


def get_nri(conn: sqlite3.Connection, county_fips: str) -> dict | None:
    row = conn.execute("SELECT * FROM nri_county WHERE county_fips = ?", (county_fips,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["hazards"] = json.loads(out.pop("data"))
    return out


# -------------------------------------------------------------------- fetch_log


def log_fetch(conn: sqlite3.Connection, source: str, key: str, detail: str = "") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO fetch_log VALUES (?, ?, ?, ?)", (source, key, _now(), detail)
    )
    conn.commit()


def last_fetch(conn: sqlite3.Connection, source: str, key: str) -> str | None:
    row = conn.execute(
        "SELECT fetched_at FROM fetch_log WHERE source = ? AND key = ?", (source, key)
    ).fetchone()
    return row[0] if row else None
