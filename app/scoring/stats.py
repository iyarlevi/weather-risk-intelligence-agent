"""Ad-hoc weather statistics for factual questions.

Example: "What percentage of days in Denver last year had snowfall?"
    -> weather_stat(conn, "denver", "snowfall", year=2025)

Each statistic has a documented default threshold. The result also reports the
"any amount" figure for snowfall and rain, because the answer depends strongly on the
definition (Denver 2025: 11.8% of days with any snow, 4.7% with >=1 cm).
"""

from __future__ import annotations

import sqlite3
from datetime import date

from pydantic import BaseModel, Field

from app.config import get_hub
from app.data_sources.ingest import history_window
from app.scoring.metrics import count_days
from app.storage import db

# name -> (column, operator, default threshold, unit, description)
STATS: dict[str, tuple[str, str, float, str, str]] = {
    "snowfall": ("snowfall_cm", ">=", 1.0, "cm", "days with snowfall"),
    "heavy_snow": ("snowfall_cm", ">=", 5.0, "cm", "days with heavy snowfall"),
    "rain": ("precip_mm", ">=", 1.0, "mm", "days with precipitation"),
    "heavy_rain": ("precip_mm", ">=", 25.0, "mm", "days with heavy precipitation"),
    "freezing": ("tmin_c", "<=", 0.0, "C", "days with min temperature at or below freezing"),
    "hard_freeze": ("tmin_c", "<=", -15.0, "C", "days with severe cold"),
    "hot": ("tmax_c", ">=", 35.0, "C", "days with high heat"),
    "high_wind": ("gust_kmh", ">=", 75.0, "km/h", "days with damaging wind gusts"),
}

# For these stats we also report the share of days with any non-zero amount.
_ANY_AMOUNT = {"snowfall": 0.0, "rain": 0.0}


class WeatherStat(BaseModel):
    hub_id: str
    hub_name: str
    stat: str
    description: str
    condition: str = Field(description='e.g. "snowfall_cm >= 1.0"')
    period: str
    matching_days: int
    total_days: int
    percent: float
    any_amount_percent: float | None = Field(
        None, description="Share of days with any non-zero amount (snowfall/rain only)"
    )
    source: str = "Open-Meteo Historical API (ERA5 reanalysis, ~25 km grid)"


def weather_stat(
    conn: sqlite3.Connection,
    hub_id: str,
    stat: str,
    year: int | None = None,
    start: date | None = None,
    end: date | None = None,
    threshold: float | None = None,
) -> WeatherStat:
    hub = get_hub(hub_id)
    if hub is None:
        raise ValueError(f"unknown hub '{hub_id}'")
    if stat not in STATS:
        raise ValueError(f"unknown stat '{stat}'. Valid: {', '.join(STATS)}")

    # Period: an explicit year, an explicit range, or the full history window.
    if year is not None:
        start, end = date(year, 1, 1), date(year, 12, 31)
    elif start is None or end is None:
        start, end = history_window()

    column, op, default_thr, unit, desc = STATS[stat]
    thr = default_thr if threshold is None else threshold
    rows = db.get_weather(conn, hub.id, start, end)
    if not rows:
        lo, hi, _ = db.weather_coverage(conn, hub.id)
        raise ValueError(f"no data for {hub.name} in {start}..{end}. Available: {lo}..{hi}")

    hits, n = count_days(rows, column, op, thr)
    any_pct = None
    if stat in _ANY_AMOUNT:
        any_hits, _ = count_days(rows, column, ">", _ANY_AMOUNT[stat])
        any_pct = round(100 * any_hits / n, 1) if n else None

    return WeatherStat(
        hub_id=hub.id,
        hub_name=hub.name,
        stat=stat,
        description=desc,
        condition=f"{column} {op} {thr} {unit}",
        period=f"{rows[0]['date']}..{rows[-1]['date']}",
        matching_days=hits,
        total_days=n,
        percent=round(100 * hits / n, 1) if n else 0.0,
        any_amount_percent=any_pct,
    )
