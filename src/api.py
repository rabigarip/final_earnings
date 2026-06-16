"""
FastAPI web app for Render + frontend.

Endpoints:
  GET  /health              — Health check (Render)
  GET  /api/companies       — List companies (ticker, company_name, exchange, country)
  GET  /api/tickers/search  — Search companies by ticker or name (?q=)
  GET  /api/reports         — List pipeline runs (for earnings-preview frontend)
  POST /api/reports         — Create run (run preview), returns report row + payload
  GET  /api/reports/:id     — Get one run (steps, no payload)
  GET  /api/reports/:id/download — Download preview .pptx file
  POST /api/reports/:id/rerun — Rerun preview for that ticker
  POST /api/preview         — Run earnings preview; returns step results + report payload
"""

from __future__ import annotations
import os
import logging
from pathlib import Path
from contextlib import asynccontextmanager

import json

# Module logger. The calendar auto-refresh path (get_calendar /
# _kickoff_auto_refresh) logs via `log`; without this it raised NameError
# and 500'd whenever the calendar DB was cold/sparse (i.e. every fresh
# Render deploy, since the DB lives in ephemeral /tmp).
log = logging.getLogger(__name__)
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# When frontend is built into static/, we serve it at / (one site on Render)
ROOT_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT_DIR / "static"
STATIC_INDEX = STATIC_DIR / "index.html"
SERVE_FRONTEND = STATIC_INDEX.exists()

# Load env before importing pipeline (needs GEMINI_API_KEY etc.)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _ensure_db() -> None:
    """Create DB if missing, run migrations, then always merge company_master.json (Render + local).

    Without re-seed, production SQLite keeps stale rows when git adds marketscreener_id slugs.
    seed_companies uses ON CONFLICT DO UPDATE — safe on every process start (~500 upserts).
    """
    from src.storage.db import init_db, seed_companies, ensure_migrations, _db_path
    if not _db_path().exists():
        init_db()
    else:
        ensure_migrations()
    seed_companies()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_db()
    yield


app = FastAPI(
    title="Earnings Research API",
    description="Backend for earnings preview pipeline; use with a frontend.",
    lifespan=lifespan,
)

# Allow frontend from any origin when developing; restrict in production if needed
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ─── Request/response models ─────────────────────────────────────────────

class PreviewRequest(BaseModel):
    ticker: str = Field(..., description="Yahoo-format ticker, e.g. 2010.SR")
    skip_llm: bool = Field(True, description="Skip Gemini news summarization (faster, no API key)")


class PreviewResponse(BaseModel):
    run_id: str
    overall: str  # success | partial
    steps: list[dict]
    payload: dict | None = None  # Full report payload when build_report_payload succeeded


class BatchPreviewRequest(BaseModel):
    tickers: list[str] = Field(..., description="List of Yahoo-format tickers (e.g. ['2010.SR','1120.SR'])")
    skip_llm: bool = Field(True, description="Skip Gemini summarization for faster batch runs")


# ─── Routes ───────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    try:
        from src.config import cfg
        v = cfg().get("general", {}).get("version", "0.1.0")
        return {"status": "ok", "version": v}
    except Exception:
        return {"status": "ok"}


@app.get("/api/diag/investing/{ticker}")
def diag_investing(ticker: str):
    """Diagnostic: try every Investing.com call we make for a ticker and
    report which succeed and which fail with what error. Used to verify
    whether Investing.com is reachable from this deploy's egress IP."""
    out: dict = {"ticker": ticker, "investing": {}}
    # 1. Equity page (Cloudflare challenge bypass test)
    try:
        from src.providers.probe_investing import (
            _slug, _fetch_equity_page, fetch_historical_prices,
            InvestingProvider,
        )
        slug = _slug(ticker.upper())
        out["slug"] = slug
        if not slug:
            out["investing"]["error"] = "No curated slug — add to _SLUGS map"
            return out
        # equity page
        state = _fetch_equity_page(slug)
        out["investing"]["equity_page"] = "ok" if state else "FAILED — Cloudflare or HTTP error"
        if state:
            eq = state.get("equityStore") or {}
            instr = eq.get("instrument") or {} if isinstance(eq, dict) else {}
            price = (instr.get("price") or {}).get("last") if instr else None
            out["investing"]["last_price"] = price
            out["investing"]["instrument_id"] = eq.get("instrumentId") if isinstance(eq, dict) else None
        # historical
        hp = fetch_historical_prices(ticker.upper())
        out["investing"]["historical"] = (
            f"ok ({len(hp['close_series'])} bars)" if hp and hp.get("close_series")
            else "FAILED — empty"
        )
        # consensus + earnings
        p = InvestingProvider()
        for method_name in ("_fetch_target_price", "_fetch_rating_split",
                            "_fetch_valuation_forward"):
            try:
                v, _, _, _ = getattr(p, method_name)(ticker.upper())
                out["investing"][method_name.lstrip("_")] = "ok"
            except Exception as e:
                out["investing"][method_name.lstrip("_")] = f"FAILED — {type(e).__name__}: {str(e)[:120]}"
    except Exception as e:
        out["investing"]["fatal"] = f"{type(e).__name__}: {str(e)[:200]}"
    return out


class LoginRequest(BaseModel):
    accessCode: str = ""


@app.post("/api/auth/login")
def auth_login(req: LoginRequest):
    """Stub: accept any access code so the frontend login works (one-site mode). Replace with real auth if needed."""
    return {"token": "ok", "message": "ok"}


@app.get("/")
def root():
    if SERVE_FRONTEND:
        return FileResponse(STATIC_INDEX, media_type="text/html")
    return {"service": "earnings-research-api", "docs": "/docs"}


@app.get("/api/companies")
def list_companies():
    from src.storage.db import list_companies as _list
    return _list()


@app.get("/api/tickers/search")
def search_tickers(q: str = ""):
    """Search companies by ticker or company name. Returns { results: [{ ticker, company, country }] }."""
    from src.storage.db import list_companies as _list
    companies = _list()
    query = (q or "").strip().lower()
    if not query:
        return {"results": [{"ticker": c["ticker"], "company": c["company_name"], "country": c["country"]} for c in companies[:100]]}
    out = []
    for c in companies:
        if query in (c.get("ticker") or "").lower() or query in (c.get("company_name") or "").lower():
            out.append({"ticker": c["ticker"], "company": c["company_name"], "country": c["country"]})
    return {"results": out[:50]}


def _run_to_report_row(run: dict) -> dict:
    """Map DB run to frontend report row: id, ticker, company, country, status, created, warnings."""
    status = (run.get("overall_status") or "").lower()
    if status in ("success", "partial"):
        frontend_status = "COMPLETED"
    elif status == "failed":
        frontend_status = "FAILED"
    else:
        frontend_status = "COMPLETED" if status else "FAILED"
    return {
        "id": run["run_id"],
        "ticker": run["ticker"],
        "company": run.get("company_name") or run["ticker"],
        "country": run.get("country") or "",
        "status": frontend_status,
        "created": run.get("started_at") or "",
        "warnings": run.get("warnings", 0),
    }


# ────────────────────── v2 dashboard endpoints ──────────────────
# Calendar-driven, on-demand workflow. Backed by data/tickers.json
# (the 500-ticker universe registry) and data/calendar/upcoming.json
# (the daily-refreshed earnings horizon). No persistent store; both
# files are git-tracked artifacts.

@app.get("/api/v2/upcoming")
def v2_upcoming(horizon_days: int = 14, family: str | None = None,
                  country: str | None = None):
    """Upcoming earnings in the next `horizon_days`, from the LIVE calendar
    DB (reporting_calendar) — the same source as /api/calendar.

    Previously this read a static data/calendar/upcoming.json that went
    stale (and was empty on Render), so the dashboard always showed
    "No earnings in the next 14 days" even when the calendar had events.
    Now it queries the DB so the dashboard and calendar never disagree.

    Optional filters:
      family   — comma-separated template families (bank,energy,tech,...)
      country  — comma-separated ISO-2 codes (SA,OM,AE,IN,...)

    Each row carries a `ticker` the analyst can click to generate a deck.
    """
    from datetime import datetime as _dt_l, timedelta as _td_l
    from src.storage.db import list_calendar_events
    from src.services.ticker_registry import _registry_index

    today = _dt_l.utcnow().date()
    horizon_end = today + _td_l(days=max(1, horizon_days))

    # Gauge DB health over a wider window (same as /api/calendar) — so a
    # legitimately quiet horizon doesn't retrigger a refresh on every load,
    # and a cold DB (fresh Render deploy → empty /tmp) self-heals: kick off
    # the same background backfill the calendar uses, then return what's there.
    broad = list_calendar_events(
        start=(today - _td_l(days=14)).isoformat(),
        end=(today + _td_l(days=46)).isoformat(),
        countries=None, confirmed_only=False,
    )
    try:
        if len(broad) < 20:
            with _calendar_jobs_lock:
                any_running = any(
                    j.get("status") in ("queued", "running")
                    for j in _calendar_jobs.values()
                )
            if not any_running:
                _kickoff_auto_refresh()
    except Exception as exc:
        log.warning("upcoming auto-refresh trigger failed: %s", exc)

    # Filter the broad set to the upcoming horizon [today, horizon_end].
    events = []
    for ev in broad:
        try:
            d = _dt_l.strptime(ev["event_date"], "%Y-%m-%d").date()
        except Exception:
            continue
        if today <= d <= horizon_end:
            events.append(ev)

    reg = _registry_index()
    # Existing decks on disk → recent_deck_exists flag.
    existing: set[str] = set()
    try:
        from src.config import report_output_dir
        out_dir = report_output_dir()
        if out_dir:
            existing = {p.stem.split("_", 1)[0] for p in out_dir.glob("*.pptx")}
    except Exception:
        pass

    fams = {f.strip().lower() for f in family.split(",") if f.strip()} if family else None
    ccs = {c.strip().upper() for c in country.split(",") if c.strip()} if country else None

    # One row per ticker — keep the earliest upcoming event. Enrich with
    # registry fields (template_family / exchange_country) the dashboard renders.
    by_ticker: dict[str, dict] = {}
    for ev in events:
        tk = ev.get("ticker")
        if not tk:
            continue
        r = reg.get(tk) or {}
        fam = (r.get("template_family") or "other").lower()
        cc = (r.get("exchange_country") or "").upper()
        if fams and fam not in fams:
            continue
        if ccs and cc not in ccs:
            continue
        try:
            ed = _dt_l.strptime(ev["event_date"], "%Y-%m-%d").date()
            days_until = (ed - today).days
        except Exception:
            ed = None
            days_until = None
        row = {
            "ticker": tk,
            "company_name": ev.get("company_name") or r.get("company_name") or tk,
            "earnings_date": ev.get("event_date"),
            "days_until": days_until,
            "next_quarter_label": ev.get("period_label") or "",
            "template_family": fam,
            "exchange_country": cc,
            "confirmed": bool(ev.get("confirmed")),
            "recent_deck_exists": tk in existing,
        }
        prev = by_ticker.get(tk)
        if prev is None or (
            days_until is not None
            and (prev["days_until"] is None or days_until < prev["days_until"])
        ):
            by_ticker[tk] = row

    rows = sorted(
        by_ticker.values(),
        key=lambda x: (x["days_until"] is None, x["days_until"] if x["days_until"] is not None else 0),
    )
    return {
        "generated_at": _dt_l.utcnow().isoformat() + "Z",
        "horizon_days": horizon_days,
        "tickers": rows,
    }


@app.get("/api/v2/universe")
def v2_universe(family: str | None = None, country: str | None = None,
                  canonical_only: int = 1, limit: int = 100):
    """List tickers from the 500-ticker universe registry.

    Defaults to canonical-only (hides cross-listing siblings) and the
    top 100 by USD market cap. Use this for the "browse" view in the
    dashboard, or for picking a ticker that isn't in upcoming.json.
    """
    from src.services.ticker_registry import _registry_index
    recs = list(_registry_index().values())
    if canonical_only:
        recs = [r for r in recs if r.get("is_canonical", True)]
    if family:
        fams = {f.strip().lower() for f in family.split(",") if f.strip()}
        recs = [r for r in recs if (r.get("template_family") or "").lower() in fams]
    if country:
        ccs = {c.strip().upper() for c in country.split(",") if c.strip()}
        recs = [r for r in recs if (r.get("exchange_country") or "").upper() in ccs]
    recs.sort(key=lambda r: -(r.get("market_cap_usd") or 0))
    # Trim payload — return only fields the dashboard renders.
    out = [{
        "ticker": r["ticker"],
        "company_name": r["company_name"],
        "exchange_country": r.get("exchange_country", ""),
        "sector": r.get("sector", ""),
        "industry": r.get("industry", ""),
        "template_family": r.get("template_family", "other"),
        "currency": r.get("currency", ""),
        "market_cap_usd": r.get("market_cap_usd"),
        "is_depositary_receipt": r.get("is_depositary_receipt", False),
        "underlying_ticker": r.get("underlying_ticker"),
    } for r in recs[:limit]]
    return {"total": len(recs), "returned": len(out), "tickers": out}


@app.get("/api/v2/ticker/{ticker}")
def v2_ticker_info(ticker: str):
    """Return the full registry record for a single ticker.
    Useful for the dashboard's per-ticker detail view and for the
    deck-generation modal's pre-confirmation."""
    from src.services.ticker_registry import get_ticker_info
    return get_ticker_info(ticker)


@app.get("/api/v2/bloomberg/manifest/{ticker}")
def v2_bloomberg_manifest_get(ticker: str):
    """Return whether Bloomberg is enabled for a ticker (manifest read)."""
    from pathlib import Path as _P
    import json as _json
    p = _P("data/bloomberg") / f"{ticker}.manifest.json"
    if not p.is_file():
        return {"ticker": ticker, "enabled": False, "manifest_present": False}
    try:
        m = _json.loads(p.read_text())
        return {"ticker": ticker, "enabled": bool(m.get("enabled")),
                "manifest_present": True, "manifest": m}
    except Exception as exc:
        return {"ticker": ticker, "enabled": False, "manifest_present": True,
                "error": str(exc)}


@app.post("/api/v2/bloomberg/manifest/{ticker}/toggle")
def v2_bloomberg_manifest_toggle(ticker: str, enabled: bool):
    """Enable / disable Bloomberg override for a ticker without re-uploading."""
    from pathlib import Path as _P
    import json as _json
    from datetime import datetime as _dt2, timezone as _tz2
    p = _P("data/bloomberg") / f"{ticker}.manifest.json"
    if not p.is_file():
        if enabled:
            raise HTTPException(status_code=404,
                                  detail=f"No Bloomberg file for {ticker} — upload one first.")
        return {"ticker": ticker, "enabled": False, "manifest_present": False}
    try:
        m = _json.loads(p.read_text())
    except Exception:
        m = {"ticker": ticker}
    m["enabled"] = bool(enabled)
    m["toggled_at"] = _dt2.now(_tz2.utc).isoformat(timespec="seconds")
    p.write_text(_json.dumps(m, indent=2))
    return {"ticker": ticker, "enabled": bool(enabled), "manifest_present": True}


@app.get("/api/v2/disclosed_status")
def v2_disclosed_status(tickers: str | None = None):
    """Per-ticker disclosed-pipeline coverage.

    `tickers` is a comma-separated list. When omitted, returns coverage
    for every ticker in KNOWN_IR_PATTERNS (the universe we can populate
    automatically). Empty entries mean we haven't pulled IR PDFs yet.

    Frontend uses this to render a coverage matrix in the dashboard:
    which tickers have disclosed actuals, how recent, how complete.
    """
    from src.services.disclosed_status import assess_universe
    # Resolve the ticker list.
    if tickers:
        target_list = [t.strip() for t in tickers.split(",") if t.strip()]
    else:
        try:
            from scripts.populate_disclosed import KNOWN_IR_PATTERNS
            target_list = sorted(KNOWN_IR_PATTERNS.keys())
        except Exception:
            target_list = []
    covers = assess_universe(target_list)
    return {
        "tickers": [c.as_dict() for c in covers],
        "n_total": len(covers),
        "n_with_data": sum(1 for c in covers if c.file_exists),
        "n_complete": sum(1 for c in covers if c.fields_complete),
    }


@app.get("/api/v2/voice_eval_latest")
def v2_voice_eval_latest():
    """Return the most-recent voice-eval JSON snapshot.

    Read from data/voice_eval/{date}.json. Useful for the dashboard
    to surface voice-drift over time without re-scoring on every page
    load. If no snapshot exists yet, returns an empty result."""
    from pathlib import Path as _P
    import json as _json
    eval_dir = _P(__file__).resolve().parents[1] / "data" / "voice_eval"
    if not eval_dir.is_dir():
        return {"results": {}, "aggregate": {}, "snapshot_date": None}
    snapshots = sorted(eval_dir.glob("*.json"), reverse=True)
    if not snapshots:
        return {"results": {}, "aggregate": {}, "snapshot_date": None}
    try:
        payload = _json.loads(snapshots[0].read_text())
        payload["snapshot_date"] = snapshots[0].stem
        return payload
    except (OSError, _json.JSONDecodeError):
        return {"results": {}, "aggregate": {}, "snapshot_date": None}


@app.get("/api/v2/decks")
def v2_recent_decks(limit: int = 30):
    """List recently generated decks from the outputs/ folder.

    Returns newest-first. Each record carries the filename, ticker
    (parsed from the filename prefix), file size, mtime, and a
    presigned-style relative download URL. The deck library view.
    """
    from pathlib import Path as _P
    from src.config import report_output_dir
    out_dir = report_output_dir()
    if not out_dir or not _P(out_dir).is_dir():
        return {"decks": []}
    files = sorted(_P(out_dir).glob("*.pptx"),
                    key=lambda p: p.stat().st_mtime, reverse=True)
    decks = []
    for f in files[:limit]:
        st = f.stat()
        ticker = f.stem.split("_", 1)[0]
        prov = f.with_suffix(".provenance.xlsx")
        decks.append({
            "filename": f.name,
            "ticker": ticker,
            "size_kb": int(st.st_size / 1024),
            "mtime": _dt.utcfromtimestamp(st.st_mtime).isoformat() + "Z",
            "has_provenance": prov.is_file(),
        })
    return {"decks": decks}


@app.get("/api/reports")
def list_reports():
    """List pipeline runs for frontend reports table."""
    from src.storage.db import list_runs
    runs = list_runs()
    return {"reports": [_run_to_report_row(r) for r in runs]}


@app.get("/api/reports/{run_id}")
def get_report(run_id: str):
    """Get one run (steps, no payload)."""
    from src.storage.db import load_run
    run = load_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Report not found")
    row = _run_to_report_row(run)
    row["steps"] = run.get("step_results", [])
    return row


@app.get("/api/reports/{run_id}/bundle")
def download_report_bundle(run_id: str):
    """Atomic download — returns a .zip containing BOTH the .pptx deck
    AND the .provenance.xlsx sidecar in a single response.

    Why: Render's free tier wipes /tmp when the container restarts (idle
    timeout, redeploy, OOM). The previous flow asked the analyst to
    click two separate download buttons — pptx first, xlsx second. If
    the container restarted between clicks (common with idle timeout
    ~15 min on free tier), the second download 404'd with "File no
    longer available". Bundling eliminates the race entirely: one
    response, both files, no chance to miss one.

    Resolution: run_id → DB → memo_filename (.pptx) → derive .xlsx by
    suffix swap. When the xlsx is missing for some reason (rare), the
    zip still contains the pptx — better than failing outright.
    """
    import io as _io
    import zipfile as _zipfile
    from fastapi.responses import StreamingResponse
    from src.storage.db import load_run
    from src.config import report_output_dir

    out_dir = report_output_dir()
    run = load_run(run_id)
    if not run:
        raise HTTPException(status_code=404,
                              detail="Report run not found in DB")
    pptx_filename = (run.get("memo_path") or "").strip()
    if not pptx_filename or ".." in pptx_filename:
        raise HTTPException(status_code=404,
                              detail="No report file for this run")
    pptx_path = out_dir / pptx_filename
    if not pptx_path.is_file():
        raise HTTPException(status_code=404,
                              detail=f"Deck not found on disk: {pptx_filename}")
    xlsx_filename = pptx_filename[:-len(".pptx")] + ".provenance.xlsx" \
                     if pptx_filename.lower().endswith(".pptx") else None
    xlsx_path = (out_dir / xlsx_filename) if xlsx_filename else None

    buf = _io.BytesIO()
    with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
        zf.write(pptx_path, arcname=pptx_filename)
        if xlsx_path and xlsx_path.is_file():
            zf.write(xlsx_path, arcname=xlsx_filename)
    buf.seek(0)
    bundle_name = pptx_filename[:-len(".pptx")] + "_bundle.zip" \
                    if pptx_filename.lower().endswith(".pptx") \
                    else f"{run_id}_bundle.zip"
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{bundle_name}"',
                  "Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/api/reports/{run_id}/download")
def download_report(run_id: str, filename: str | None = None, type: str | None = None):
    """Download an artefact from this run.

    Query params:
      `filename` — explicit filename to serve (path-traversal protected;
                   must exist in report_output_dir()).
      `type`     — `report` (default, serves the .pptx deck) OR
                   `provenance` (serves the .xlsx data-trace sidecar
                   that's written alongside every deck — every cell on
                   the deck traced back to its source provider, URL,
                   data period, and fetch timestamp).

    Resolution order:
      1. `?filename=<name>` (explicit).
      2. `?type=provenance` (auto-resolves the sidecar from the deck's
         filename in the DB).
      3. DB lookup by run_id → memo_path field (the .pptx).
    """
    from src.storage.db import load_run
    from src.config import report_output_dir
    out_dir = report_output_dir()

    memo_filename: str | None = None
    if filename:
        candidate = filename.strip().replace("\\", "/").split("/")[-1]
        if candidate and ".." not in candidate:
            memo_filename = candidate

    if not memo_filename:
        run = load_run(run_id)
        if not run:
            raise HTTPException(
                status_code=404,
                detail="Report not found (run_id absent from DB and no filename provided)",
            )
        memo_filename = (run.get("memo_path") or "").strip()
        if not memo_filename or ".." in memo_filename:
            raise HTTPException(status_code=404, detail="No report file for this run")

    # Provenance sidecar swap: take the deck filename and substitute
    # the `.pptx` suffix with `.provenance.xlsx`. The sidecar is
    # written by `generate_report._write_jabal_preview` via
    # `out_path.with_suffix(".provenance.xlsx")`, so the deck and
    # sidecar always live in the same directory with a deterministic
    # name pair.
    if (type or "").strip().lower() == "provenance":
        if memo_filename.lower().endswith(".pptx"):
            memo_filename = memo_filename[:-len(".pptx")] + ".provenance.xlsx"
        else:
            raise HTTPException(
                status_code=404,
                detail="Provenance sidecar only available for .pptx reports",
            )

    file_path = out_dir / memo_filename
    if not file_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"File no longer available: {memo_filename}",
        )
    if memo_filename.lower().endswith(".pptx"):
        media = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    elif memo_filename.lower().endswith(".xlsx"):
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    elif memo_filename.lower().endswith(".docx"):
        media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    else:
        media = "application/octet-stream"
    response = FileResponse(path=str(file_path), media_type=media, filename=memo_filename)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def _readiness_error_detail(results: list) -> dict | None:
    """If report_readiness failed, return a structured error for HTTP 422."""
    from src.models.step_result import Status

    for r in results:
        if r.step_name == "report_readiness" and r.status == Status.FAILED:
            d = r.data if isinstance(r.data, dict) else {}
            return {
                "error": "report_not_ready",
                "summary": d.get("summary") or (r.error_detail or "") or r.message,
                "reasons": d.get("reasons", []),
                "step_failures": d.get("step_failures", []),
            }
    return None


def _run_preview_and_response(ticker: str, skip_llm: bool = True, *, raise_on_readiness: bool = True) -> dict:
    """Run pipeline and return frontend-shaped response: report row + payload + steps."""
    from src.pipeline import run_preview
    from src.models.step_result import Status
    from src.storage.db import load_company, load_run

    run_id, results = run_preview(ticker, skip_llm=skip_llm)
    err = _readiness_error_detail(results)
    if err and raise_on_readiness:
        raise HTTPException(status_code=422, detail=err)
    statuses = {r.status for r in results}
    overall = "partial" if (Status.FAILED in statuses or Status.PARTIAL in statuses) else "success"
    # run_id comes directly from pipeline (no fragile extraction needed)
    payload = None
    for r in results:
        if r.step_name == "build_report_payload" and r.data is not None and r.status != Status.FAILED:
            try:
                payload = r.data.model_dump(mode="json")
            except Exception:
                payload = None

    steps = [r.to_log_dict() for r in results]

    # Do not return 200 if the client expects a file: generate_report failed → download would 404.
    if raise_on_readiness:
        gen = next((s for s in steps if s.get("step_name") == "generate_report"), None)
        if gen and gen.get("status") == "failed":
            reasons = []
            if gen.get("error_detail"):
                reasons.append(str(gen["error_detail"]))
            if gen.get("message") and gen.get("message") not in reasons:
                reasons.append(str(gen["message"]))
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "pptx_generation_failed",
                    "summary": gen.get("message") or "PPTX file was not created",
                    "reasons": reasons or ["Report generation step failed"],
                    "step_failures": [],
                },
            )

    # If the pipeline's save_run failed (now surfaced as a `save_run` step),
    # the download endpoint would 404 with "Report not found". Try to persist
    # here using in-memory step data; if that also fails, raise 500 with the
    # actual DB error so the user sees something actionable.
    save_failure = next(
        (s for s in steps if s.get("step_name") == "save_run" and s.get("status") == "failed"),
        None,
    )
    run = load_run(run_id) if run_id else None
    if run_id and run is None and raise_on_readiness:
        from src.storage.db import save_run as _save_run
        from datetime import datetime as _dt, timezone as _tz
        gen_step = next((s for s in steps if s.get("step_name") == "generate_report"), None)
        memo_path = None
        if gen_step and gen_step.get("status") == "success" and gen_step.get("data"):
            from pathlib import Path as _P
            memo_path = _P(str(gen_step["data"])).name
        try:
            _save_run(
                run_id, ticker, "preview",
                _dt.now(_tz.utc).isoformat(), _dt.now(_tz.utc).isoformat(),
                overall, steps, memo_path=memo_path,
            )
            run = load_run(run_id)
        except Exception as db_exc:
            detail = (save_failure or {}).get("error_detail") or f"{type(db_exc).__name__}: {db_exc}"
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "run_persist_failed",
                    "summary": "Generated the report but could not persist the run record.",
                    "reasons": [detail],
                    "step_failures": [],
                },
            )

    company = load_company(ticker) if ticker else None
    row = _run_to_report_row(run) if run else {
        "id": run_id,
        "ticker": ticker,
        "company": (company or {}).get("company_name") or ticker,
        "country": (company or {}).get("country") or "",
        "status": "COMPLETED" if overall in ("success", "partial") else "FAILED",
        "created": "",
        "warnings": sum(1 for s in steps if s.get("status") in ("partial", "failed")),
    }
    if run:
        row["created"] = run.get("started_at") or row["created"]

    # Surface the generated filename so the frontend can download even if
    # the DB row isn't queryable. Stored on row["filename"] for the
    # `?filename=` query param of /api/reports/{id}/download.
    gen_step_done = next(
        (s for s in steps if s.get("step_name") == "generate_report" and s.get("status") == "success"),
        None,
    )
    if gen_step_done and gen_step_done.get("data"):
        from pathlib import Path as _P
        row["filename"] = _P(str(gen_step_done["data"])).name

    out: dict = {"report": row, "payload": payload, "steps": steps}
    if not raise_on_readiness:
        out["readiness_error"] = err
    return out


class CreateReportRequest(BaseModel):
    ticker: str = Field(..., description="Yahoo-format ticker, e.g. 2010.SR")
    skip_llm: bool = Field(True, description="Skip Gemini news summarization")


import re as _re

_TICKER_RE = _re.compile(r"^[A-Z0-9\.\-]{1,20}$")


@app.post("/api/reports")
def create_report(req: CreateReportRequest):
    """Create a report synchronously. Returns report row + payload + steps."""
    ticker = (req.ticker or "").strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker is required")
    if not _TICKER_RE.match(ticker):
        raise HTTPException(status_code=400, detail="Invalid ticker format")
    try:
        data = _run_preview_and_response(ticker, skip_llm=req.skip_llm)
        return data
    except HTTPException:
        raise
    except Exception as e:
        msg = str(e).strip() or repr(e)
        raise HTTPException(status_code=500, detail=f"Report creation failed: {msg}")


@app.post("/api/reports/{run_id}/rerun")
def rerun_report(run_id: str):
    """Rerun preview for the same ticker. Returns report row + payload + steps."""
    from src.storage.db import load_run
    run = load_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Report not found")
    ticker = run.get("ticker", "").strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker not found for run")
    try:
        return _run_preview_and_response(ticker, skip_llm=True)
    except HTTPException:
        raise
    except Exception as e:
        msg = str(e).strip() or repr(e)
        raise HTTPException(status_code=500, detail=f"Rerun failed: {msg}")


@app.post("/api/preview", response_model=PreviewResponse)
def run_preview_api(req: PreviewRequest):
    from src.pipeline import run_preview
    from src.models.step_result import Status

    ticker = (req.ticker or "").strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker is required")

    run_id, results = run_preview(ticker, skip_llm=req.skip_llm)
    err = _readiness_error_detail(results)
    if err:
        raise HTTPException(status_code=422, detail=err)

    statuses = {r.status for r in results}
    overall = "partial" if (Status.FAILED in statuses or Status.PARTIAL in statuses) else "success"
    payload = None
    for r in results:
        if r.step_name == "build_report_payload" and r.status != Status.FAILED and r.data is not None:
            try:
                payload = r.data.model_dump(mode="json")
            except Exception:
                payload = None

    steps = [r.to_log_dict() for r in results]
    return PreviewResponse(run_id=run_id, overall=overall, steps=steps, payload=payload)


# ─── Earnings calendar ───────────────────────────────────────────────────

import threading as _threading
import uuid as _uuid
from datetime import datetime as _dt, timedelta as _td

# In-memory job registry for background refreshes (session-scoped).
_calendar_jobs: dict[str, dict] = {}
_calendar_jobs_lock = _threading.Lock()


def _iso_date_or_none(s: str | None) -> str | None:
    if not s:
        return None
    try:
        # Accept YYYY-MM-DD (strict) to avoid surprises
        _dt.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        return None


@app.get("/api/calendar")
def get_calendar(
    start: str | None = None,
    end: str | None = None,
    countries: str | None = None,
    confirmed: int = 0,
):
    """Return earnings events in [start, end] (inclusive).

    Defaults to a 60-day window around today if start/end omitted.
    `countries` is an optional comma-separated list of ISO-2 codes.
    `confirmed=1` returns only confirmed events.

    Auto-refresh: when the DB has < 20 events in the requested window
    (cold start / first visit) and no refresh job is already running,
    kick off a background backfill so the next page-load has data.
    This request still returns immediately with whatever's in the DB.
    """
    from src.storage.db import list_calendar_events

    s = _iso_date_or_none(start)
    e = _iso_date_or_none(end)
    if not s or not e:
        today = _dt.utcnow().date()
        if not s:
            s = (today - _td(days=14)).isoformat()
        if not e:
            e = (today + _td(days=46)).isoformat()
    country_list = None
    if countries:
        country_list = [c.strip().upper() for c in countries.split(",") if c.strip()]
    events = list_calendar_events(
        start=s,
        end=e,
        countries=country_list,
        confirmed_only=bool(confirmed),
    )

    # Auto-trigger a backfill on cold start / sparse DB. Idempotent —
    # only fires when no other refresh job is already in flight.
    refresh_started = False
    try:
        if len(events) < 20:
            with _calendar_jobs_lock:
                any_running = any(
                    j.get("status") in ("queued", "running")
                    for j in _calendar_jobs.values()
                )
            if not any_running:
                _kickoff_auto_refresh()
                refresh_started = True
    except Exception as exc:
        log.warning("calendar auto-refresh trigger failed: %s", exc)

    return {
        "start": s, "end": e, "events": events,
        "auto_refresh_started": refresh_started,
    }


def _kickoff_auto_refresh() -> str:
    """Spin up a background refresh job (same path as the manual button).

    Returns the job_id. Idempotent in practice — `get_calendar` only
    calls this when no other job is running.
    """
    import uuid as _uuid
    job_id = str(_uuid.uuid4())
    with _calendar_jobs_lock:
        _calendar_jobs[job_id] = {
            "status": "queued",
            "auto": True,
            "started_at": _dt.utcnow().isoformat() + "Z",
            "summary": None,
        }
    _threading.Thread(
        target=_run_calendar_refresh_job,
        args=(job_id, None, False),
        daemon=True,
    ).start()
    log.info("[calendar] auto-refresh job kicked off: %s", job_id)
    return job_id


@app.get("/api/calendar/ticker/{ticker}")
def get_calendar_for_ticker(ticker: str):
    from src.storage.db import list_calendar_for_ticker
    ticker = (ticker or "").strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker is required")
    return {"ticker": ticker, "events": list_calendar_for_ticker(ticker)}


class CalendarRefreshRequest(BaseModel):
    tickers: list[str] | None = Field(None, description="Optional ticker list; defaults to all seeded companies")
    force: bool = Field(False, description="Bypass freshness check and re-fetch every ticker")


def _run_calendar_refresh_job(job_id: str, tickers: list[str] | None, force: bool) -> None:
    from src.services.fetch_calendar import refresh_calendar
    with _calendar_jobs_lock:
        _calendar_jobs[job_id]["status"] = "running"
    try:
        summary = refresh_calendar(tickers=tickers, force=force)
        with _calendar_jobs_lock:
            _calendar_jobs[job_id]["status"] = "completed"
            _calendar_jobs[job_id]["summary"] = summary
            _calendar_jobs[job_id]["finished_at"] = _dt.utcnow().isoformat() + "Z"
    except Exception as exc:
        with _calendar_jobs_lock:
            _calendar_jobs[job_id]["status"] = "failed"
            _calendar_jobs[job_id]["error"] = str(exc)
            _calendar_jobs[job_id]["finished_at"] = _dt.utcnow().isoformat() + "Z"


@app.post("/api/calendar/refresh")
def post_calendar_refresh(req: CalendarRefreshRequest):
    """Kick off an async refresh of the earnings calendar. Returns a job_id."""
    tickers = [t.strip().upper() for t in (req.tickers or []) if (t or "").strip()] or None
    job_id = _uuid.uuid4().hex[:12]
    with _calendar_jobs_lock:
        _calendar_jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "started_at": _dt.utcnow().isoformat() + "Z",
            "finished_at": None,
            "summary": None,
            "error": None,
        }
    t = _threading.Thread(
        target=_run_calendar_refresh_job,
        args=(job_id, tickers, bool(req.force)),
        daemon=True,
    )
    t.start()
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/calendar/refresh/{job_id}")
def get_calendar_refresh_status(job_id: str):
    with _calendar_jobs_lock:
        job = _calendar_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/batch")
def batch_preview_api(req: BatchPreviewRequest):
    """Run previews for multiple tickers (PPTX output per ticker). Max 20."""
    tickers = [t.strip().upper() for t in (req.tickers or []) if (t or "").strip()]
    if not tickers:
        raise HTTPException(status_code=400, detail="tickers is required")
    if len(tickers) > 20:
        raise HTTPException(status_code=400, detail="Maximum 20 tickers per batch")

    results = []
    for t in tickers:
        results.append(_run_preview_and_response(t, skip_llm=req.skip_llm, raise_on_readiness=False))
    return {"results": results}


# ─── Jabal deck routes: pre-rendered, zero-compute consumer path ───────────
#
# Decks are rendered offline by `scripts/render_panel_decks.py` (run
# nightly via Render cron). Hosting a deck is then a static-file lookup —
# clicking a panel ticker is INSTANT for the consumer (no 30-90s pipeline
# wait, no Cloudflare-protected source on the request path).
#
# Off-panel tickers still flow through /api/reports (the legacy on-demand
# path); the Jabal panel is the **fast lane** for the curated 10.

_DECKS_DIR = ROOT_DIR / "static" / "decks"


def _decks_index() -> dict:
    p = _DECKS_DIR / "index.json"
    if not p.is_file():
        return {"panel_size": 0, "rendered_at": None, "decks": []}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {"panel_size": 0, "rendered_at": None, "decks": []}


@app.get("/api/jabal/panel")
def jabal_panel_list():
    """List all pre-rendered Jabal panel decks."""
    return _decks_index()


@app.get("/api/jabal/{ticker}")
def jabal_deck_meta(ticker: str):
    """Metadata for one ticker's pre-rendered deck (sources, confidence,
    last refresh time, file size)."""
    safe = ticker.strip().upper()
    p = _DECKS_DIR / f"{safe}.meta.json"
    if not p.is_file():
        raise HTTPException(status_code=404,
            detail=f"No pre-rendered deck for {safe}. "
                   "Use POST /api/reports for on-demand rendering.")
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/jabal/{ticker}/deck.pptx")
def jabal_deck_download(ticker: str):
    """Serve the static PPTX for one panel ticker."""
    safe = ticker.strip().upper()
    p = _DECKS_DIR / f"{safe}.pptx"
    if not p.is_file():
        raise HTTPException(status_code=404,
            detail=f"No pre-rendered deck for {safe}.")
    return FileResponse(
        str(p),
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=f"{safe}_jabal_preview.pptx",
    )


@app.post("/api/bloomberg/upload")
async def bloomberg_upload(file: UploadFile = File(...)):
    """Accept a CSV (or .xlsx → converted to CSV by pandas) of Bloomberg
    BEST consensus data, persist it to data/bloomberg/consensus.csv, run
    a refresh + re-render for every ticker present in the file.

    Returns: {tickers_updated, rows_persisted, decks_rerendered}."""
    import csv as _csv
    import io
    import subprocess
    import sys

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty upload")

    # Convert .xlsx → CSV in-memory if needed.
    filename = (file.filename or "").lower()
    if filename.endswith(".xlsx") or filename.endswith(".xls"):
        try:
            import pandas as pd
            df = pd.read_excel(io.BytesIO(raw))
            buf = io.StringIO()
            df.to_csv(buf, index=False)
            text = buf.getvalue()
        except Exception as exc:
            raise HTTPException(status_code=400,
                detail=f"Excel parse failed: {type(exc).__name__}: {exc}")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1", errors="replace")

    # Validate header
    reader = _csv.DictReader(io.StringIO(text))
    required = {"ticker", "period_type", "metric", "mean"}
    missing = required - set(reader.fieldnames or [])
    if missing:
        raise HTTPException(status_code=400,
            detail=f"Missing required columns: {sorted(missing)}")

    # Persist to data/bloomberg/consensus.csv
    tickers_seen: set[str] = set()
    rows = list(reader)
    for r in rows:
        if r.get("ticker"):
            tickers_seen.add(r["ticker"].strip().upper())

    target = ROOT_DIR / "data" / "bloomberg" / "consensus.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")

    # OPT-IN: write a per-ticker manifest that enables Bloomberg as the
    # canonical source. Without this manifest the pipeline ignores any
    # data/bloomberg/{ticker}_*.xlsx files on disk and uses free-source
    # data — the analyst explicitly opts in here when they upload.
    import json as _json
    from datetime import datetime as _dt2, timezone as _tz2
    for t in tickers_seen:
        manifest_path = ROOT_DIR / "data" / "bloomberg" / f"{t}.manifest.json"
        manifest_path.write_text(_json.dumps({
            "ticker": t,
            "enabled": True,
            "uploaded_at": _dt2.now(_tz2.utc).isoformat(timespec="seconds"),
            "source_filename": file.filename,
            "note": ("Bloomberg consensus is enabled for this ticker. The "
                      "deck will prefer Bloomberg values over free-source "
                      "estimates and provenance.xlsx will document the "
                      "Bloomberg path. Toggle off by deleting this file "
                      "or setting enabled=false."),
        }, indent=2))

    # Refresh + re-render. Run in subprocess so the API request returns
    # promptly; the cron path is the canonical way to refresh, this is the
    # one-off "I just uploaded" trigger.
    decks_rerendered = []
    for t in sorted(tickers_seen):
        try:
            subprocess.run(
                [sys.executable, "-m", "scripts.daily_refresh",
                 "--cadence=weekly", "--only=bloomberg", f"--tickers={t}"],
                timeout=60, check=False, capture_output=True,
            )
            subprocess.run(
                [sys.executable, "-m", "scripts.daily_refresh",
                 "--cadence=quarterly", "--only=bloomberg", f"--tickers={t}"],
                timeout=60, check=False, capture_output=True,
            )
            subprocess.run(
                [sys.executable, "-m", "scripts.render_panel_decks",
                 "--skip-refresh", f"--tickers={t}"],
                timeout=60, check=False, capture_output=True,
            )
            decks_rerendered.append(t)
        except Exception:
            continue

    return {
        "tickers_in_file": sorted(tickers_seen),
        "rows_persisted":  len(rows),
        "decks_rerendered": decks_rerendered,
        "file": str(target.name),
    }


@app.get("/jabal", response_class=HTMLResponse)
def jabal_panel_page():
    """Minimal HTML page that lists the panel with one-click download.

    Exists so the panel is browseable WITHOUT a frontend build — the
    hosted site can link to /jabal as the public "research downloads"
    surface even before the React SPA picks up the new endpoints."""
    idx = _decks_index()
    decks = idx.get("decks", [])
    rendered_at = idx.get("rendered_at", "—")
    rows = []
    for d in decks:
        ticker = d.get("ticker", "")
        name   = d.get("company_name", ticker)
        sector = d.get("sector", "—")
        sources = ", ".join(d.get("sources", []))
        conf = d.get("by_confidence", {})
        conf_str = (
            f"<span class='conf high'>{conf.get('High', 0)} H</span>"
            f" / <span class='conf med'>{conf.get('Medium', 0)} M</span>"
            f" / <span class='conf low'>{conf.get('Low', 0)} L</span>"
        )
        rows.append(f"""
        <tr>
          <td><span class='tk'>{ticker}</span></td>
          <td>{name}</td>
          <td class='dim'>{sector}</td>
          <td>{conf_str}</td>
          <td class='dim'>{sources}</td>
          <td><a href='/api/jabal/{ticker}/deck.pptx' class='dl'>Download .pptx</a></td>
        </tr>""")
    if not rows:
        rows.append("<tr><td colspan='6' class='dim'>No decks rendered yet. "
                     "Run <code>python -m scripts.render_panel_decks</code>.</td></tr>")
    html = f"""<!doctype html>
<html><head><meta charset='utf-8'><title>Jabal Research — Panel Decks</title>
<style>
  body {{ font: 14px/1.5 Calibri, sans-serif; background:#FAF8F4; color:#1A1A1A; margin:0; padding:0; }}
  .wrap {{ max-width: 1080px; margin: 40px auto; padding: 0 24px; }}
  h1 {{ font: 26px Georgia, serif; margin: 0 0 4px; }}
  .sub {{ color:#5C5C5C; font-size: 11px; letter-spacing: 0.5px; text-transform: uppercase; margin-bottom: 32px; }}
  .meta {{ color:#9A9A9A; font-size: 12px; margin-bottom: 24px; }}
  table {{ width: 100%; border-collapse: collapse; background:#fff; border: 1px solid #EFE8DC; }}
  th, td {{ padding: 10px 14px; text-align: left; border-bottom: 1px solid #EFE8DC; }}
  th {{ font-size: 10.5px; color:#5C5C5C; text-transform: uppercase; letter-spacing:0.5px; background:#EFE8DC; }}
  .tk {{ font-weight: 700; font-family: Consolas, monospace; }}
  .dim {{ color:#5C5C5C; }}
  .conf {{ font-weight: 600; }}
  .conf.high {{ color:#2F7D4F; }}
  .conf.med {{ color:#A28860; }}
  .conf.low {{ color:#9A9A9A; }}
  .dl {{ color:#7E6849; text-decoration: none; font-weight: 600; }}
  .dl:hover {{ text-decoration: underline; }}
  footer {{ margin-top: 32px; color:#9A9A9A; font-size: 12px; }}
  code {{ background: #EFE8DC; padding: 1px 4px; border-radius: 2px; }}
</style></head>
<body><div class='wrap'>
<h1>Jabal Research — Panel Decks</h1>
<div class='sub'>Institutional Research &middot; Earnings Previews</div>
<div class='meta'>{len(decks)} ticker(s) &middot; rendered {rendered_at} &middot;
free-source stack (yahoo, marketscreener, investing, macro, ishares, commodities, ir_pdf)</div>
<table>
  <thead><tr><th>Ticker</th><th>Company</th><th>Sector</th>
       <th>Data confidence</th><th>Sources</th><th>Deck</th></tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
<footer>
  Confidence: <span class='conf high'>H</span>igh (filing-grade or 3-source) &middot;
  <span class='conf med'>M</span>edium (2-source ≤2% drift) &middot;
  <span class='conf low'>L</span>ow (single-source).<br>
  Search any other ticker via the <a href='/'>main app</a>.
</footer>
</div></body></html>"""
    return html


# ─── One site: serve frontend from static/ when present ─────────────────────

if SERVE_FRONTEND:
    assets_dir = STATIC_DIR / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/{path:path}")
    def serve_spa(path: str):
        """SPA fallback: serve index.html for non-API routes (e.g. /reports/123)."""
        if path.startswith("api/") or path == "api":
            raise HTTPException(status_code=404, detail="Not found")
        if path.startswith("docs") or path.startswith("openapi") or path == "health":
            raise HTTPException(status_code=404, detail="Not found")
        # /jabal is served by an explicit handler above; the catch-all must
        # not intercept it (otherwise FastAPI returns the SPA shell).
        if path == "jabal" or path.startswith("jabal/"):
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(STATIC_INDEX, media_type="text/html")
