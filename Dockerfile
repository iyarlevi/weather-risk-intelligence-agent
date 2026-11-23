# Container for the API + chat UI. Build:  docker build -t wria .
# Run:    docker run -p 8000:8000 -e ANTHROPIC_API_KEY=... wria
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
# Compile .pyc at build time (faster cold start); copy files instead of hard-linking from cache.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies first, in their own layer: code edits don't re-download packages.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Only what the app needs at runtime (tests, evals and docs stay out of the image).
COPY app ./app
COPY config ./config
COPY web ./web
# The starting database: on first start it's copied to data/wria.db, so no download is needed.
COPY data/seed ./data/seed

# Run as a normal user; it only needs to write to data/ (the working database).
RUN useradd --create-home appuser && chown -R appuser /app/data
USER appuser

ENV PATH="/app/.venv/bin:$PATH" PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health')"
# One worker on purpose: the alert scheduler runs inside the process, and SQLite has one writer.
# $PORT lets hosts like Render choose the port. --proxy-headers: the host's proxy sets the scheme.
CMD ["sh", "-c", "exec uvicorn app.api.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips='*'"]
