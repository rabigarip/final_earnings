"""
StepResult — every service function returns one of these.

This is the governance backbone. No service is allowed to return raw data
without wrapping it here. The pipeline runner collects these, prints them,
and stores them in the run log for auditability.
"""

from __future__ import annotations
from enum import Enum
from typing import Any
import time
from pydantic import BaseModel, Field


class Status(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED  = "failed"
    SKIPPED = "skipped"


class StepResult(BaseModel):
    step_name:       str
    status:          Status
    source:          str            = ""
    fallback_used:   bool           = False
    message:         str            = ""
    error_detail:    str | None     = None
    record_count:    int | None     = None
    elapsed_seconds: float          = 0.0
    data:            Any            = None

    model_config = {"arbitrary_types_allowed": True}

    # ── Terminal formatting ────────────────────────────────────

    _ICONS: dict[Status, str] = {
        Status.SUCCESS: "✓ SUCCESS",
        Status.PARTIAL: "⚠ PARTIAL",
        Status.FAILED:  "✗ FAILED",
        Status.SKIPPED: "⊘ SKIPPED",
    }

    def print_box(self) -> None:
        bar = "═" * 66
        icon = self._ICONS.get(self.status, self.status.value)
        lines = [
            "",
            bar,
            f"  STEP   {self.step_name}",
            bar,
            f"  Status:   {icon}",
        ]
        if self.source:
            lines.append(f"  Source:   {self.source}")
        if self.fallback_used:
            lines.append(f"  Fallback: YES")
        lines.append(f"  Message:  {self.message}")
        if self.error_detail:
            lines.append(f"  Error:    {self.error_detail}")
        if self.record_count is not None:
            lines.append(f"  Records:  {self.record_count}")
        lines.append(f"  Elapsed:  {self.elapsed_seconds:.2f}s")
        lines.append(bar)
        print("\n".join(lines))

    def to_log_dict(self) -> dict:
        """Serializable dict for DB / JSON log (excludes bulky data)."""
        return {
            "step_name":       self.step_name,
            "status":          self.status.value,
            "source":          self.source,
            "fallback_used":   self.fallback_used,
            "message":         self.message,
            "error_detail":    self.error_detail,
            "record_count":    self.record_count,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


class StepTimer:
    """Context manager that measures wall-clock time for a step."""
    def __init__(self):
        self._start = 0.0
        self.elapsed = 0.0
    def __enter__(self):
        self._start = time.perf_counter()
        return self
    def __exit__(self, *_):
        self.elapsed = time.perf_counter() - self._start
