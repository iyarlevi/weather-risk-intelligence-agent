"""Offline tests for the agent: tools, schema, grounding, and the loop (with a fake Claude)."""

import json
from types import SimpleNamespace

import pytest

from app.agent import tools
from app.agent.agent import AgentError, WeatherRiskAgent
from app.agent.grounding import check_grounding
from app.agent.schemas import AgentAnswer
from app.config import get_settings
from app.storage import db

needs_db = pytest.mark.skipif(
    not get_settings().database_path.exists(),
    reason="run `python -m app.data_sources.ingest` first",
)


def make_answer(**overrides) -> AgentAnswer:
    base = {
        "answer": "ok",
        "answer_type": "statistic",
        "hubs": ["denver"],
        "ranking": [],
        "key_figures": [],
        "reasoning": [],
        "assumptions": [],
        "data_sources": [],
        "confidence": "high",
        "confidence_reason": "test",
        "follow_up_suggestions": [],
    }
    return AgentAnswer(**{**base, **overrides})


# ------------------------------------------------------------------------ schemas


def test_tool_schemas_forbid_extra_fields():
    for d in tools.tool_definitions():
        assert d["input_schema"]["type"] == "object"
        assert d["input_schema"].get("additionalProperties") is False, d["name"]


def test_answer_schema_has_no_unsupported_constraints():
    # Structured outputs reject min/max/length constraints, so the schema must not have any.
    text = json.dumps(AgentAnswer.model_json_schema())
    for banned in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"):
        assert banned not in text


# -------------------------------------------------------------------------- tools


@needs_db
def test_execute_tools_happy_path():
    conn = db.connect()
    text, err = tools.execute(conn, "rank_hubs", {"rank_by": "winter", "region": "Midwest"})
    assert not err and json.loads(text)["ranking"][0]["region"] == "Midwest"
    text, err = tools.execute(conn, "get_hub_risk", {"hub": "Dallas"})
    assert not err and json.loads(text)["hub_id"] == "dallas"
    text, err = tools.execute(
        conn, "weather_stat", {"hub": "denver", "stat": "snowfall", "year": 2025}
    )
    assert not err and json.loads(text)["total_days"] == 365


@needs_db
def test_execute_tools_errors_are_returned_not_raised():
    conn = db.connect()
    text, err = tools.execute(conn, "get_hub_risk", {"hub": "Portland"})
    assert err and "not one of our hubs" in json.loads(text)["error"]
    text, err = tools.execute(conn, "rank_hubs", {"rank_by": "volcano"})
    assert err and "invalid input" in json.loads(text)["error"]
    text, err = tools.execute(conn, "compare_hubs", {"hubs": ["miami"]})
    assert err and "between 2 and 6" in json.loads(text)["error"]
    text, err = tools.execute(conn, "no_such_tool", {})
    assert err


# ---------------------------------------------------------------------- grounding


def test_grounding_accepts_numbers_from_tools_and_rounding():
    ans = make_answer(
        key_figures=[
            {"label": "a", "value": 4.7, "unit": "%", "source_tool": "weather_stat"},
            {"label": "b", "value": 47, "unit": "score", "source_tool": "rank_hubs"},
        ]
    )
    outputs = [("weather_stat", '{"percent":4.7}'), ("rank_hubs", '{"score":46.63}')]
    assert check_grounding(ans, outputs) == []


def test_grounding_flags_invented_numbers_and_hubs():
    ans = make_answer(
        hubs=["gotham"],
        key_figures=[{"label": "made up", "value": 99.9, "unit": "%", "source_tool": "x"}],
    )
    warnings = check_grounding(ans, [("weather_stat", '{"percent":4.7}')])
    assert any("gotham" in w for w in warnings)
    assert any("made up" in w for w in warnings)


# ---------------------------------------------------------- agent loop (fake Claude)


class Block(SimpleNamespace):
    def to_dict(self):
        return {k: v for k, v in vars(self).items()}


class FakeClient:
    """Plays back scripted responses and records the requests it received."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs["messages"], default=str)))
        return self.responses.pop(0)


def response(stop_reason, *blocks):
    usage = SimpleNamespace(
        input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0
    )
    return SimpleNamespace(
        stop_reason=stop_reason, content=list(blocks), usage=usage, model="fake-model"
    )


FINAL = make_answer(
    key_figures=[{"label": "snow", "value": 4.7, "unit": "%", "source_tool": "weather_stat"}]
).model_dump_json()


@needs_db
def test_agent_runs_tools_then_returns_validated_answer(tmp_path):
    conn = db.connect()
    client = FakeClient(
        [
            response(
                "tool_use",
                Block(
                    type="tool_use",
                    id="t1",
                    name="weather_stat",
                    input={"hub": "denver", "stat": "snowfall", "year": 2025},
                ),
            ),
            response("end_turn", Block(type="text", text=FINAL)),
        ]
    )
    agent = WeatherRiskAgent(conn=conn, client=client)
    out = agent.chat("snow in denver last year?", session_id="test-session")

    assert out.answer.key_figures[0].value == 4.7
    assert [c.name for c in out.tool_calls] == ["weather_stat"] and out.tool_calls[0].ok
    assert out.grounding_warnings == []
    # The second request carried the tool result back to Claude.
    last_msg = client.requests[1][-1]
    assert last_msg["role"] == "user" and last_msg["content"][0]["type"] == "tool_result"

    # The session was saved, so a follow-up starts from the full history.
    history = db.load_session(conn, "test-session")
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]
    db.delete_session(conn, "test-session")


def test_agent_refusal_raises_and_does_not_save(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    client = FakeClient([response("refusal")])
    agent = WeatherRiskAgent(conn=conn, client=client)
    with pytest.raises(AgentError, match="declined"):
        agent.chat("...", session_id="s1")
    assert db.load_session(conn, "s1") == []


def test_agent_rejects_answer_that_breaks_schema(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    client = FakeClient([response("end_turn", Block(type="text", text='{"answer": "hi"}'))])
    with pytest.raises(AgentError, match="schema"):
        WeatherRiskAgent(conn=conn, client=client).chat("hi")
