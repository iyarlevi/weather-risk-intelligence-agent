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

## Risk KPI (deterministic): as built in stage 3
Full definition: `config/scoring.yaml`. Code: `app/scoring/`.

| Hazard (weight) | Components |
|---|---|
| Winter (27%) | snow days ≥1 cm/yr, heavy-snow days ≥5 cm/yr, hard-freeze days ≤−15 °C/yr, FEMA winter-weather and ice-storm frequency |
| Hurricane & wind (26%) | FEMA hurricane and strong-wind frequency, damaging-gust days ≥75 km/h/yr, strongest gust |
| Flood (27%) | FEMA inland and coastal flood frequency, heavy-rain days ≥25 mm/yr, extreme-rain days ≥50 mm/yr, wettest day |
| Tornado & hail (15%) | FEMA tornado and hail frequency (daily reanalysis can't see these storms) |
| Heat (5%) | hot days ≥35 °C/yr, extreme-heat days ≥38 °C/yr, FEMA heat-wave frequency |

- Each component is mapped onto 0–100 using a **fixed scale** (`lo`, `hi`), not min-max across
  hubs. A hub's score then changes only when its own data changes, which keeps alerts meaningful.
- Hazard score = weighted average of its components. Composite = weighted average of the hazards.
- Every result carries the full breakdown (raw value → scaled score → weight → contribution).
  That's how the agent answers "why".

**Design decisions found in the data (stage 3):**
- FEMA **risk percentiles** include exposed property value, so every large urban county scores
  95–100 for flood and heat. We use FEMA **annual event frequency** instead, which measures the
  hazard itself.
- FEMA cold-wave and heat-wave frequencies are relative to local climate (Miami shows 1.3 cold
  waves a year). Cold wave is dropped, and heat wave gets a lower weight than our measured hot days.
- ERA5 smooths extremes. Hurricanes show up in 5 years only as single events (Ida at New Orleans
  in 2021, Beryl at Houston in 2024), so FEMA's long record leads the hurricane score.
- FEMA data is per county. King County (Seattle) and Salt Lake County include mountains,
  which inflates their FEMA winter frequency.

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
8. **Alerts (bonus)**: scheduled re-score, diff against the last snapshot, webhook POST. Built as `app/alerts/` (daily APScheduler job + `POST /alerts/check`); a change is a move of at least 2 points or a tier change
9. **Docs and deploy**: ARCHITECTURE.md, README run instructions, Dockerfile, deploy
10. **Session export**: save this AI session transcript into `docs/ai-session/` (mandatory deliverable)

## Scope and assumptions
- About 20 hubs at representative metro locations (`config/hubs.yaml`). We assume the hubs are fictional and placed in real US cities.
- History window: the last 5 full calendar years. "Last year" means the previous calendar year.
- ERA5 reanalysis is gridded (~25 km), so these are point estimates, not station observations.
- FEMA NRI is county-level, relative and static. It measures long-term hazard exposure, not forecasts.
- The risk score measures **exposure**, not financial loss. We have no hub cost or throughput data.
