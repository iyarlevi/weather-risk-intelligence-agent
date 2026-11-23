"""Deterministic risk scoring: config + cached data -> HubRisk for every hub.

The LLM never computes scores. It calls these functions (through agent tools) and explains
what they return.
"""

from __future__ import annotations

import hashlib
import sqlite3
from functools import lru_cache

import yaml

from app.config import CONFIG_DIR, Hub, load_hubs
from app.data_sources.ingest import history_window
from app.scoring.metrics import compute_metric, scale
from app.scoring.models import ComponentScore, HazardScore, HubRisk, RankingEntry
from app.storage import db


@lru_cache
def load_scoring_config() -> dict:
    cfg = yaml.safe_load((CONFIG_DIR / "scoring.yaml").read_text())
    validate_config(cfg)
    return cfg


def config_fingerprint() -> str:
    """Short hash of scoring.yaml. Stored with score snapshots so we know which model made them."""
    return hashlib.sha256((CONFIG_DIR / "scoring.yaml").read_bytes()).hexdigest()[:12]


def validate_config(cfg: dict) -> None:
    """Fail fast on a broken config instead of silently producing odd scores."""
    total = sum(h["weight"] for h in cfg["hazards"].values())
    assert abs(total - 1) < 1e-6, f"hazard weights must sum to 1, got {total}"
    for name, hz in cfg["hazards"].items():
        w = sum(c["weight"] for c in hz["components"])
        assert abs(w - 1) < 1e-6, f"{name}: component weights must sum to 1, got {w}"
        for c in hz["components"]:
            assert c["hi"] > c["lo"], f"{name}.{c['metric']}: hi must be > lo"
            if c["source"] == "weather":
                assert c["metric"] in cfg["weather_metrics"], f"unknown metric {c['metric']}"


def tier_for(score: float, cfg: dict | None = None) -> str:
    cfg = cfg or load_scoring_config()
    for t in cfg["tiers"]:
        if score >= t["min"]:
            return t["label"]
    return cfg["tiers"][-1]["label"]


def _fema_value(nri: dict | None, hazard: str) -> float | None:
    if not nri:
        return None
    entry = nri["hazards"].get(hazard) or {}
    freq = entry.get("annual_freq")
    # FEMA "Not Applicable" (e.g. hurricanes inland) has no frequency: the hazard doesn't occur.
    if freq is None and entry.get("rating") in ("Not Applicable", "No Rating", None):
        return 0.0
    return freq


def score_hub(hub: Hub, weather_rows: list[dict], nri: dict | None, cfg: dict) -> HubRisk:
    """Pure function: all inputs are passed in, so it can be tested without a database."""
    quality: list[str] = []
    if not weather_rows:
        quality.append("No weather history available; weather components scored as 0.")
    if not nri:
        quality.append("No FEMA NRI record for this county; FEMA components scored as 0.")

    # Compute every weather metric once; hazards reuse them.
    weather_values = {
        name: compute_metric(weather_rows, spec) for name, spec in cfg["weather_metrics"].items()
    }

    hazards: dict[str, HazardScore] = {}
    for key, hz in cfg["hazards"].items():
        comps = []
        for c in hz["components"]:
            if c["source"] == "weather":
                spec = cfg["weather_metrics"][c["metric"]]
                raw, unit, label = weather_values[c["metric"]], spec["unit"], spec["label"]
            else:
                raw = _fema_value(nri, c["metric"])
                unit = "events/yr"
                label = f"FEMA {c['metric'].replace('_', ' ')} frequency"
            s = scale(raw, c["lo"], c["hi"])
            comps.append(
                ComponentScore(
                    metric=c["metric"],
                    source=c["source"],
                    label=label,
                    raw_value=None if raw is None else round(raw, 2),
                    unit=unit,
                    scale=(c["lo"], c["hi"]),
                    score=round(s, 1),
                    weight=c["weight"],
                    contribution=round(s * c["weight"], 2),
                )
            )
        h_score = sum(cp.score * cp.weight for cp in comps)
        hazards[key] = HazardScore(
            hazard=key,
            label=hz["label"],
            score=round(h_score, 1),
            tier=tier_for(h_score, cfg),
            weight=hz["weight"],
            contribution=round(h_score * hz["weight"], 2),
            components=comps,
            top_driver=max(comps, key=lambda cp: cp.contribution).label,
        )

    composite = sum(h.score * h.weight for h in hazards.values())
    start, end = (weather_rows[0]["date"], weather_rows[-1]["date"]) if weather_rows else ("", "")
    return HubRisk(
        hub_id=hub.id,
        name=hub.name,
        state=hub.state,
        region=hub.region,
        composite_score=round(composite, 1),
        tier=tier_for(composite, cfg),
        hazards=hazards,
        top_hazards=sorted(hazards, key=lambda k: hazards[k].contribution, reverse=True),
        data_window=f"{start}..{end}",
        data_quality=quality,
    )


def score_all(conn: sqlite3.Connection) -> dict[str, HubRisk]:
    cfg = load_scoring_config()
    start, end = history_window()
    out = {}
    for hub in load_hubs():
        rows = db.get_weather(conn, hub.id, start, end)
        out[hub.id] = score_hub(hub, rows, db.get_nri(conn, hub.county_fips), cfg)
    # Attach each hub's overall rank so a single-hub answer can say "#2 of 21".
    for entry in rank(out):
        out[entry.hub_id].composite_rank = entry.rank
        out[entry.hub_id].hubs_ranked = len(out)
    return out


def rank(
    scores: dict[str, HubRisk],
    hazard: str | None = None,
    region: str | None = None,
    limit: int | None = None,
) -> list[RankingEntry]:
    """Rank hubs by composite score, or by one hazard's score. Optionally filter by region."""
    cfg = load_scoring_config()
    if hazard and hazard not in cfg["hazards"]:
        raise ValueError(f"unknown hazard '{hazard}'. Valid: {', '.join(cfg['hazards'])}")
    pool = [s for s in scores.values() if not region or s.region.lower() == region.lower()]

    def key(s: HubRisk) -> float:
        return s.hazards[hazard].score if hazard else s.composite_score

    # Sort by score, then by hub id so ties always come out in the same order.
    pool.sort(key=lambda s: (-key(s), s.hub_id))
    entries = []
    for i, s in enumerate(pool[:limit] if limit else pool, start=1):
        driver = s.hazards[hazard].top_driver if hazard else s.hazards[s.top_hazards[0]].label
        entries.append(
            RankingEntry(
                rank=i,
                hub_id=s.hub_id,
                name=s.name,
                region=s.region,
                score=key(s),
                tier=s.hazards[hazard].tier if hazard else s.tier,
                top_driver=driver,
            )
        )
    return entries
