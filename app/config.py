"""Application settings and the hub registry."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
# Starting copy of the database, committed to git: source data + a baseline score snapshot.
# A fresh checkout or a new serverless instance copies it instead of re-downloading (stage 9).
SEED_DB_PATH = ROOT / "data" / "seed" / "wria-seed.db"
# Vercel sets VERCEL=1. Its filesystem is read-only except /tmp, and there are no
# long-lived background threads, so a few defaults differ there.
ON_VERCEL = bool(os.environ.get("VERCEL"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    anthropic_api_key: str = ""
    database_path: Path = Path("/tmp/wria.db") if ON_VERCEL else ROOT / "data" / "wria.db"
    # Risk-change alerts (stage 8). Empty webhook URL = alerts are only logged and stored.
    alert_webhook_url: str = ""
    # A score must move by at least this many points (0-100 scale) to raise an alert.
    # A tier change (e.g. Elevated -> High) always alerts, whatever the size of the move.
    alert_threshold_points: float = 2.0
    # When the scheduled check runs (cron syntax, server local time). Empty = no scheduler.
    # On Vercel the in-process scheduler is off: Vercel Cron calls GET /alerts/cron instead.
    alert_schedule_cron: str = "" if ON_VERCEL else "0 6 * * *"
    # Shared secret Vercel Cron sends as "Authorization: Bearer <secret>" to /alerts/cron.
    cron_secret: str = ""

    # Historical window: the last N full calendar years before the current year.
    history_years: int = 5
    # Identifies us to api.weather.gov, which requires a User-Agent.
    http_user_agent: str = "weather-risk-intelligence-agent/0.1"


@lru_cache  # read .env once per process
def get_settings() -> Settings:
    return Settings()


class Hub(BaseModel):
    id: str
    name: str
    state: str
    region: str
    lat: float
    lon: float
    county_fips: str


@lru_cache
def load_hubs() -> tuple[Hub, ...]:
    raw = yaml.safe_load((CONFIG_DIR / "hubs.yaml").read_text())
    return tuple(Hub(**h) for h in raw["hubs"])


def get_hub(hub_id: str) -> Hub | None:
    # Accept either the id ("salt_lake") or the display name ("Salt Lake City"),
    # because the LLM may pass whichever the user typed.
    key = hub_id.strip().lower().replace(" ", "_")
    for hub in load_hubs():
        if key in (hub.id, hub.name.lower().replace(" ", "_")):
            return hub
    return None


def regions() -> list[str]:
    return sorted({h.region for h in load_hubs()})
