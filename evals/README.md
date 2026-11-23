# Evaluation

A small, repeatable check that the agent picks the right tools, quotes the right numbers,
handles follow-ups, and stays honest when it doesn't have data.

```bash
uv run python -m evals.run                    # all 17 cases (~75 s, ~$1 on claude-opus-5-5)
uv run python -m evals.run --tag assignment   # just the 4 example questions from the brief
uv run python -m evals.run --only off_topic,dallas_why_high
```

Results go to `evals/results/<timestamp>/` (ignored by git):
- `results.jsonl`: one row per case, with the full answer, tools, checks, latency and cost.
- `summary.md`: the pass/fail table, the pass rate for each check, and the totals.

The latest baseline is in [baseline.md](baseline.md).

## What's in the set ([cases.yaml](cases.yaml))

| Group | Cases | What it tests |
|---|---|---|
| Assignment examples | 4 | The four questions from the brief |
| Rankings | 3 | Overall top 3, worst heat hub, Northeast hurricane leader |
| Statistics | 2 | Day counts and percentages for a year or the whole window |
| Follow-ups | 2 | "How does **it** compare…", "Why is **the top one**…" |
| Methodology / live | 2 | Explaining the weights; live NOAA alerts |
| Negatives (honesty, scope) | 4 | A hub that doesn't exist, an off-topic question, a year with no data, a request for dollar losses |

## How it's graded

- **Code checks only, with no LLM judge.** The answers have objective parts (which tool, which
  hubs, which numbers, which answer type), so code can grade them exactly, for free, and
  reproducibly.
- **Expected values come from the scoring engine at grading time** (e.g. "the Midwest winter top
  3", "Denver's 2025 snowfall %"), so the eval doesn't go stale when data or weights change.
- **Every case also requires a clean grounding check**: no number in the answer that a tool didn't
  return.
- **API failures count as `error`, not `fail`**, because an outage says nothing about answer quality.
- **The pass rate comes with a 95% Wilson interval.** With 17 cases one flip moves the score by
  ~6 points, so small differences between runs are noise.

The harness checks itself in `tests/test_evals.py` (offline, free):
- an "oracle" answer built from the engine passes all 17 cases;
- an empty answer fails all 17;
- an answer with an invented number fails.

## Limits

- 17 cases is small: good for catching regressions, too small for fine-grained comparisons.
- Not graded: writing quality and how clear the explanation is. That would need a human or an
  LLM judge, and the objective checks were the priority.
- The live-alerts case depends on current weather, so only its tool use and hub are checked.
