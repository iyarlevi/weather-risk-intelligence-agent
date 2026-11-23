"""Deterministic grader: checks an AgentResponse against a case's `checks`.

No LLM judge. Every check is code, and expected values come from the scoring engine at
grading time. A check that can't be evaluated raises, so a bug never shows up as a fake pass.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from app.agent.agent import AgentResponse
from app.agent.tools import SCORES
from app.data_sources.ingest import history_window
from app.scoring.engine import rank
from app.scoring.stats import weather_stat


def _close(a: float, b: float) -> bool:
    # The agent may round (70.3 -> 70) or quote more decimals than the engine: allow that.
    return abs(a - b) <= max(0.051, abs(b) * 0.005) or round(b) == a or round(a, 1) == round(b, 1)


def _expected_value(conn: sqlite3.Connection, spec: dict) -> float:
    kind = spec["kind"]
    scores = SCORES.get(conn)
    if kind == "hazard_score":
        return scores[spec["hub"]].hazards[spec["hazard"]].score
    if kind == "composite_score":
        return scores[spec["hub"]].composite_score
    if kind == "weather_stat":
        year = spec.get("year")
        if year == "last":
            year = date.today().year - 1
        if year is None:  # the full history window
            start, end = history_window()
            st = weather_stat(conn, spec["hub"], spec["stat"], start=start, end=end)
        else:
            st = weather_stat(conn, spec["hub"], spec["stat"], year=year)
        return float(getattr(st, spec["field"]))
    raise ValueError(f"unknown figure kind '{kind}'")


def _engine_top(conn: sqlite3.Connection, rank_by: str, region: str | None, n: int) -> list[str]:
    hazard = None if rank_by == "composite" else rank_by
    return [e.hub_id for e in rank(SCORES.get(conn), hazard=hazard, region=region, limit=n)]


def grade(
    conn: sqlite3.Connection, resp: AgentResponse, checks: dict
) -> dict[str, tuple[bool, str]]:
    """Return {check name: (passed, detail)}. The case passes only if every check passes."""
    a = resp.answer
    tools_used = [c.name for c in resp.tool_calls]
    answer_hubs = set(a.hubs) | {r.hub_id for r in a.ranking}
    numbers = [f.value for f in a.key_figures] + [r.score for r in a.ranking]
    text = a.answer.lower()
    out: dict[str, tuple[bool, str]] = {}

    for name, spec in checks.items():
        if name == "tools_any":
            ok = any(t in tools_used for t in spec)
            out[name] = (ok, f"used {tools_used or 'no tools'}, wanted one of {spec}")
        elif name == "answer_type_in":
            out[name] = (a.answer_type in spec, f"got '{a.answer_type}', wanted {spec}")
        elif name == "hubs_include":
            missing = [h for h in spec if h not in a.hubs]
            out[name] = (not missing, f"missing {missing}" if missing else "ok")
        elif name == "hubs_exclude":
            bad = [h for h in spec if h in answer_hubs]
            out[name] = (not bad, f"should not contain {bad}" if bad else "ok")
        elif name == "ranking_top":
            want = _engine_top(conn, spec["rank_by"], spec.get("region"), spec["n"])
            got = [r.hub_id for r in a.ranking][: spec["n"]]
            out[name] = (got == want, f"got {got}, engine says {want}")
        elif name == "top_hub_named":
            want = _engine_top(conn, spec["rank_by"], spec.get("region"), 1)[0]
            out[name] = (want in a.hubs, f"engine #1 is '{want}', answer hubs {a.hubs}")
        elif name == "figure":
            for i, fig in enumerate(spec):
                want = _expected_value(conn, fig)
                ok = any(_close(v, want) for v in numbers)
                label = f"figure[{i}] {fig['kind']}:{fig.get('hub')}"
                out[label] = (ok, f"engine value {want}, answer numbers {numbers[:10]}")
        elif name == "text_includes_any":
            ok = any(s.lower() in text for s in spec)
            out[name] = (ok, "ok" if ok else f"none of {spec} in answer")
        elif name == "no_figures_with_unit":
            bad = [f.label for f in a.key_figures if any(u.lower() in f.unit.lower() for u in spec)]
            out[name] = (not bad, f"figures with forbidden unit: {bad}" if bad else "ok")
        elif name == "no_figure_label_contains":
            # e.g. ["2015"]: the agent may offer another year, but never a number presented as 2015.
            bad = [
                f.label for f in a.key_figures if any(t.lower() in f.label.lower() for t in spec)
            ]
            out[name] = (not bad, f"figures labeled with {spec}: {bad}" if bad else "ok")
        elif name == "ranking_empty":
            out[name] = (not a.ranking, "ok" if not a.ranking else "unexpected ranking")
        else:
            raise ValueError(f"unknown check '{name}'")

    # Always on: the agent's numbers must all come from tool results.
    out["grounded"] = (not resp.grounding_warnings, "; ".join(resp.grounding_warnings) or "ok")
    return out
