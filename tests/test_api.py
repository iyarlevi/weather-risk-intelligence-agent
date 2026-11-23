"""API tests. The LLM is replaced by a stub agent, so these run offline and for free."""

import pytest
from fastapi.testclient import TestClient

from app.agent.agent import AgentError, AgentResponse
from app.api.main import app, get_agent
from app.config import get_settings
from tests.test_agent import make_answer

pytestmark = pytest.mark.skipif(
    not get_settings().database_path.exists(),
    reason="run `python -m app.data_sources.ingest` first",
)


class StubAgent:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = []

    def chat(self, message, session_id=None):
        self.calls.append((message, session_id))
        if self.fail:
            raise AgentError("LLM unavailable")
        return AgentResponse(
            session_id=session_id or "new-session",
            answer=make_answer(answer=f"echo: {message}"),
            tool_calls=[],
            grounding_warnings=[],
            model="stub",
            usage={},
            latency_ms=1,
        )


@pytest.fixture
def client():
    with TestClient(app) as c:  # `with` runs the startup warm-up
        yield c
    app.dependency_overrides.clear()


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["hubs"] == 21 and body["weather_days"] > 30_000


def test_hubs_and_region_filter(client):
    assert len(client.get("/hubs").json()) == 21
    midwest = client.get("/hubs", params={"region": "Midwest"}).json()
    assert {h["region"] for h in midwest} == {"Midwest"}
    assert client.get("/hubs", params={"region": "Mars"}).status_code == 422


def test_hub_risk(client):
    body = client.get("/hubs/Salt Lake City/risk").json()
    assert body["hub_id"] == "salt_lake" and set(body["hazards"]) >= {"winter", "heat"}
    assert client.get("/hubs/gotham/risk").status_code == 404


def test_rankings(client):
    top = client.get("/rankings", params={"hazard": "heat", "limit": 1}).json()
    assert top[0]["hub_id"] == "phoenix"
    assert client.get("/rankings", params={"hazard": "volcano"}).status_code == 422


def test_methodology(client):
    body = client.get("/methodology").json()
    assert abs(sum(h["weight"] for h in body["hazards"].values()) - 1) < 1e-9


def test_chat_passes_session_through(client):
    stub = StubAgent()
    app.dependency_overrides[get_agent] = lambda: stub
    r = client.post("/chat", json={"message": "  hi  ", "session_id": "abc"})
    assert r.status_code == 200
    assert r.json()["session_id"] == "abc"
    assert r.json()["answer"]["answer"] == "echo: hi"  # message is trimmed
    assert stub.calls == [("hi", "abc")]


def test_chat_validation_and_errors(client):
    app.dependency_overrides[get_agent] = lambda: StubAgent(fail=True)
    assert client.post("/chat", json={"message": ""}).status_code == 422
    assert client.post("/chat", json={"message": "x" * 2001}).status_code == 422
    r = client.post("/chat", json={"message": "hi"})
    assert r.status_code == 502 and "LLM unavailable" in r.json()["detail"]


def test_chat_history_rebuilds_turns(client):
    from app.storage import db

    conn = db.connect()
    answer = make_answer(answer="Denver had 4.7%").model_dump_json()
    db.save_session(
        conn,
        "hist-test",
        [
            {"role": "user", "content": "snow in denver?"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "weather_stat", "input": {}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{}"}],
            },
            {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": answer}],
            },
        ],
    )
    turns = client.get("/chat/hist-test").json()
    assert len(turns) == 1
    assert turns[0]["question"] == "snow in denver?"
    assert turns[0]["answer"]["answer"] == "Denver had 4.7%"
    assert turns[0]["tools"] == ["weather_stat"]
    assert client.get("/chat/no-such-session").json() == []
    db.delete_session(conn, "hist-test")
