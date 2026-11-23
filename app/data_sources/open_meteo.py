"""Open-Meteo Historical Weather API (ERA5 reanalysis). Free, no API key.

Docs: https://open-meteo.com/en/docs/historical-weather-api
Values are for a ~25 km grid cell around the hub, not a weather station.
"""

from __future__ import annotations

from datetime import date

from app.data_sources.http import get_json

URL = "https://archive-api.open-meteo.com/v1/archive"

# Open-Meteo variable -> our column name
DAILY_VARS = {
    "snowfall_sum": "snowfall_cm",
    "precipitation_sum": "precip_mm",
    "temperature_2m_max": "tmax_c",
    "temperature_2m_min": "tmin_c",
    "wind_gusts_10m_max": "gust_kmh",
}


def fetch_daily(lat: float, lon: float, start: date, end: date) -> list[dict]:
    payload = get_json(
        URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "daily": ",".join(DAILY_VARS),
            "timezone": "auto",
        },
        timeout=60.0,
    )
    return parse_daily(payload)


def parse_daily(payload: dict) -> list[dict]:
    """Turn Open-Meteo's column-oriented `daily` block into one dict per day."""
    daily = payload["daily"]
    rows = []
    for i, day in enumerate(daily["time"]):
        row = {"date": day}
        for src, col in DAILY_VARS.items():
            values = daily.get(src) or []
            row[col] = values[i] if i < len(values) else None
        rows.append(row)
    return rows
