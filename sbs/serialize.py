"""Tiny JSON helpers shared across layers (data, backtest, db, reporting)."""
from __future__ import annotations

import math
from typing import Any


def json_safe(obj: Any) -> Any:
    """Recursively replace non-finite floats (inf/-inf/NaN) with ``None``.

    ``json.dumps`` otherwise emits the non-standard tokens ``Infinity``/``NaN``,
    which strict JSON parsers (and PostgreSQL ``jsonb`` columns) reject. The
    canonical case is ``profit_factor`` = ``inf`` when a run has no losing trades.
    The in-memory metric keeps ``inf`` (so the walk-forward gate's ``PF > 1`` check
    still passes); only the serialized form is sanitized.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    return obj
