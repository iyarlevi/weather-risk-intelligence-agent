"""SQLite storage: cached source data, chat sessions, score snapshots and risk alerts.

SQLite fits here: the data is small (~40k daily rows), there is one writer, and the
whole store is one file that ships with the app. No database server to run.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path

from app.config import SEED_DB_PATH, get_settings

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

-- One row per chat session: the full message list sent to Claude (JSON), so
-- follow-up questions keep their context across API calls and server restarts.
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id    TEXT PRIMARY KEY,
    messages      TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- Stage 8: every alert check stores the full score table, so the next check can diff it.
CREATE TABLE IF NOT EXISTS score_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at            TEXT NOT NULL,
    config_fingerprint  TEXT NOT NULL,    -- which scoring.yaml produced these scores
    data_window         TEXT NOT NULL,    -- which history window they cover
    scores              TEXT NOT NULL     -- JSON: {hub_id: {composite, tier, hazards: {...}}}
);

-- One row per detected change (a hub's composite or one hazard score).
CREATE TABLE IF NOT EXISTS risk_alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    snapshot_id     INTEGER NOT NULL REFERENCES score_snapshots(id),
    hub_id          TEXT NOT NULL,
    metric          TEXT NOT NULL,        -- "composite" or a hazard key
    old_score       REAL NOT NULL,
    new_score       REAL NOT NULL,
    old_tier        TEXT NOT NULL,
    new_tier        TEXT NOT NULL,
    delivered       INTEGER NOT NULL,     -- 1 if the webhook accepted it
    delivery_detail TEXT
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
    is_app_db = path is None  # an explicit path (tests, seed builder) is used as given
    path = Path(path or get_settings().database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_app_db and not path.exists() and SEED_DB_PATH.exists():
        # First start (fresh clone, or a new Vercel instance with an empty /tmp): begin from
        # the committed starting copy instead of an empty database.
        shutil.copyfile(SEED_DB_PATH, path)
    # check_same_thread=False: FastAPI may use the connection from worker threads.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row  # rows behave like dicts: row["date"]
    # WAL mode lets the API read while the ingest/alert job writes.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)  # CREATE IF NOT EXISTS, so safe on every start
    return conn


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------- weather_daily

WEATHER_COLS = ("snowfall_cm", "precip_mm", "tmax_c", "tmin_c", "gust_kmh")


def upsert_weather(conn: sqlite3.Connection, hub_id: str, rows: Iterable[dict]) -> int:
    # INSERT OR REPLACE on the (hub_id, date) key makes re-running the ingest safe:
    # re-fetched days overwrite old values instead of creating duplicates.
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
    """(first date, last date, number of days) stored for a hub. Used to skip re-downloads."""
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
    out["hazards"] = json.loads(out.pop("data"))  # stored as JSON text, returned as a dict
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


# ---------------------------------------------------------------- chat_sessions


def load_session(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    row = conn.execute(
        "SELECT messages FROM chat_sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    return json.loads(row[0]) if row else []


def save_session(conn: sqlite3.Connection, session_id: str, messages: list[dict]) -> None:
    now = _now()
    # Upsert that keeps the original created_at.
    conn.execute(
        "INSERT INTO chat_sessions VALUES (?, ?, ?, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET messages = excluded.messages, "
        "updated_at = excluded.updated_at",
        (session_id, json.dumps(messages), now, now),
    )
    conn.commit()


def delete_session(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("DELETE FROM chat_sessions WHERE session_id = ?", (session_id,))
    conn.commit()


# -------------------------------------------------------------- score_snapshots


def save_snapshot(
    conn: sqlite3.Connection, fingerprint: str, data_window: str, scores: dict
) -> int:
    cur = conn.execute(
        "INSERT INTO score_snapshots (taken_at, config_fingerprint, data_window, scores) "
        "VALUES (?, ?, ?, ?)",
        (_now(), fingerprint, data_window, json.dumps(scores)),
    )
    conn.commit()
    return cur.lastrowid


def latest_snapshot(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM score_snapshots ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    out = dict(row)
    out["scores"] = json.loads(out["scores"])
    return out


# ------------------------------------------------------------------ risk_alerts


def save_alerts(
    conn: sqlite3.Connection,
    snapshot_id: int,
    changes: Iterable[dict],
    delivered: bool,
    detail: str,
) -> None:
    now = _now()
    conn.executemany(
        "INSERT INTO risk_alerts (created_at, snapshot_id, hub_id, metric, old_score, "
        "new_score, old_tier, new_tier, delivered, delivery_detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                now,
                snapshot_id,
                c["hub_id"],
                c["metric"],
                c["old_score"],
                c["new_score"],
                c["old_tier"],
                c["new_tier"],
                int(delivered),
                detail,
            )
            for c in changes
        ],
    )
    conn.commit()


def recent_alerts(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute("SELECT * FROM risk_alerts ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]
