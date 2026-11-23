# Weather Risk Intelligence Agent

An AI agent that helps logistics analysts decide which US distribution hubs are most
exposed to weather disruption. It ranks, compares and explains hub risk, and it bases
its answers on public weather and hazard data plus a deterministic scoring model.

**Live demo:** <https://weather-risk-intelligence-agent-8l6j.onrender.com> (API docs at
[`/docs`](https://weather-risk-intelligence-agent-8l6j.onrender.com/docs)). It runs on Render's
free plan, so after ~15 minutes idle the first request can take about a minute while it wakes up.

Design and architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · build plan and
scoring rationale: [docs/PLAN.md](docs/PLAN.md).
The full AI agent session that built it: [docs/ai-session/](docs/ai-session/README.md).

## Quick start

```bash
uv sync
echo "ANTHROPIC_API_KEY=your-key" > .env
uv run uvicorn app.api.main:app --reload
```

No download step is needed: the repo ships a starting copy of the database
(`data/seed/wria-seed.db`, 3.4 MB, the 2021-2025 weather and FEMA data plus a baseline score
snapshot). On first start it's copied to `data/wria.db`, the working database. To fetch fresh
data instead, run `uv run python -m app.data_sources.ingest` (~1-2 min), then rebuild the
starting copy with `uv run python -m app.storage.seed`.

Optional settings, also read from `.env` (or environment variables):

| Variable | Default | What it does |
|---|---|---|
| `DATABASE_PATH` | `data/wria.db` | Where the working SQLite database lives |
| `ALERT_WEBHOOK_URL` | empty | Where risk-change alerts are POSTed (e.g. a Slack webhook) |
| `ALERT_THRESHOLD_POINTS` | `2.0` | Smallest score move that raises an alert |
| `ALERT_SCHEDULE_CRON` | `0 6 * * *` | When the daily alert check runs; empty turns it off |

## Run with Docker

```bash
docker build -t wria .
docker run -p 8000:8000 --env-file .env wria   # then open http://localhost:8000
```

## Chat UI

Start the server and open <http://localhost:8000>:

```bash
uv run uvicorn app.api.main:app --reload
```

- **Chat** with follow-up memory. Each answer shows its ranking table or key figures, a
  confidence badge, and expandable reasoning, assumptions and data sources.
- **Hub ranking panel** from the deterministic model (no AI), filterable by hazard and region.
  Hidden by default; open it with "📊 Show hub ranking". Click a hub to ask why it scores that way.
- **Voice (bonus)**: 🎤 for speech-to-text and "Read aloud" for spoken answers, both through
  the browser's Web Speech API (Chrome, Edge, Safari). No extra server or keys needed.
- Plain HTML/CSS/JS in `web/`, served by FastAPI. No build step. Light and dark mode, works on phones.

## API

Start the server, then open the interactive docs at <http://localhost:8000/docs>:

```bash
uv run uvicorn app.api.main:app --reload
```

| Method | Path | What it does | Calls the LLM? |
|---|---|---|---|
| `POST` | `/chat` | Ask the agent. Body: `{"message": "...", "session_id": "optional"}`. Reuse the returned `session_id` for follow-ups. | Yes |
| `GET` | `/chat/{session_id}` | Past questions and answers (the UI redraws them after a refresh) | No |
| `DELETE` | `/chat/{session_id}` | Forget a conversation | No |
| `GET` | `/hubs?region=` | Hub list | No |
| `GET` | `/hubs/{hub}/risk` | Full score breakdown for one hub | No |
| `GET` | `/rankings?hazard=&region=&limit=` | Ranking by composite or one hazard | No |
| `GET` | `/methodology` | Weights, scales, limitations | No |
| `GET` | `/health` | Data loaded? API key configured? | No |
| `POST` | `/alerts/check?notify=true` | Re-score now, compare with the last snapshot, send alerts | No |
| `GET` | `/alerts?limit=` | Recent risk-change alerts | No |

```bash
curl -s -X POST localhost:8000/chat -H 'content-type: application/json' \
  -d '{"message": "Compare Miami and Houston in terms of hurricane and flood exposure."}'
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
Dockerfile       Container image (API + UI + starting database)
render.yaml      Render deployment
```

## Data sources

| Source | Used for | Key needed |
|---|---|---|
| [Open-Meteo Historical API](https://open-meteo.com/en/docs/historical-weather-api) (ERA5) | Daily snowfall, precipitation, min/max temperature, max wind gusts, last 5 full years | No |
| [FEMA National Risk Index](https://hazards.fema.gov/nri/) | County hazard risk percentiles and ratings (hurricane, flood, winter weather, ice storm, cold/heat wave, tornado, hail, strong wind) | No |
| [NOAA NWS Alerts](https://www.weather.gov/documentation/services-web-api) | Live active alerts at each hub | No |

The ingest is idempotent: it skips data that is already cached, and it waits and retries
when Open-Meteo's free tier rate-limits it. Run it with `--refresh` to re-download everything.

## Risk-change alerts (bonus)

The server re-scores every hub once a day (06:00, set by `ALERT_SCHEDULE_CRON`) and compares
the result with the previous snapshot stored in SQLite. It alerts when a hub's overall or hazard
score moves by at least `ALERT_THRESHOLD_POINTS` (default 2.0) **or** changes tier
(e.g. Elevated -> High). Alerts are POSTed as JSON to `ALERT_WEBHOOK_URL`. The payload has a
`text` field, so a Slack incoming webhook works as-is. With no URL set, alerts are logged and
stored, and `GET /alerts` lists them. The first check only records a baseline.

Each alert says why the scores could have moved: `config_changed` (scoring.yaml was edited) or
`data_window_changed` (a new year of history rolled in). The scores are built from history,
so they move rarely: when a new year of data arrives, when FEMA updates its index, or when the
weights change.

```bash
uv run python -m app.alerts.monitor             # run a check now (also: POST /alerts/check)
uv run python -m app.alerts.monitor --no-notify # detect and store, don't call the webhook
```

To see an alert fire: run a check, change a hazard weight in `config/scoring.yaml` (the
weights must still sum to 1), and run the check again.

## Evaluation

17 cases graded by code (tool choice, exact numbers from the engine, rankings, follow-ups, and
honesty on out-of-scope questions). Details and baseline: [evals/README.md](evals/README.md).

```bash
uv run python -m evals.run
```

## Tests

```bash
uv run pytest
```

## AI Session

https://claude.ai/code/cshk_0119uiYWrp6Q9Mu4rn8XrgWMFLBdHBw8VtnB5bsgyKACha
