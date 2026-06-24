"""Memory-safety regression tests.

Two layers protect the 512MB Render instance from OOM-restarting:
  1. A build semaphore (one deck build at a time — concurrent builds would
     stack their ~280-400MB peaks in one process and blow past 512MB).
  2. A pre-build RSS guard that refuses to START a build when the process is
     already near the limit, surfaced to the API as HTTP 503.

These tests pin that behaviour so a future change can't silently remove it.
The deterministic tests below need no network. The opt-in RSS-plateau test
(RUN_RSS_TEST=1) actually builds decks and asserts steady-state RSS stays
under budget — it hits the network, so it's skipped in normal CI.
"""

from __future__ import annotations

import os

import pytest

import src.pipeline as pipeline
from src.pipeline import BuildRejectedError


def test_build_semaphore_serializes_to_one():
    """Concurrency must default to 1 — the whole point is that two builds
    never run at once on a 512MB box."""
    assert pipeline._MAX_CONCURRENT_BUILDS == 1
    assert pipeline._BUILD_SEMAPHORE._initial_value == 1


def test_guard_rejects_build_when_rss_over_limit(monkeypatch):
    """When RSS is over the limit, run_preview must refuse BEFORE doing any
    work (no network, no semaphore wait) by raising BuildRejectedError."""
    monkeypatch.setattr(pipeline, "_RSS_LIMIT_MB", 400.0)
    monkeypatch.setattr(pipeline, "_process_rss_mb", lambda: 999.0)
    with pytest.raises(BuildRejectedError):
        pipeline.run_preview("AAPL", skip_llm=True)


def test_guard_is_noop_when_disabled(monkeypatch):
    """EARNINGS_RSS_LIMIT_MB=0 disables the guard even at high RSS."""
    monkeypatch.setattr(pipeline, "_RSS_LIMIT_MB", 0.0)
    monkeypatch.setattr(pipeline, "_process_rss_mb", lambda: 999.0)
    assert pipeline._under_memory_pressure() is False


def test_guard_is_noop_when_rss_unreadable(monkeypatch):
    """Off-Linux (no /proc/self/statm) the guard must not fire — it returns
    None and the build proceeds normally on local dev machines."""
    monkeypatch.setattr(pipeline, "_RSS_LIMIT_MB", 400.0)
    monkeypatch.setattr(pipeline, "_process_rss_mb", lambda: None)
    assert pipeline._under_memory_pressure() is False


def test_guard_allows_build_under_limit(monkeypatch):
    """Below the limit the guard is transparent."""
    monkeypatch.setattr(pipeline, "_RSS_LIMIT_MB", 460.0)
    monkeypatch.setattr(pipeline, "_process_rss_mb", lambda: 280.0)
    assert pipeline._under_memory_pressure() is False


@pytest.mark.skipif(os.environ.get("RUN_RSS_TEST") != "1",
                    reason="hits the network; run explicitly with RUN_RSS_TEST=1")
def test_rss_plateaus_across_repeated_builds():
    """Build several decks back-to-back and assert RSS plateaus under budget
    instead of climbing — catches a future change that re-introduces a leak."""
    try:
        import psutil  # type: ignore
        proc = psutil.Process(os.getpid())
        def rss_mb():
            return proc.memory_info().rss / (1024 * 1024)
    except Exception:
        rss = pipeline._process_rss_mb()
        if rss is None:
            pytest.skip("no RSS source available on this platform")
        rss_mb = pipeline._process_rss_mb  # type: ignore

    budget_mb = float(os.environ.get("RSS_BUDGET_MB", "420"))
    tickers = ["2020.SR", "000063.SZ", "2020.SR", "000063.SZ"]
    samples = []
    for tk in tickers:
        pipeline.run_preview(tk, skip_llm=True)
        samples.append(rss_mb())
    # Steady state = the last two builds; both must be under budget, and the
    # series must not be climbing meaningfully (last ≤ first warmed sample + 10%).
    assert samples[-1] < budget_mb, f"RSS {samples[-1]:.0f}MB exceeded budget {budget_mb:.0f}MB"
    assert samples[-1] <= samples[1] * 1.10, f"RSS climbing: {samples}"
