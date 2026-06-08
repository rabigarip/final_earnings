"""Batch voice eval — score thesis paragraphs from recent decks.

Usage:
    python scripts/eval_voice.py                          # last 10 decks
    python scripts/eval_voice.py BKMB.OM 2020.SR          # specific tickers
    python scripts/eval_voice.py --references             # score the 3 references

Reads thesis paragraphs from the PPTX files in outputs/ by parsing
the slide-2 XML and grabbing the long text run that is the thesis
paragraph. Outputs a JSON report at data/voice_eval/{date}.json and
a one-line-per-ticker summary to stdout.

Designed to run cheaply enough that it can fire after every deck
generation, or as a daily batch job to track voice drift over time.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.voice_eval import (
    evaluate, evaluate_batch, evaluate_references,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUTS_DIR = ROOT / "outputs"
EVAL_DIR = ROOT / "data" / "voice_eval"


# The slide-2 thesis paragraph is the longest text run on slide 2.
# Extract it by reading the slide XML and picking the longest <a:t>.
_A_T_RE = re.compile(r"<a:t>([^<]+)</a:t>")


def extract_thesis_from_pptx(pptx_path: Path) -> str | None:
    """Read slide2.xml from a .pptx and return the longest text run."""
    try:
        with zipfile.ZipFile(pptx_path) as zf:
            with zf.open("ppt/slides/slide2.xml") as f:
                xml = f.read().decode("utf-8", errors="ignore")
    except (KeyError, zipfile.BadZipFile):
        return None
    runs = _A_T_RE.findall(xml)
    if not runs: return None
    longest = max(runs, key=len)
    # Sanity gate: thesis runs are typically > 200 chars.
    if len(longest) < 100: return None
    # Decode XML entities (apostrophes etc.).
    import html
    return html.unescape(longest)


def latest_pptx_per_ticker(limit: int = 10) -> dict[str, Path]:
    """Return ticker → latest .pptx mapping for the N most recent decks."""
    if not OUTPUTS_DIR.is_dir(): return {}
    files = sorted(OUTPUTS_DIR.glob("*.pptx"),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    by_ticker: dict[str, Path] = {}
    for f in files:
        ticker = f.stem.split("_", 1)[0]
        if ticker not in by_ticker:
            by_ticker[ticker] = f
        if len(by_ticker) >= limit: break
    return by_ticker


def cmd_references():
    """Score the 3 reference examples — calibration mode."""
    refs = evaluate_references()
    for name, r in refs.items():
        print(f"{name:<10}  composite={r.composite_score:.3f}  "
              f"grade={r.grade:<10}  wc={r.word_count:3d}  "
              f"setup={r.setup_label}")
    return 0


def cmd_evaluate(tickers: list[str] | None, limit: int = 10):
    """Score recent decks. If `tickers` is None, take the latest deck
    per ticker (up to `limit`). Otherwise filter to the given list."""
    by_ticker = latest_pptx_per_ticker(limit=limit if not tickers else 500)
    if tickers:
        by_ticker = {t: by_ticker[t] for t in tickers if t in by_ticker}
        missing = [t for t in tickers if t not in by_ticker]
        for m in missing:
            print(f"WARN: no recent deck for {m}", file=sys.stderr)
    if not by_ticker:
        print("No decks to evaluate.", file=sys.stderr)
        return 1

    decks: list[tuple[str, str]] = []
    for ticker, path in by_ticker.items():
        thesis = extract_thesis_from_pptx(path)
        if not thesis:
            print(f"WARN: could not extract thesis from {path.name}", file=sys.stderr)
            continue
        decks.append((ticker, thesis))

    report = evaluate_batch(decks)
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    out_path = EVAL_DIR / f"{date.today().isoformat()}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    agg = report["aggregate"]
    print(f"\nScored {agg['n_decks']} deck(s) — "
          f"avg composite {agg['avg_composite']:.3f}")
    print(f"  matches: {agg['n_matches']}   "
          f"close: {agg['n_close']}   "
          f"diverges: {agg['n_diverges']}\n")
    for ticker, r in report["results"].items():
        grade = r["grade"]
        mark = {"matches": "✓", "close": "≈", "diverges": "✗"}[grade]
        print(f"  {mark}  {ticker:<14}  "
              f"composite={r['composite_score']:.3f}  "
              f"struct={r['structural_score']:.2f}  "
              f"lex={r['lexical_score']:.2f}  "
              f"style={r['style_score']:.2f}  "
              f"({grade})")
    print(f"\nFull report → {out_path}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tickers", nargs="*",
                    help="Optional tickers to filter to. Empty = last N decks.")
    ap.add_argument("--limit", type=int, default=10,
                    help="When tickers omitted, take N most recent decks.")
    ap.add_argument("--references", action="store_true",
                    help="Score the 3 reference examples for calibration.")
    args = ap.parse_args()
    if args.references:
        return cmd_references()
    return cmd_evaluate(args.tickers or None, limit=args.limit)


if __name__ == "__main__":
    sys.exit(main())
