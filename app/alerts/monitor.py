"""Risk-change alerts: re-score every hub, compare with the last snapshot, notify on changes.

Flow of one check (`run_check`):
  1. (optional) pull any missing source data (same idempotent ingest as the CLI),
  2. reload scoring.yaml and re-score all hubs (the deterministic engine, no LLM),
  3. diff against the previous snapshot: a change is reported when a score moves by at least
     `alert_threshold_points`, or when its tier changes (Elevated -> High, ...),
  4. POST the changes to ALERT_WEBHOOK_URL (Slack-compatible: it has a "text" field),
  5. store the new snapshot and the alerts, so the next check diffs against this one.

The first check ever has nothing to compare with: it only records a baseline.

Usage:
    uv run python -m app.alerts.monitor                 # check now
    uv run python -m app.alerts.monitor --refresh-data  # fetch missing data first
    uv run python -m app.alerts.monitor --no-notify     # detect + store, don't call the webhook
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import threading
from datetime import UTC, datetime

import httpx
from pydantic import BaseModel, Field

from app.agent.tools import SCORES
from app.config import get_settings, load_hubs
from app.data_sources.ingest import history_window, ingest_nri, ingest_weather
from app.scoring.engine import config_fingerprint, load_scoring_config
from app.scoring.models import HubRisk
from app.storage import db

log = logging.getLogger("wria.alerts")

# The scheduler and POST /alerts/check could fire together: one check at a time.
_CHECK_LOCK = threading.Lock()


class ScoreChange(BaseModel):
    hub_id: str
    hub_name: str
    metric: str = Field(description='"composite" or a hazard key such as "winter"')
    label: str
    old_score: float
    new_score: float
    delta: float
    old_tier: str
    new_tier: str
    tier_changed: bool


class CheckResult(BaseModel):
    checked_at: str
    snapshot_id: int
    previous_snapshot_id: int | None
    baseline: bool = Field(description="True on the first check: nothing to compare with yet")
    # Why scores could have moved. Scores only change when the inputs change, so this tells
    # the reader whether it's new data or a new model.
    config_changed: bool
    data_window_changed: bool
    threshold_points: float
    changes: list[ScoreChange]
    delivered: bool
    delivery_detail: str


# ---------------------------------------------------------------------- pure logic


def snapshot_of(scores: dict[str, HubRisk]) -> dict:
    """The part of the scores worth diffing: composite + every hazard, with tiers."""
    return {
        hub_id: {
            "composite": r.composite_score,
            "tier": r.tier,
            "hazards": {k: {"score": h.score, "tier": h.tier} for k, h in r.hazards.items()},
        }
        for hub_id, r in scores.items()
    }


def diff_snapshots(old: dict, new: dict, threshold: float) -> list[ScoreChange]:
    """Changes between two snapshots, biggest moves first. Pure function: easy to test."""
    names = {h.id: h.name for h in load_hubs()}
    labels = {k: v["label"] for k, v in load_scoring_config()["hazards"].items()}
    changes = []

    def check(hub_id, metric, label, o_score, n_score, o_tier, n_tier):
        delta = round(n_score - o_score, 1)
        if abs(delta) >= threshold or o_tier != n_tier:
            changes.append(
                ScoreChange(
                    hub_id=hub_id,
                    hub_name=names.get(hub_id, hub_id),
                    metric=metric,
                    label=label,
                    old_score=o_score,
                    new_score=n_score,
                    delta=delta,
                    old_tier=o_tier,
                    new_tier=n_tier,
                    tier_changed=o_tier != n_tier,
                )
            )

    # Hubs that exist in only one snapshot (hub added/removed in hubs.yaml) are skipped:
    # there is no "before" to compare with.
    for hub_id in sorted(old.keys() & new.keys()):
        o, n = old[hub_id], new[hub_id]
        check(hub_id, "composite", "Overall", o["composite"], n["composite"], o["tier"], n["tier"])
        for hz in sorted(o["hazards"].keys() & n["hazards"].keys()):
            oh, nh = o["hazards"][hz], n["hazards"][hz]
            check(hub_id, hz, labels.get(hz, hz), oh["score"], nh["score"], oh["tier"], nh["tier"])

    # Tier changes first (they matter most), then the largest moves.
    changes.sort(key=lambda c: (not c.tier_changed, -abs(c.delta), c.hub_id, c.metric))
    return changes


def format_message(changes: list[ScoreChange], config_changed: bool, window_changed: bool) -> str:
    """Human-readable summary (Slack shows the "text" field of the payload)."""
    cause = []
    if config_changed:
        cause.append("scoring model updated")
    if window_changed:
        cause.append("new history window")
    head = f"Weather risk changed for {len({c.hub_id for c in changes})} hub(s)"
    lines = [head + (f" ({', '.join(cause)})" if cause else "") + ":"]
    for c in changes[:15]:  # keep the chat message short; the payload has all of them
        tier = f", {c.old_tier} -> {c.new_tier}" if c.tier_changed else ""
        lines.append(
            f"- {c.hub_name} {c.label}: {c.old_score:.1f} -> {c.new_score:.1f} "
            f"({c.delta:+.1f}{tier})"
        )
    if len(changes) > 15:
        lines.append(f"... and {len(changes) - 15} more")
    return "\n".join(lines)


# ------------------------------------------------------------------- side effects


def send_webhook(url: str, payload: dict) -> tuple[bool, str]:
    """POST the alert. Never raises: a broken webhook must not break the check itself."""
    if not url:
        return False, "no ALERT_WEBHOOK_URL set (alert logged and stored only)"
    try:
        r = httpx.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True, f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        log.warning("alert webhook failed: %s", exc)
        return False, f"webhook failed: {exc}"


def run_check(
    conn: sqlite3.Connection, *, refresh_data: bool = False, notify: bool = True
) -> CheckResult:
    settings = get_settings()
    with _CHECK_LOCK:
        if refresh_data:
            ingest_nri(conn)  # fetch-if-missing; cheap when everything is cached
            ingest_weather(conn)

        # Pick up edits to scoring.yaml and new data: drop the cached config and scores.
        # This also means the chat agent sees the new scores right after the check.
        load_scoring_config.cache_clear()
        SCORES.invalidate()
        scores = SCORES.get(conn)

        start, end = history_window()
        window, fingerprint = f"{start}..{end}", config_fingerprint()
        new = snapshot_of(scores)
        prev = db.latest_snapshot(conn)
        checked_at = datetime.now(UTC).isoformat(timespec="seconds")

        if prev is None:
            changes, config_changed, window_changed = [], False, False
        else:
            changes = diff_snapshots(prev["scores"], new, settings.alert_threshold_points)
            config_changed = prev["config_fingerprint"] != fingerprint
            window_changed = prev["data_window"] != window

        snapshot_id = db.save_snapshot(conn, fingerprint, window, new)

        delivered, detail = False, "no changes"
        if prev is None:
            detail = "baseline recorded (first check)"
        elif changes:
            text = format_message(changes, config_changed, window_changed)
            log.warning(text)  # always visible in the server log, webhook or not
            if notify:
                payload = {
                    "text": text,
                    "event": "hub_risk_changed",
                    "checked_at": checked_at,
                    "config_changed": config_changed,
                    "data_window_changed": window_changed,
                    "threshold_points": settings.alert_threshold_points,
                    "changes": [c.model_dump() for c in changes],
                }
                delivered, detail = send_webhook(settings.alert_webhook_url, payload)
            else:
                detail = "notification skipped (notify off)"
            db.save_alerts(conn, snapshot_id, [c.model_dump() for c in changes], delivered, detail)

        return CheckResult(
            checked_at=checked_at,
            snapshot_id=snapshot_id,
            previous_snapshot_id=prev["id"] if prev else None,
            baseline=prev is None,
            config_changed=config_changed,
            data_window_changed=window_changed,
            threshold_points=settings.alert_threshold_points,
            changes=changes,
            delivered=delivered,
            delivery_detail=detail,
        )


def scheduled_check() -> None:
    """Entry point for the scheduler: own connection, fetch new data, never raise."""
    conn = db.connect()
    try:
        result = run_check(conn, refresh_data=True)
        log.info(
            "scheduled alert check: %d change(s), %s", len(result.changes), result.delivery_detail
        )
    except Exception:  # a failed run is logged and retried at the next scheduled time
        log.exception("scheduled alert check failed")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--refresh-data", action="store_true", help="fetch missing data first")
    parser.add_argument("--no-notify", action="store_true", help="don't call the webhook")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = run_check(db.connect(), refresh_data=args.refresh_data, notify=not args.no_notify)
    if result.baseline:
        print(f"Baseline snapshot #{result.snapshot_id} recorded. Run again later to compare.")
    elif not result.changes:
        print(f"No changes since snapshot #{result.previous_snapshot_id}.")
    else:
        print(format_message(result.changes, result.config_changed, result.data_window_changed))
        print(f"Delivery: {result.delivery_detail}")


if __name__ == "__main__":
    main()
