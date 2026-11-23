"""The contract between the LLM and our code.

- Tool inputs: one Pydantic model per tool. Its JSON schema is sent to Claude as the tool's
  `input_schema`, and the same model validates whatever Claude sends back.
- AgentAnswer: the JSON schema Claude's final reply MUST follow (enforced by the API through
  `output_config.format`, then validated again here with Pydantic).

Note: structured outputs don't support numeric/string constraints (min/max/length),
so these models use only types, enums and descriptions.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

HazardKey = Literal["winter", "hurricane_wind", "flood", "severe_storm", "heat"]
RankBy = Literal["composite", "winter", "hurricane_wind", "flood", "severe_storm", "heat"]
Region = Literal["Midwest", "South", "West", "Northeast"]
StatName = Literal[
    "snowfall", "heavy_snow", "rain", "heavy_rain", "freezing", "hard_freeze", "hot", "high_wind"
]


class _Strict(BaseModel):
    # extra="forbid" emits `additionalProperties: false`, which structured outputs require.
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- tool inputs


class ListHubsInput(_Strict):
    region: Region | None = Field(None, description="Only hubs in this region")


class RankHubsInput(_Strict):
    rank_by: RankBy = Field(
        "composite", description="'composite' for overall risk, or a single hazard"
    )
    region: Region | None = Field(None, description="Only rank hubs in this region")
    limit: int | None = Field(None, description="Return only the top N hubs")


class HubInput(_Strict):
    hub: str = Field(description="Hub id or city name, e.g. 'dallas' or 'Salt Lake City'")


class CompareHubsInput(_Strict):
    hubs: list[str] = Field(description="2 to 6 hub ids or city names")
    hazards: list[HazardKey] | None = Field(
        None, description="Hazards to show in detail. Omit for all five."
    )


class WeatherStatInput(_Strict):
    hub: str = Field(description="Hub id or city name")
    stat: StatName = Field(
        description=(
            "snowfall (>=1 cm), heavy_snow (>=5 cm), rain (>=1 mm), heavy_rain (>=25 mm), "
            "freezing (min <=0 C), hard_freeze (min <=-15 C), hot (max >=35 C), "
            "high_wind (gusts >=75 km/h)"
        )
    )
    year: int | None = Field(None, description="A single calendar year, e.g. 2025")
    start_date: str | None = Field(None, description="ISO date. Use with end_date instead of year")
    end_date: str | None = Field(None, description="ISO date. Use with start_date")
    threshold: float | None = Field(
        None, description="Override the default threshold (same unit as the stat)"
    )


class NoInput(_Strict):
    pass


# ------------------------------------------------------------------------ final answer


class RankedHub(_Strict):
    rank: int
    hub_id: str
    name: str
    score: float = Field(description="Score copied exactly from a tool result")
    note: str = Field(description="Main driver or a short comment")


class KeyFigure(_Strict):
    label: str = Field(description="What the number is, e.g. 'Denver snow days 2025'")
    value: float = Field(description="Copied exactly from a tool result, never calculated")
    unit: str = Field(description="e.g. '%', 'score 0-100', 'days/yr', 'events/yr'")
    source_tool: str = Field(description="The tool whose result contains this number")


class AgentAnswer(_Strict):
    answer: str = Field(
        description="Direct answer for the analyst, 2-6 sentences, plain text or light markdown"
    )
    answer_type: Literal[
        "ranking",
        "comparison",
        "statistic",
        "explanation",
        "methodology",
        "clarification",
        "out_of_scope",
    ]
    hubs: list[str] = Field(description="Hub ids this answer is about")
    ranking: list[RankedHub] = Field(description="Ordered hubs for ranking answers, else []")
    key_figures: list[KeyFigure] = Field(description="The numbers the answer relies on")
    reasoning: list[str] = Field(description="Short steps: what was checked and why it matters")
    assumptions: list[str] = Field(description="Definitions, thresholds and scoping choices used")
    data_sources: list[str] = Field(description="Datasets behind this answer")
    confidence: Literal["high", "medium", "low"]
    confidence_reason: str = Field(description="One sentence on what limits confidence")
    follow_up_suggestions: list[str] = Field(description="1-3 useful next questions")
