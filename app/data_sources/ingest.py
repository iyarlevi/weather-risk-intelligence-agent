"""Download historical weather and FEMA hazard data for every hub into SQLite.

Usage:
    uv run python -m app.data_sources.ingest            # fetch what's missing
    uv run python -m app.data_sources.ingest --refresh  # re-download everything
"""

from __future__ import annotations

import argparse
import time
from datetime import date

from app.config import get_settings, load_hubs
from app.data_sources import fema_nri, open_meteo
from app.storage import db


def history_window(today: date | None = None, years: int | None = None) -> tuple[date, date]:
    """Last N *full* calendar years, e.g. 2021-01-01..2025-12-31 when run in 2026."""
    today = today or date.today()
    years = years or get_settings().history_years
    return date(today.year - years, 1, 1), date(today.year - 1, 12, 31)


def ingest_weather(conn, refresh: bool = False) -> None:
    start, end = history_window()
    expected_days = (end - start).days + 1
    for hub in load_hubs():
        lo, hi, n = db.weather_coverage(conn, hub.id)
        if not refresh and lo == start.isoformat() and hi == end.isoformat() and n == expected_days:
            print(f"  weather  {hub.id:<14} cached ({n} days)")
            continue
        rows = open_meteo.fetch_daily(hub.lat, hub.lon, start, end)
        db.upsert_weather(conn, hub.id, rows)
        db.log_fetch(conn, "open_meteo", hub.id, f"{start}..{end}")
        print(f"  weather  {hub.id:<14} fetched {len(rows)} days")
        time.sleep(0.5)  # be polite to the free API


def ingest_nri(conn, refresh: bool = False) -> None:
    hubs = load_hubs()
    missing = [h.county_fips for h in hubs if refresh or db.get_nri(conn, h.county_fips) is None]
    if not missing:
        print(f"  nri      all {len(hubs)} counties cached")
        return
    records = fema_nri.fetch_counties(sorted(set(missing)))
    for rec in records:
        db.upsert_nri(conn, rec)
        db.log_fetch(conn, "fema_nri", rec["county_fips"], rec["nri_version"] or "")
    found = {r["county_fips"] for r in records}
    print(f"  nri      fetched {len(records)} counties")
    for fips in set(missing) - found:
        print(f"  nri      WARNING: no NRI record for county {fips}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="re-download everything")
    args = parser.parse_args()

    conn = db.connect()
    start, end = history_window()
    print(f"Database: {get_settings().database_path}")
    print(f"History window: {start} .. {end}")
    ingest_nri(conn, args.refresh)
    ingest_weather(conn, args.refresh)
    print("Done.")


if __name__ == "__main__":
    main()
