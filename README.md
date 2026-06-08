# Jabal Earnings-Preview Deck Generator

Type one equity ticker → get a polished **3-slide institutional PowerPoint
earnings-preview deck** plus an **Excel provenance sidecar**, delivered as a
single `.zip`. Built entirely from free public data, with an LLM writing the
analyst commentary. Universe: ~500 global emerging/frontier names (Gulf,
China/HK, India, …).

Every number is source-traceable; the prose is written in the company's own
sector vocabulary (a chip foundry never gets bank language).

---

## The 3-slide deck

1. **Snapshot** — company header; analyst-consensus box (rating · # analysts ·
   target · implied move — `—` when uncovered, never fabricated); key data
   (last close, market cap, next earnings, P/E FY-est, div yield TTM,
   currency); 1D/1W/1M/3M/6M/YTD performance; 52-week range; 5 interpretive
   highlight pills (EARNINGS · VALUATION · POSITIONING · WATCH · RISK); a
   truthful data-freshness banner.
2. **Thesis & Expectations** — 4-sentence executive summary; an
   earnings-expectations table (METRIC · ⟨quarter⟩E · YoY · QoQ · CONSENSUS)
   whose rows are the sector's key metrics; 3 catalysts; 3 risks; 3
   what-to-watch management questions.
3. **Valuation & Positioning** — 52-week price chart; forward-P/E chart;
   peer-comparables table (Peer Average + subject + 4–6 peers, all market caps
   in USD); EPS actual-vs-estimate history chart.

Plus `…​.provenance.xlsx`: one row per displayed number (Slide · Section ·
Metric · Value · Source · URL · Period · Fetched-At · Notes), a readiness
score, grounding-currency status, cross-source findings, and LLM-prose
attribution.

---

## Architecture — how the data fills (the important part)

The renderer reads exclusively from a **canonical store** (SQLite). On every
on-demand render the requested ticker is refreshed through a layered,
gap-filling pipeline so the deck never ships half-empty:

| Tier | Source | Provides | Notes |
|------|--------|----------|-------|
| **A — backbone** | **Yahoo Finance** (`yfinance`) | price, market cap, 52-wk, performance, trailing+forward P/E, **analyst rating / target / # analysts**, **forward EPS & revenue estimates**, **EPS earnings history** | **NOT Cloudflare-blocked → works from Render's datacenter IPs.** This is what makes single-ticker generation reliable. |
| **B — grounding** | `data/disclosed/*.json` | verified full-year actuals the LLM cites | hand/semi-auto curated, fiscal-calendar aware |
| **C — enrichment / Yahoo-blind fallback** | Investing.com + MarketScreener via **curl_cffi** (Chrome-TLS impersonation, the Cloudflare bypass) | consensus/target/forecasts/peers; primary for Yahoo-blind names (Oman MSX: `BKMB.OM`) | live first, committed snapshots last |
| **D — registry / FX** | `data/company_master.json`, FX table | peers, currency→USD normalization | every universe currency covered |

Sources are reconciled on a **trust ladder** (IR ▸ Bloomberg ▸ Investing ▸
Yahoo ▸ MarketScreener); the highest-trust source that returns a value wins,
and the freshness banner reflects the true per-class data age.

> **Why this matters:** the prior build leaned on the Cloudflare-blocked
> scrapers for consensus/estimates/earnings-history and shipped half-filled
> decks when they 403'd. Yahoo had all of it the whole time and isn't blocked.

The numeric pipeline is deterministic and unit-testable offline against the
committed fixtures; the LLM (Gemini, per-sector playbooks) is reserved for
prose, with every LLM number validated against the canonical anchors.

---

## Quick start (local)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then put your GEMINI_API_KEY in .env
python -m src.main --init-db  # seed SQLite from data/company_master.json

# Generate a deck (full, with Gemini prose):
python -m src.main --ticker 1180.SR --mode preview
# …or the fast numbers-only path:
python -m src.main --ticker 1180.SR --mode preview --skip-llm
# Output: outputs/1180.SR_<ts>_earnings_preview.pptx (+ .provenance.xlsx)
```

Run the web app locally:

```bash
uvicorn src.api:app --reload --port 8000   # UI + API at http://localhost:8000
```

Verified priority tickers (complete, sector-voiced decks):
`1180.SR` Saudi National Bank · `ORDS.QA` Ooredoo · `9988.HK` Alibaba ·
`0981.HK` SMIC · `2010.SR` SABIC · `2020.SR` SABIC Agri-Nutrients ·
`BKMB.OM` Bank Muscat (Yahoo-blind, via Investing/MS fallback).

---

## Deploy to Render

This repo ships a `render.yaml` blueprint (a web service + an optional nightly
warm cron).

1. Push this repo to GitHub (already at `github.com/rabigarip/final_earnings`).
2. In Render: **New ▸ Blueprint**, pick this repo. Render reads `render.yaml`.
3. **Set the secret:** on the `final-earnings-api` service, Environment tab,
   add `GEMINI_API_KEY` = your key (it's declared `sync:false`, so it is NOT in
   the repo and must be set here). If you keep the cron, set it there too.
4. Deploy. Health check: `GET /health`. The UI is served at `/`.

**Plan:** a full deck with Gemini prose takes ~60–100s. Render's **free** web
tier hard-caps requests at ~30s and will 502 — the blueprint requests the
paid **Starter** instance. For a free demo, call the API with `skip_llm=true`
(numbers-only, faster).

Writable paths (`DATABASE_PATH`, `REPORT_OUTPUT_DIR`) point at `/tmp` because
Render's project dir is read-only; the DB is re-seeded from
`data/company_master.json` on boot.

---

## Quality bar (enforced)

1. Every figure matches its source; no hallucinated numbers in prose.
2. Every slide number has a provenance row.
3. Commentary uses the company's own sector vocabulary — non-banks never get
   NIM / cost-of-risk / loan-growth language.
4. Data freshness detected and shown truthfully.
5. Revenue / EBITDA / net-income moves in a period stay internally consistent.
6. Missing data shows `—` and is flagged — never crashes, never fabricates.
7. No text overflow.

---

## Repo layout

```
src/providers/      data providers (Yahoo backbone, Investing/MS via curl_cffi, IR PDF, macro)
src/services/       pipeline, reconciliation, canonical store, renderers (render_jabal_*), provenance, LLM
scripts/            daily_refresh (canonical population), render_panel_decks (cron), init/db tooling
data/disclosed/     verified full-year actuals (grounding) — incl. all priority tickers
data/investing/     Investing.com snapshots (Gulf consensus fallback)
data/marketscreener/ MarketScreener snapshots (Gulf consensus/forecast fallback)
data/company_master.json, em_500_tickers.csv  — the ~500-name registry
frontend/ + static/  React/Vite UI (committed build in static/)
tests/              offline numeric-pipeline tests against committed fixtures
```
