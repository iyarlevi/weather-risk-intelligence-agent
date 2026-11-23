"""Pure functions that turn daily weather rows into the metrics defined in scoring.yaml.

No I/O here, so they are easy to unit test and reuse (scoring, ad-hoc stats).
"""

from __future__ import annotations

import operator
from collections.abc import Callable

OPS: dict[str, Callable[[float, float], bool]] = {
    ">=": operator.ge,
    ">": operator.gt,
    "<=": operator.le,
    "<": operator.lt,
}


def count_days(rows: list[dict], column: str, op: str, threshold: float) -> tuple[int, int]:
    """(days meeting the condition, days with a value). Missing values are not counted."""
    cmp = OPS[op]
    values = [r[column] for r in rows if r.get(column) is not None]
    return sum(1 for v in values if cmp(v, threshold)), len(values)


def compute_metric(rows: list[dict], spec: dict) -> float | None:
    """Compute one metric from its scoring.yaml spec. None means there is no data."""
    column = spec["column"]
    if spec["agg"] == "max":
        values = [r[column] for r in rows if r.get(column) is not None]
        return max(values) if values else None
    if spec["agg"] == "days_per_year":
        hits, n = count_days(rows, column, spec["op"], spec["threshold"])
        if n == 0:
            return None
        # Normalise by the days actually observed, so a partial year doesn't bias the rate.
        return hits / n * 365.25
    raise ValueError(f"unknown agg: {spec['agg']}")


def scale(value: float | None, lo: float, hi: float) -> float:
    """Map a raw value onto 0..100 using a fixed [lo, hi] range, clamped at both ends."""
    if value is None:
        return 0.0
    if hi == lo:
        return 100.0 if value >= hi else 0.0
    return max(0.0, min(1.0, (value - lo) / (hi - lo))) * 100.0
