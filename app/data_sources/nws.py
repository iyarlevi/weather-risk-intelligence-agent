"""NOAA National Weather Service active alerts. Free, no API key (needs a User-Agent).

Docs: https://www.weather.gov/documentation/services-web-api
This is live context ("is anything happening right now?"). It is not part of the
long-term exposure score. Results are cached in memory for a few minutes.
"""

from __future__ import annotations

import time

from app.data_sources.http import get_json

URL = "https://api.weather.gov/alerts/active"
_CACHE_TTL_S = 300
_cache: dict[tuple[float, float], tuple[float, list[dict]]] = {}


def active_alerts(lat: float, lon: float) -> list[dict]:
    # NWS rejects coordinates with more than 4 decimals; 2 decimals (~1 km) is plenty.
    key = (round(lat, 2), round(lon, 2))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL_S:
        return hit[1]
    payload = get_json(URL, params={"point": f"{key[0]},{key[1]}"}, timeout=15.0)
    alerts = parse_alerts(payload)
    _cache[key] = (time.time(), alerts)
    return alerts


def parse_alerts(payload: dict) -> list[dict]:
    out = []
    for feat in payload.get("features", []):
        p = feat.get("properties", {})
        out.append(
            {
                "event": p.get("event"),
                "severity": p.get("severity"),
                "urgency": p.get("urgency"),
                "headline": p.get("headline"),
                "onset": p.get("onset"),
                "ends": p.get("ends") or p.get("expires"),
                "area": p.get("areaDesc"),
            }
        )
    return out
