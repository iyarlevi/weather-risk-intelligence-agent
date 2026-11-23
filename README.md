# Weather Risk Intelligence Agent

An AI agent that helps logistics analysts decide which US distribution hubs are most
exposed to weather disruption. It ranks, compares and explains hub risk, and it bases
its answers on public weather and hazard data plus a deterministic scoring model.

> Status: work in progress. See [docs/PLAN.md](docs/PLAN.md) for the stages.

## Quick start

```bash
uv sync
cp .env.example .env   # add your ANTHROPIC_API_KEY
uv run uvicorn app.api.main:app --reload
```

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
