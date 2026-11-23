"""Sanity checks for the eval harness itself (offline, free).

- Oracle: a hand-built 'perfect' answer for every case must pass every check. This proves each
  case is satisfiable and the grader computes expectations correctly.
- Null: an empty answer with no tools must fail every case. This proves the grader can't be
  passed by doing nothing.
"""

import pytest
import yaml

from app.agent.agent import AgentResponse, ToolCall
from app.config import get_settings
from app.storage import db
from evals.grader import _engine_top, _expected_value, grade
from evals.run import EVAL_DIR, cost_usd, wilson_interval
from tests.test_agent import make_answer

pytestmark = pytest.mark.skipif(
    not get_settings().database_path.exists(),
    reason="run `python -m app.data_sources.ingest` first",
)

CASES = yaml.safe_load((EVAL_DIR / "cases.yaml").read_text())["cases"]


def response(answer, tools=(), warnings=()):
    return AgentResponse(
        session_id="t",
        answer=answer,
        tool_calls=[ToolCall(name=t, input={}, ok=True, duration_ms=1) for t in tools],
        grounding_warnings=list(warnings),
        model="oracle",
        usage={},
        latency_ms=1,
    )


def oracle_for(conn, checks: dict) -> AgentResponse:
    """Build the answer a perfect agent would give, using the same engine the grader uses."""
    hubs, ranking, figures = list(checks.get("hubs_include", [])), [], []
    spec = checks.get("ranking_top")
    if spec:
        for i, hub in enumerate(
            _engine_top(conn, spec["rank_by"], spec.get("region"), spec["n"]), 1
        ):
            ranking.append({"rank": i, "hub_id": hub, "name": hub, "score": 1.0, "note": ""})
    if "top_hub_named" in checks:
        s = checks["top_hub_named"]
        hubs.append(_engine_top(conn, s["rank_by"], s.get("region"), 1)[0])
    for fig in checks.get("figure", []):
        value = _expected_value(conn, fig)
        figures.append({"label": "x", "value": value, "unit": "n", "source_tool": "t"})
    answer = make_answer(
        answer=" ".join(checks.get("text_includes_any", ["ok"])[:1]),
        answer_type=checks.get("answer_type_in", ["statistic"])[0],
        hubs=hubs,
        ranking=ranking,
        key_figures=figures,
    )
    return response(answer, tools=checks.get("tools_any", [])[:1])


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_oracle_passes_every_case(case):
    conn = db.connect()
    results = grade(conn, oracle_for(conn, case["checks"]), case["checks"])
    failed = {k: d for k, (ok, d) in results.items() if not ok}
    assert not failed, failed


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_null_answer_fails_every_case(case):
    conn = db.connect()
    null = response(make_answer(answer="", answer_type="clarification", hubs=[]))
    results = grade(conn, null, case["checks"])
    assert not all(ok for ok, _ in results.values())


def test_grounding_warning_fails_a_case():
    conn = db.connect()
    case = CASES[0]
    good = oracle_for(conn, case["checks"])
    bad = response(good.answer, [t.name for t in good.tool_calls], warnings=["invented"])
    assert not all(ok for ok, _ in grade(conn, bad, case["checks"]).values())


def test_case_ids_unique_and_checks_known():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids))
    assert len(CASES) >= 15


def test_cost_and_interval_helpers():
    assert cost_usd({"input_tokens": 1_000_000}) == pytest.approx(4.0)
    assert cost_usd({"cache_read_input_tokens": 1_000_000}) == pytest.approx(0.4)
    lo, hi = wilson_interval(15, 17)
    assert 0.6 < lo < 0.88 < hi <= 1.0
