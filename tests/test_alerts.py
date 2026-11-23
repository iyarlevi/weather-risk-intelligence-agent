"""Risk-change alerts (stage 8). Offline: the webhook is faked, and every test that writes
snapshots uses a temporary copy of the database, never the real one."""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.alerts import monitor
from app.api.main import app, get_conn
from app.config import get_settings
from app.storage import db


def snap(composite, tier, winter=50.0, winter_tier="Elevated"):
    return {
        "composite": composite,
        "tier": tier,
        "hazards": {"winter": {"score": winter, "tier": winter_tier}},
    }


# ------------------------------------------------------------------ pure diff logic


def test_no_change_below_threshold():
    old = {"chicago": snap(43.2, "Elevated")}
    new = {"chicago": snap(44.0, "Elevated", winter=51.5)}
    assert monitor.diff_snapshots(old, new, threshold=2.0) == []


def test_move_over_threshold_is_reported():
    old = {"chicago": snap(43.2, "Elevated")}
    new = {"chicago": snap(46.0, "Elevated")}
    [c] = monitor.diff_snapshots(old, new, threshold=2.0)
    assert (c.hub_id, c.metric, c.delta, c.tier_changed) == ("chicago", "composite", 2.8, False)


def test_tier_change_always_reported_and_sorted_first():
    # 59.9 -> 60.1 is a tiny move but crosses Elevated -> High: that must alert.
    old = {"chicago": snap(43.2, "Elevated"), "denver": snap(59.9, "Elevated")}
    new = {"chicago": snap(48.0, "Elevated"), "denver": snap(60.1, "High")}
    changes = monitor.diff_snapshots(old, new, threshold=2.0)
    assert [(c.hub_id, c.tier_changed) for c in changes] == [("denver", True), ("chicago", False)]


def test_hazard_level_change_and_new_hubs_skipped():
    old = {"chicago": snap(43.2, "Elevated", winter=58.4)}
    new = {
        "chicago": snap(43.2, "Elevated", winter=62.0, winter_tier="High"),
        "portland": snap(30.0, "Moderate"),
    }  # not in the old snapshot: nothing to compare
    [c] = monitor.diff_snapshots(old, new, threshold=2.0)
    assert (c.metric, c.label, c.old_tier, c.new_tier) == (
        "winter",
        "Winter storms & extreme cold",
        "Elevated",
        "High",
    )


def test_message_mentions_hub_scores_and_cause():
    old = {"denver": snap(59.9, "Elevated")}
    new = {"denver": snap(60.1, "High")}
    text = monitor.format_message(monitor.diff_snapshots(old, new, 2.0), True, False)
    assert "Denver" in text and "59.9 -> 60.1" in text and "Elevated -> High" in text
    assert "scoring model updated" in text


def test_webhook_without_url_is_not_an_error():
    ok, detail = monitor.send_webhook("", {"text": "x"})
    assert not ok and "no ALERT_WEBHOOK_URL" in detail


# ------------------------------------------------------- end to end on a temp database

needs_data = pytest.mark.skipif(
    not get_settings().database_path.exists(),
    reason="run `python -m app.data_sources.ingest` first",
)


@pytest.fixture
def temp_conn(tmp_path):
    """A copy of the real database, so the test can write snapshots freely."""
    src = sqlite3.connect(get_settings().database_path)
    conn = db.connect(tmp_path / "copy.db")
    src.backup(conn)
    src.close()
    conn.execute("DELETE FROM score_snapshots")
    conn.execute("DELETE FROM risk_alerts")
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def fake_webhook(monkeypatch):
    sent = []
    settings = get_settings().model_copy(update={"alert_webhook_url": "https://hooks.example/x"})
    monkeypatch.setattr(monitor, "get_settings", lambda: settings)
    monkeypatch.setattr(
        monitor, "send_webhook", lambda url, payload: (sent.append(payload), (True, "HTTP 200"))[1]
    )
    return sent


def _lower_stored_score(conn, hub, points):
    """Pretend the previous check saw a lower score, as if new data had pushed it up."""
    snap_row = db.latest_snapshot(conn)
    snap_row["scores"][hub]["composite"] -= points
    conn.execute(
        "UPDATE score_snapshots SET scores = ? WHERE id = ?",
        (json.dumps(snap_row["scores"]), snap_row["id"]),
    )
    conn.commit()


@needs_data
def test_first_check_records_baseline_then_no_changes(temp_conn, fake_webhook):
    first = monitor.run_check(temp_conn)
    assert first.baseline and not first.changes
    second = monitor.run_check(temp_conn)
    assert not second.baseline and second.previous_snapshot_id == first.snapshot_id
    assert second.changes == [] and not second.config_changed and fake_webhook == []


@needs_data
def test_score_change_fires_webhook_and_is_stored(temp_conn, fake_webhook):
    monitor.run_check(temp_conn)
    _lower_stored_score(temp_conn, "houston", 5)
    result = monitor.run_check(temp_conn)

    assert [(c.hub_id, c.metric) for c in result.changes] == [("houston", "composite")]
    assert result.changes[0].delta == 5.0 and result.delivered
    [payload] = fake_webhook
    assert payload["event"] == "hub_risk_changed" and "Houston" in payload["text"]
    [row] = db.recent_alerts(temp_conn)
    assert row["hub_id"] == "houston" and row["delivered"] == 1


@needs_data
def test_alert_endpoints(temp_conn, fake_webhook):
    def conn_override():
        yield temp_conn

    app.dependency_overrides[get_conn] = conn_override
    try:
        with TestClient(app) as client:
            assert client.post("/alerts/check").json()["baseline"] is True
            _lower_stored_score(temp_conn, "miami", 3)
            body = client.post("/alerts/check").json()
            assert body["changes"][0]["hub_id"] == "miami"
            assert client.get("/alerts").json()[0]["hub_id"] == "miami"
    finally:
        app.dependency_overrides.clear()
