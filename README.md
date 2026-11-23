# Weather Risk Intelligence Agent

An AI agent that helps logistics analysts decide which US distribution hubs are most
exposed to weather disruption. It ranks, compares and explains hub risk, and it bases
its answers on public weather and hazard data plus a deterministic scoring model.

> Status: work in progress. See [docs/PLAN.md](docs/PLAN.md) for the stages.

## Quick start

```bash
uv sync
cp .env.example .env   # add your ANTHROPIC_API_KEY
uv run python -m app.data_sources.ingest   # download weather + hazard data into SQLite (~1-2 min)
uv run uvicorn app.api.main:app --reload
```

## Ask the agent from the terminal

```bash
uv run python -m app.agent.agent "Which hubs in the Midwest are most exposed to winter disruption?"
# Several questions in one session (the follow-up keeps the context):
uv run python -m app.agent.agent "Why is Dallas high risk?" "How does it compare to Houston?"
# Add --json to also print the structured answer
```

## How the agent works

1. Claude (`claude-opus-5-5`) gets the question, the conversation history, and 7 tools:
   `list_hubs`, `rank_hubs`, `get_hub_risk`, `compare_hubs`, `weather_stat`,
   `active_alerts`, `get_methodology`.
2. Every tool runs deterministic Python (the scoring engine, SQLite and NOAA). **The LLM never
   calculates a number.** It only picks tools and explains their results.
3. The final reply must match the `AgentAnswer` JSON schema (`app/agent/schemas.py`). The API
   enforces it with `output_config.format`, and Pydantic validates it again in our code.
4. A grounding check confirms that every number in `key_figures`/`ranking` appears in a tool
   result, and that every hub id is real. Failures come back as `grounding_warnings`.
5. Chat history is stored per `session_id` in SQLite, so follow-up questions keep their context.

## Repository layout

```
app/
  api/           FastAPI app (chat + hub endpoints)
  agent/         LLM loop, tool definitions, JSON-schema output models
  data_sources/  Open-Meteo, FEMA NRI, NWS clients
  scoring/       Deterministic risk KPI
  storage/       SQLite cache, sessions, score history
  alerts/        Scheduled re-scoring + webhook alerts
web/             Chat UI (static HTML/JS, voice input)
config/          Hub list and scoring weights
evals/           Evaluation cases + runner
tests/           Unit tests
docs/            Plan, architecture, AI session log
```

## Data sources

| Source | Used for | Key needed |
|---|---|---|
| [Open-Meteo Historical API](https://open-meteo.com/en/docs/historical-weather-api) (ERA5) | Daily snowfall, precipitation, min/max temperature, max wind gusts, last 5 full years | No |
| [FEMA National Risk Index](https://hazards.fema.gov/nri/) | County hazard risk percentiles and ratings (hurricane, flood, winter weather, ice storm, cold/heat wave, tornado, hail, strong wind) | No |
| [NOAA NWS Alerts](https://www.weather.gov/documentation/services-web-api) | Live active alerts at each hub | No |

The ingest is idempotent: it skips data that is already cached, and it waits and retries
when Open-Meteo's free tier rate-limits it. Run it with `--refresh` to re-download everything.

## Tests

```bash
uv run pytest
```
