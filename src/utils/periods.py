"""Period-label helpers shared between pipeline, CLI, and services.

Why this module exists:
  Lexical sort of `period_label` ("Q1 2026" vs "2025 Q4") doesn't yield a
  chronological order. Prefer `period_end` when present; otherwise parse the
  label. Keep the logic in one place to avoid drift.
"""

from __future__ import annotations

import re
from typing import Iterable, TypeVar

T = TypeVar("T")


def _sort_key(p):
    end = getattr(p, "period_end", None)
    if end is not None:
        try:
            return (0, str(end))
        except Exception:
            pass
    label = (getattr(p, "period_label", "") or "").strip()
    m = re.search(r"Q([1-4]).*?(\d{4})|(\d{4}).*?Q([1-4])", label)
    if m:
        if m.group(1):
            q, y = int(m.group(1)), int(m.group(2))
        else:
            q, y = int(m.group(4)), int(m.group(3))
        return (1, y, q)
    return (2, label)


def sort_periods_ascending(periods: Iterable[T]) -> list[T]:
    """Return periods sorted oldest → newest (chronological)."""
    return sorted(periods, key=_sort_key)


def latest_period(periods: Iterable[T]) -> T:
    """Return the most recent period. Raises IndexError on empty input."""
    return sort_periods_ascending(periods)[-1]
