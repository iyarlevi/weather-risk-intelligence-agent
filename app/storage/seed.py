"""Build the starting copy of the database that is committed to git (data/seed/wria-seed.db).

It holds the downloaded source data (weather + FEMA) and one baseline score snapshot, but
no chat sessions or alerts. Rebuild it after re-running the ingest or changing the weights:

    uv run python -m app.storage.seed
"""

from __future__ import annotations

import sqlite3

from app.alerts.monitor import run_check
from app.config import SEED_DB_PATH
from app.storage import db


def build_seed() -> None:
    SEED_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEED_DB_PATH.unlink(missing_ok=True)
    # VACUUM INTO writes a compact, consistent copy of the live database (WAL included).
    src = db.connect()
    src.execute("VACUUM INTO ?", (str(SEED_DB_PATH),))
    src.close()

    conn = sqlite3.connect(SEED_DB_PATH)
    conn.executescript(
        "DELETE FROM chat_sessions; DELETE FROM risk_alerts; DELETE FROM score_snapshots;"
    )
    conn.commit()
    conn.close()
    # Rollback-journal mode for the committed file: no -wal/-shm side files, and readers copy
    # one self-contained file. db.connect() switches the working copy back to WAL.
    conn = db.connect(SEED_DB_PATH)
    run_check(conn, notify=False)  # stores the baseline snapshot alerts will diff against
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("VACUUM")
    conn.close()

    check = sqlite3.connect(SEED_DB_PATH)
    counts = {
        t: check.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("weather_daily", "nri_county", "score_snapshots", "chat_sessions")
    }
    check.close()
    size_mb = SEED_DB_PATH.stat().st_size / 1e6
    print(f"Wrote {SEED_DB_PATH} ({size_mb:.1f} MB): {counts}")


if __name__ == "__main__":
    build_seed()
