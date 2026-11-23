"""Shared HTTP helper with timeouts and simple retry/backoff."""

from __future__ import annotations

import time

import httpx

from app.config import get_settings


class SourceError(RuntimeError):
    """A public data source failed after retries."""


RATE_LIMIT_WAIT_S = 65  # Open-Meteo's free tier limits are per minute


def get_json(url: str, params: dict | None = None, retries: int = 4, timeout: float = 30.0):
    headers = {"User-Agent": get_settings().http_user_agent, "Accept": "application/json"}
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = httpx.get(url, params=params, headers=headers, timeout=timeout)
            # 429 = "too many requests": wait for the rate-limit window, then retry.
            if resp.status_code == 429 and attempt < retries - 1:
                wait = float(resp.headers.get("Retry-After", RATE_LIMIT_WAIT_S))
                print(f"    rate limited by {httpx.URL(url).host}, waiting {wait:.0f}s ...")
                time.sleep(wait)
                continue
            # 5xx = temporary server problem: worth retrying. 4xx = our mistake: fail fast.
            if resp.status_code == 429 or resp.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp
                )
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(2**attempt)  # exponential backoff: 1s, 2s, 4s ...
    raise SourceError(f"GET {url} failed: {last_exc}") from last_exc
