"""Run the evaluation set against the real agent (real Claude calls, real data).

Usage:
    uv run python -m evals.run                     # all cases
    uv run python -m evals.run --only dallas_why_high,off_topic
    uv run python -m evals.run --tag assignment    # cases with a tag
    uv run python -m evals.run --workers 2         # fewer parallel conversations

Output:
    evals/results/<timestamp>/results.jsonl   one row per case, written as each finishes
    evals/results/<timestamp>/summary.md      pass/fail table, per-check rates, latency, cost
"""

from __future__ import annotations

import argparse
import json
import math
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import anthropic
import yaml

from app.agent.agent import MODEL, AgentError, WeatherRiskAgent
from app.config import get_settings
from app.storage import db
from evals.grader import grade

EVAL_DIR = Path(__file__).parent
# Claude Opus 5.5 list prices, USD per million tokens. Cache writes cost 1.25x input and
# cache reads 0.1x input.
PRICE_IN, PRICE_OUT = 4.00, 20.00


# One Claude client shared by all worker threads (it's thread-safe and pools connections).
_CLIENT = anthropic.Anthropic(api_key=get_settings().anthropic_api_key or None)


def cost_usd(usage: dict) -> float:
    return (
        usage.get("input_tokens", 0) * PRICE_IN
        + usage.get("cache_creation_input_tokens", 0) * PRICE_IN * 1.25
        + usage.get("cache_read_input_tokens", 0) * PRICE_IN * 0.1
        + usage.get("output_tokens", 0) * PRICE_OUT
    ) / 1_000_000


def wilson_interval(passed: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% confidence interval for a pass rate. With ~17 cases it's wide, so it's shown."""
    if n == 0:
        return 0.0, 0.0
    p = passed / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def run_case(case: dict) -> dict:
    """Run one conversation and grade its last answer. Each case gets its own DB connection."""
    conn = db.connect()
    agent = WeatherRiskAgent(conn=conn, client=_CLIENT)
    session = f"eval-{case['id']}-{uuid.uuid4().hex[:6]}"
    t0 = time.monotonic()
    usage_total: dict[str, int] = {}
    row = {"id": case["id"], "tags": case.get("tags", []), "turns": case["turns"]}
    try:
        resp = None
        for turn in case["turns"]:
            resp = agent.chat(turn, session)
            for k, v in resp.usage.items():
                usage_total[k] = usage_total.get(k, 0) + v
        checks = grade(conn, resp, case["checks"])
        row.update(
            status="ok",
            passed=all(ok for ok, _ in checks.values()),
            checks={k: {"passed": ok, "detail": d} for k, (ok, d) in checks.items()},
            answer=resp.answer.model_dump(),
            tools=[c.name for c in resp.tool_calls],
            grounding_warnings=resp.grounding_warnings,
        )
    except AgentError as exc:
        # An API/agent failure is an ERROR, not a graded failure: it says nothing about quality.
        row.update(status="error", passed=False, error=str(exc), checks={})
    finally:
        db.delete_session(conn, session)  # don't leave eval chats in the app's database
        conn.close()
    row["latency_s"] = round(time.monotonic() - t0, 1)
    row["usage"] = usage_total
    row["cost_usd"] = round(cost_usd(usage_total), 4)
    return row


def summarize(rows: list[dict], out_dir: Path, wall_s: float) -> str:
    graded = [r for r in rows if r["status"] == "ok"]
    errors = [r for r in rows if r["status"] == "error"]
    passed = sum(r["passed"] for r in graded)
    lo, hi = wilson_interval(passed, len(graded))
    total_cost = sum(r["cost_usd"] for r in rows)
    lat = sorted(r["latency_s"] for r in graded) or [0]

    lines = [
        f"# Eval run {out_dir.name}",
        "",
        f"- Model: `{MODEL}`",
        f"- **Passed {passed}/{len(graded)} cases ({passed / max(len(graded), 1):.0%})**, "
        f"95% CI {lo:.0%}-{hi:.0%}" + (f" · {len(errors)} errored (not graded)" if errors else ""),
        f"- Latency per case: median {lat[len(lat) // 2]}s, max {lat[-1]}s · wall clock {wall_s:.0f}s",
        f"- Cost: ${total_cost:.2f} total, ${total_cost / max(len(rows), 1):.3f} per case",
        "",
        "| Case | Tags | Result | Failed checks | Tools | Latency | Cost |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in sorted(rows, key=lambda r: r["id"]):
        if r["status"] == "error":
            result, failed = "ERROR", r["error"]
        else:
            result = "pass" if r["passed"] else "**FAIL**"
            failed = "; ".join(
                f"{k}: {v['detail']}" for k, v in r["checks"].items() if not v["passed"]
            )
        lines.append(
            f"| {r['id']} | {', '.join(r['tags'])} | {result} | {failed or '-'} | "
            f"{', '.join(r.get('tools', [])) or '-'} | {r['latency_s']}s | ${r['cost_usd']:.3f} |"
        )

    # Pass rate per check type: shows WHAT tends to fail (tool choice, numbers, scope...).
    per_check: dict[str, list[bool]] = {}
    for r in graded:
        for k, v in r["checks"].items():
            per_check.setdefault(k.split("[")[0], []).append(v["passed"])
    lines += ["", "| Check | Pass rate |", "|---|---|"]
    lines += [f"| {k} | {sum(v)}/{len(v)} |" for k, v in sorted(per_check.items())]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--only", help="comma-separated case ids")
    parser.add_argument("--tag", help="only cases with this tag")
    parser.add_argument("--workers", type=int, default=4, help="parallel conversations")
    args = parser.parse_args()

    cases = yaml.safe_load((EVAL_DIR / "cases.yaml").read_text())["cases"]
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    if args.tag:
        cases = [c for c in cases if args.tag in c.get("tags", [])]
    if not cases:
        raise SystemExit("no cases selected")

    out_dir = EVAL_DIR / "results" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True)
    print(f"Running {len(cases)} cases on {MODEL} with {args.workers} workers -> {out_dir}")

    rows, t0 = [], time.monotonic()
    with (
        ThreadPoolExecutor(max_workers=args.workers) as pool,
        open(out_dir / "results.jsonl", "w") as f,
    ):
        futures = {pool.submit(run_case, c): c["id"] for c in cases}
        for fut in as_completed(futures):
            row = fut.result()
            rows.append(row)
            f.write(json.dumps(row) + "\n")  # written as each case finishes: a crash keeps them
            f.flush()
            mark = "ERROR" if row["status"] == "error" else ("pass" if row["passed"] else "FAIL")
            print(f"  {mark:<5} {row['id']:<30} {row['latency_s']:>5}s  ${row['cost_usd']:.3f}")

    summary = summarize(rows, out_dir, time.monotonic() - t0)
    (out_dir / "summary.md").write_text(summary)
    print("\n" + summary)


if __name__ == "__main__":
    main()
