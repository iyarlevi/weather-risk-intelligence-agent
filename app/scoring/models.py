"""Typed results of the scoring engine.

The API returns these models, and the agent's tools pass them to the LLM, so every number
the LLM explains has a traceable origin (raw value -> 0-100 scale -> weight -> contribution).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ComponentScore(BaseModel):
    metric: str
    source: Literal["weather", "fema"]
    label: str
    raw_value: float | None = Field(description="The measured value, in `unit`")
    unit: str
    scale: tuple[float, float] = Field(description="(lo, hi) mapped onto 0..100")
    score: float = Field(description="0-100 after scaling")
    weight: float = Field(description="Weight inside its hazard (hazard weights sum to 1)")
    contribution: float = Field(description="score * weight: points added to the hazard score")


class HazardScore(BaseModel):
    hazard: str
    label: str
    score: float
    tier: str
    weight: float = Field(description="Weight of this hazard in the composite")
    contribution: float = Field(description="score * weight: points added to the composite")
    components: list[ComponentScore]
    top_driver: str = Field(description="Label of the component adding the most points")


class HubRisk(BaseModel):
    hub_id: str
    name: str
    state: str
    region: str
    composite_score: float = Field(description="0-100 weather disruption risk")
    tier: str
    composite_rank: int = Field(0, description="1 = highest composite risk among all hubs")
    hubs_ranked: int = Field(0, description="Number of hubs in the ranking")
    hazards: dict[str, HazardScore]
    top_hazards: list[str] = Field(description="Hazard keys sorted by contribution, highest first")
    data_window: str
    data_quality: list[str] = Field(default_factory=list, description="Warnings, if any")


class RankingEntry(BaseModel):
    rank: int
    hub_id: str
    name: str
    region: str
    score: float
    tier: str
    top_driver: str
