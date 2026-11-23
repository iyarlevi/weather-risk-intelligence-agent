# Working Plan — Weather Risk Intelligence Agent

## Goal
An AI agent that helps logistics analysts decide which US distribution hubs are most
exposed to weather disruption. It answers ranking, comparison, statistics and "why"
questions. The numbers come from deterministic scoring code, and the LLM only explains them.

## Guiding principle
**The LLM never invents numbers.** All metrics, scores and rankings come from
deterministic Python code over public data. The LLM's jobs are:
1. understanding the question and choosing tools (tool calling),
2. writing a clear explanation,
3. returning that explanation in an enforced JSON schema.

## Tech stack

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12 | Preferred by the assignment |
| Package manager | `uv` | Fast, reproducible lockfile |
| API | FastAPI + Uvicorn | Async, Pydantic-native, auto OpenAPI docs |
| LLM | Anthropic Claude (tool use + structured output) | Strong tool calling and JSON-schema-constrained output |
| Schemas | Pydantic v2 | One source of truth for the JSON schema used by the LLM and the API |
| Storage | SQLite | Zero-ops, file-based, enough for cache + score history + alerts |
| HTTP | httpx | Async client with timeouts and retries |
| Chat UI | Static HTML/JS served by FastAPI | Single deploy unit; Web Speech API gives voice for free (bonus) |
| Alerts | APScheduler + webhook POST | Scheduled re-score and notify on change (bonus) |
| Evals | pytest-style runner over `evals/cases.yaml` | Small, repeatable, runnable in CI |
| Deploy | Dockerfile → Render / Fly.io | One URL for the interview |

## Public data sources (no API keys needed)

| Source | What we use | Endpoint |
|---|---|---|
| Open-Meteo Historical (ERA5) | Daily snowfall, precipitation, min/max temp, max wind gusts for 2020–2025 | `archive-api.open-meteo.com/v1/archive` |
| FEMA National Risk Index | County-level hazard risk scores (hurricane, coastal/riverine flood, winter weather, ice storm, tornado, heat, …) | NRI ArcGIS feature service / CSV |
| NOAA NWS Alerts | Active alerts for a hub's location (live context) | `api.weather.gov/alerts/active?point=lat,lon` |

## Risk KPI (deterministic)
For each hub we compute **hazard sub-scores (0–100)**:

- **Winter**: % of days with snowfall ≥ 1 cm, % of days with min temp ≤ −10 °C, FEMA winter weather + ice storm rating
- **Hurricane / wind**: days with gusts ≥ 90 km/h, FEMA hurricane rating
- **Flood / heavy rain**: days with precipitation ≥ 50 mm, FEMA riverine + coastal flood rating
- **Heat**: days with max temp ≥ 38 °C, FEMA heat wave rating
- **Severe convective**: FEMA tornado + hail + strong wind rating

Each raw metric is min-max normalised across all hubs, then combined with fixed, documented weights.
**Composite Disruption Risk = weighted sum of the sub-scores**, and the weights live in `config/scoring.yaml`.
Every score carries its component breakdown, so "why is Dallas high?" can be answered from the data.

## Agent design
- Tools exposed to the LLM: `list_hubs`, `rank_hubs`, `get_hub_risk`, `compare_hubs`,
  `weather_stat` (e.g. % snow days in a year), `active_alerts`.
- Loop: user message + conversation history → Claude → tool calls → tool results → final answer.
- Final answer must match `AgentAnswer` (JSON schema): `answer`, `hubs`, `ranking`,
  `reasoning`, `data_sources`, `assumptions`, `confidence`.
- Conversation memory: session id → message history in SQLite (supports follow-ups).

## Stages

1. **Scaffold**: repo structure, config, hub list, plan doc *(this commit)*
2. **Data layer**: Open-Meteo, FEMA NRI and NWS clients plus a SQLite cache
3. **Scoring engine**: metrics, normalisation, composite score and unit tests
4. **Agent**: tool definitions, Pydantic schemas, Claude tool-use loop, structured output
5. **API**: FastAPI `/chat`, `/hubs`, `/hubs/{id}/risk`, `/health`
6. **Chat UI**: static page with conversation, ranking table and voice input
7. **Evaluation**: `evals/cases.yaml` plus a runner script (tool choice, numeric correctness, schema validity)
8. **Alerts (bonus)**: scheduled re-score, diff against the last snapshot, webhook POST
9. **Docs and deploy**: ARCHITECTURE.md, README run instructions, Dockerfile, deploy
10. **Session export**: save this AI session transcript into `docs/ai-session/` (mandatory deliverable)

## Scope and assumptions
- About 20 hubs at representative metro locations (`config/hubs.yaml`). We assume the hubs are fictional and placed in real US cities.
- History window: the last 5 full calendar years. "Last year" means the previous calendar year.
- ERA5 reanalysis is gridded (~25 km), so these are point estimates, not station observations.
- FEMA NRI is county-level, relative and static. It measures long-term hazard exposure, not forecasts.
- The risk score measures **exposure**, not financial loss. We have no hub cost or throughput data.
