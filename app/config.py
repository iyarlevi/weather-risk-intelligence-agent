"""Application settings and the hub registry."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    anthropic_api_key: str = ""
    database_path: Path = ROOT / "data" / "wria.db"
    alert_webhook_url: str = ""

    # Historical window: the last N full calendar years before the current year.
    history_years: int = 5
    # Identifies us to api.weather.gov, which requires a User-Agent.
    http_user_agent: str = "weather-risk-intelligence-agent/0.1"


@lru_cache
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
    key = hub_id.strip().lower().replace(" ", "_")
    for hub in load_hubs():
        if key in (hub.id, hub.name.lower().replace(" ", "_")):
            return hub
    return None


def regions() -> list[str]:
    return sorted({h.region for h in load_hubs()})
