"""FEMA National Risk Index (NRI), county level. Free, no API key.

Docs: https://hazards.fema.gov/nri/
Per hazard we keep:
  risk_score  - national percentile 0-100 of the county's risk for that hazard
                (None when FEMA marks the hazard "Not Applicable", e.g. hurricanes in Denver)
  rating      - FEMA's label ("Very Low" ... "Very High", "Not Applicable", ...)
  annual_freq - annualized frequency of events (events per year)
"""

from __future__ import annotations

from app.data_sources.http import get_json

URL = (
    "https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/"
    "National_Risk_Index_Counties/FeatureServer/0/query"
)

# FEMA hazard prefix -> our hazard key
HAZARDS = {
    "HRCN": "hurricane",
    "CFLD": "coastal_flood",
    "IFLD": "inland_flood",  # riverine/inland flooding (was RFLD in older NRI releases)
    "WNTW": "winter_weather",
    "ISTM": "ice_storm",
    "CWAV": "cold_wave",
    "HWAV": "heat_wave",
    "TRND": "tornado",
    "HAIL": "hail",
    "SWND": "strong_wind",
}


def _out_fields() -> str:
    fields = ["STCOFIPS", "COUNTY", "STATEABBRV", "NRI_VER", "RISK_SCORE", "RISK_RATNG"]
    for p in HAZARDS:
        fields += [f"{p}_RISKS", f"{p}_RISKR", f"{p}_AFREQ"]
    return ",".join(fields)


def fetch_counties(county_fips: list[str]) -> list[dict]:
    in_list = ",".join(f"'{f}'" for f in county_fips if f.isdigit())
    payload = get_json(
        URL,
        params={
            "where": f"STCOFIPS IN ({in_list})",
            "outFields": _out_fields(),
            "returnGeometry": "false",
            "f": "json",
        },
    )
    if "error" in payload:
        raise RuntimeError(f"FEMA NRI error: {payload['error']}")
    return [parse_county(f["attributes"]) for f in payload.get("features", [])]


def parse_county(attrs: dict) -> dict:
    hazards = {
        key: {
            "risk_score": attrs.get(f"{p}_RISKS"),
            "rating": attrs.get(f"{p}_RISKR"),
            "annual_freq": attrs.get(f"{p}_AFREQ"),
        }
        for p, key in HAZARDS.items()
    }
    hazards["overall"] = {
        "risk_score": attrs.get("RISK_SCORE"),
        "rating": attrs.get("RISK_RATNG"),
        "annual_freq": None,
    }
    return {
        "county_fips": attrs["STCOFIPS"],
        "county": attrs.get("COUNTY"),
        "state": attrs.get("STATEABBRV"),
        "nri_version": attrs.get("NRI_VER"),
        "hazards": hazards,
    }
