"""Tools the LLM can call. Each one is a thin wrapper around deterministic code.

Every tool:
  1. validates Claude's input with its Pydantic model (schemas.py),
  2. calls the scoring engine / stats / NWS client,
  3. returns compact JSON. Errors go back as `is_error` results so Claude can recover
     (e.g. ask the user which hub they meant) instead of the request crashing.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, ValidationError

from app.agent import schemas as s
from app.config import get_hub, load_hubs
from app.data_sources import nws
from app.data_sources.http import SourceError
from app.data_sources.ingest import history_window
from app.scoring.engine import config_fingerprint, load_scoring_config, rank, score_all
from app.scoring.models import HubRisk
from app.scoring.stats import weather_stat
from app.storage import db


class ToolError(Exception):
    """A problem Claude should see and react to (unknown hub, bad dates, ...)."""


# ------------------------------------------------------------------------ score cache


class ScoreCache:
    """Scores only change when data or config change, so compute once per process."""

    def __init__(self) -> None:
        self._scores: dict[str, HubRisk] | None = None
        self._lock = threading.Lock()

    def get(self, conn: sqlite3.Connection) -> dict[str, HubRisk]:
        with self._lock:
            if self._scores is None:
                self._scores = score_all(conn)
            return self._scores

    def invalidate(self) -> None:
        with self._lock:
            self._scores = None


SCORES = ScoreCache()


def _resolve_hub(name: str):
    hub = get_hub(name)
    if hub is None:
        known = ", ".join(h.name for h in load_hubs())
        raise ToolError(f"'{name}' is not one of our hubs. Known hubs: {known}")
    return hub


# ------------------------------------------------------------------------ tool bodies


def list_hubs(conn, inp: s.ListHubsInput) -> dict:
    hubs = [h for h in load_hubs() if not inp.region or h.region == inp.region]
    return {"hubs": [h.model_dump(exclude={"county_fips"}) for h in hubs]}


def rank_hubs(conn, inp: s.RankHubsInput) -> dict:
    scores = SCORES.get(conn)
    hazard = None if inp.rank_by == "composite" else inp.rank_by
    entries = rank(scores, hazard=hazard, region=inp.region, limit=inp.limit)
    cfg = load_scoring_config()
    return {
        "ranked_by": "composite disruption risk"
        if hazard is None
        else cfg["hazards"][hazard]["label"],
        "region": inp.region or "all",
        "score_scale": "0-100, higher = more exposed",
        "ranking": [e.model_dump() for e in entries],
    }


def get_hub_risk(conn, inp: s.HubInput) -> dict:
    hub = _resolve_hub(inp.hub)
    return SCORES.get(conn)[hub.id].model_dump()


def compare_hubs(conn, inp: s.CompareHubsInput) -> dict:
    if not 2 <= len(inp.hubs) <= 6:
        raise ToolError("compare_hubs needs between 2 and 6 hubs")
    scores = SCORES.get(conn)
    hazards = inp.hazards or list(load_scoring_config()["hazards"])
    out = []
    for name in inp.hubs:
        r = scores[_resolve_hub(name).id]
        out.append(
            {
                "hub_id": r.hub_id,
                "name": r.name,
                "composite_score": r.composite_score,
                "composite_rank": f"{r.composite_rank} of {r.hubs_ranked}",
                "tier": r.tier,
                # Full component breakdown only for the hazards asked about, to keep it short.
                "hazards": {h: r.hazards[h].model_dump() for h in hazards},
            }
        )
    return {"hubs": out}


def get_weather_stat(conn, inp: s.WeatherStatInput) -> dict:
    hub = _resolve_hub(inp.hub)
    try:
        start = date.fromisoformat(inp.start_date) if inp.start_date else None
        end = date.fromisoformat(inp.end_date) if inp.end_date else None
    except ValueError as exc:
        raise ToolError(f"bad date: {exc}") from exc
    try:
        st = weather_stat(conn, hub.id, inp.stat, inp.year, start, end, inp.threshold)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return st.model_dump()


def get_active_alerts(conn, inp: s.HubInput) -> dict:
    hub = _resolve_hub(inp.hub)
    try:
        alerts = nws.active_alerts(hub.lat, hub.lon)
    except SourceError as exc:
        raise ToolError(f"NWS alerts are unavailable right now: {exc}") from exc
    return {
        "hub": hub.name,
        "source": "NOAA NWS active alerts (live)",
        # The count comes from code, so the model doesn't have to count (rule: no LLM math).
        "alert_count": len(alerts),
        "alerts": alerts,
    }


def get_methodology(conn, inp: s.NoInput) -> dict:
    cfg = load_scoring_config()
    start, end = history_window()
    return {
        "history_window": f"{start}..{end}",
        "scoring_config_version": f"{cfg['version']} ({config_fingerprint()})",
        "composite": "weighted average of hazard scores",
        "hazard_score": "weighted average of components; each component = "
        "clamp((value - lo) / (hi - lo)) * 100 on a FIXED scale",
        "hazards": {
            k: {
                "label": h["label"],
                "weight": h["weight"],
                "components": [
                    {k2: c[k2] for k2 in ("source", "metric", "lo", "hi", "weight")}
                    for c in h["components"]
                ],
            }
            for k, h in cfg["hazards"].items()
        },
        "tiers": cfg["tiers"],
        "known_limitations": [
            "Weights are expert judgment, not fitted to the company's own disruption history.",
            "ERA5 reanalysis (~25 km grid) smooths local extremes like peak gusts.",
            (
                "FEMA NRI is county level; large counties with mountains (King, Salt Lake) "
                "inflate winter frequency."
            ),
            (
                "5 years of weather is short for rare events like hurricanes; FEMA's longer "
                "record leads that hazard."
            ),
            "Scores measure exposure, not financial loss: no hub throughput or cost data.",
        ],
    }


# ---------------------------------------------------------------------- the registry


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    fn: Callable[[sqlite3.Connection, BaseModel], dict]

    def definition(self) -> dict:
        """The tool definition sent to Claude. The input schema comes from the Pydantic model."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
        }


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool(
            "list_hubs",
            "List the company's distribution hubs (id, city, state, region, coordinates). "
            "Use it to resolve which hubs exist or which are in a region.",
            s.ListHubsInput,
            list_hubs,
        ),
        Tool(
            "rank_hubs",
            "Rank hubs by composite weather-disruption risk or by one hazard "
            "(winter, hurricane_wind, flood, severe_storm, heat), optionally within a region. "
            "Scores are deterministic, 0-100, higher = more exposed.",
            s.RankHubsInput,
            rank_hubs,
        ),
        Tool(
            "get_hub_risk",
            "Full risk breakdown for one hub: composite score, rank, every hazard score, and "
            "every component (raw value, scale, weight, contribution). Use it for 'why' questions.",
            s.HubInput,
            get_hub_risk,
        ),
        Tool(
            "compare_hubs",
            "Side-by-side comparison of 2-6 hubs with component detail for the chosen hazards.",
            s.CompareHubsInput,
            compare_hubs,
        ),
        Tool(
            "weather_stat",
            "Count days meeting a weather condition at a hub over a year or date range, and "
            "return the percentage. Example: share of days with snowfall in Denver in 2025.",
            s.WeatherStatInput,
            get_weather_stat,
        ),
        Tool(
            "active_alerts",
            "Live NOAA weather alerts at a hub right now. Current conditions only, not part of "
            "the long-term risk score.",
            s.HubInput,
            get_active_alerts,
        ),
        Tool(
            "get_methodology",
            "How the scores are calculated: hazards, weights, scales, tiers, data window and "
            "known limitations.",
            s.NoInput,
            get_methodology,
        ),
    ]
}


def tool_definitions() -> list[dict]:
    return [t.definition() for t in TOOLS.values()]


def execute(conn: sqlite3.Connection, name: str, raw_input: dict) -> tuple[str, bool]:
    """Run a tool. Returns (JSON text for the tool_result, is_error)."""
    tool = TOOLS.get(name)
    if tool is None:
        return json.dumps({"error": f"unknown tool '{name}'"}), True
    try:
        inp = tool.input_model.model_validate(raw_input)
        result = tool.fn(conn, inp)
    except ValidationError as exc:
        return json.dumps({"error": f"invalid input: {exc.errors(include_url=False)}"}), True
    except ToolError as exc:
        return json.dumps({"error": str(exc)}), True
    # Compact separators: tool results are re-sent every turn, so smaller = cheaper.
    return json.dumps(result, separators=(",", ":"), default=str), False


# Re-exported so callers can open a connection without importing storage directly.
connect = db.connect
