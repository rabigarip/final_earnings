# Earnings-Preview Deck Generator — Restart Brief

**Audience:** an engineering agent rebuilding this product from scratch, free to
choose its own architecture, stack, and module structure.

**Why a rewrite:** the working product exists but has accreted three different
fetch conventions, scattered cache locations, sector logic bolted on after the
fact, and silent-divergence bugs (e.g. one data path bypassed the hardened
fetcher and shipped empty consensus). The *domain knowledge* below is the real
asset — it was learned the expensive way. Inherit it; re-architect everything
else.

This document specifies **what to build** and **the constraints/gotchas you must
respect**. It deliberately does **not** prescribe how — language, framework,
module layout, and data model are yours to decide.

---

## 1. Mission (one paragraph)

A buy-side analyst at a boutique asset manager types a single equity ticker into
a web app and, on demand, gets a polished **3-slide PowerPoint earnings-preview
deck** plus an **Excel provenance sidecar**, built entirely from free public
data, with an LLM writing the analyst commentary. The universe is ~500 global
emerging/frontier-market names (Gulf, China/HK, India, etc.). The bar is
**institutional**: every number must be correct and source-traceable, and the
prose must read like a sector specialist wrote it — not a generic template.

---

## 2. The product

- **Input:** one ticker (e.g. `2010.SR`, `9988.HK`, `BKMB.OM`). Free-text entry
  with autocomplete over the known universe; must also *attempt* any ticker the
  user types.
- **Output:** a single downloadable bundle (one `.zip`) containing:
  1. `<ticker>_<timestamp>_earnings_preview.pptx` — the 3-slide deck
  2. `<ticker>_<timestamp>_earnings_preview.provenance.xlsx` — the audit trail
- **Mode:** synchronous, on demand, **for the one requested ticker only** (no
  universe pre-compute required at request time — see §7).
- **Deploy target:** a cloud web host. Generation may take ~30–90s; assume a
  paid tier (no hard 30s request cap), but design so an LLM-skipped "fast path"
  is possible.
- **Branding:** the deck is white-labeled "Jabal Asset Management — Institutional
  Research." Footer on every slide: `CONFIDENTIAL · FOR INSTITUTIONAL &
  QUALIFIED INVESTORS ONLY` and `Jabal Asset Management · Regulated by the
  Financial Services Authority of Oman`. Keep branding data-driven/configurable.

---

## 3. Exact output spec — the deck

Three 16:9 slides. Numbers shown here are illustrative of *shape*, not values.

### Slide 1 — SNAPSHOT
- **Header:** company name, `TICKER · Sector · Industry · Exchange (Country)`,
  and a period label `Q<n> <year> Earnings Preview` (the *upcoming* quarter).
- **Analyst Consensus box:** rating (e.g. OUTPERFORM), analyst count, average
  target price, implied move vs current price. If no coverage, show "—" — do
  not fabricate.
- **Key Data:** Last close · Market cap · Next earnings date · P/E (FY est) ·
  Dividend yield (TTM) · Currency.
- **Recent Performance:** 1D / 1W / 1M / 3M / 6M / YTD price changes (%).
- **52-week range:** low–high with a marker at the current price.
- **Analyst Highlights:** exactly 5 one-line interpretive pills, in order —
  `EARNINGS`, `VALUATION`, `POSITIONING`, `WATCH`, `RISK`. Each frames an
  investor debate around a mechanism, ≤~22 words, sector-appropriate.
- **Data-freshness banner:** e.g. "Price live (0m ago) · Forecasts < 1d old".
  Must reflect the *true* age of each data class (see §6.4).
- **Footer:** source line, generation date, confidential notice.

### Slide 2 — THESIS & EXPECTATIONS
- **Executive Summary:** a 4-sentence (~80–110 word) interpretive paragraph.
  Pattern: "<Company> enters [quarter] earnings with focus on [4 sector levers]…
  Recent performance supported by […] while […] remain concerns… Investors
  should watch […]… The setup appears [balanced/asymmetric/…]." Must cite at
  least one concrete company figure.
- **Earnings Expectations table:** rows = the metrics that matter for the
  *sector* (e.g. revenue, EBITDA, net income, EPS, EBITDA margin for an
  industrial; or the bank equivalents). Columns = `METRIC | <quarter>E (own
  estimate, may be blank) | YoY | QoQ | CONSENSUS`. Row count flexes with data
  availability. Margin rows render YoY/QoQ in bps.
- **Catalysts:** 3 forward, company-specific upside drivers (trigger → effect).
- **Key Risks:** 3 distinct downside mechanisms (mechanism + impact).
- **What to Watch:** 3 sharp questions for management (each names a metric +
  threshold).

### Slide 3 — VALUATION & POSITIONING
- **52-week price chart.**
- **Forward P/E chart** (or "No P/E history available" when absent).
- **Peer Comparables table:** a "Peer Average" row, then the subject company
  (bold), then 4–6 peers. Columns: Company · Ticker · **MCAP (USD)** ·
  P/E · P/B · EV/EBITDA · Div Yield · 1Y Return. **All market caps in one
  currency (USD).**
- **Earnings history chart:** EPS actual vs estimate over recent quarters.

### Provenance sidecar (`.xlsx`)
Every displayed number gets a row: `Slide · Section · Metric · Value · Source ·
Source URL · Data Period · Fetched At · Notes`. Plus sections for: a
**data-completeness/readiness score**, **grounding currency** (is the FY
baseline current?), **cross-source validation findings**, **LLM prose** (marked
as model-generated with a context hash), and any **QA flags**. Rule of thumb:
*if a number is on a slide, an analyst must be able to trace it here.*

---

## 4. What "good" means (the quality bar)

These are the acceptance criteria. A deck fails review if any are violated:

1. **Numerically correct.** Every figure matches the cited source within
   rounding. No hallucinated numbers in prose.
2. **Source-traceable.** Every slide number has a provenance row.
3. **Sector-voiced.** Commentary uses the right vocabulary for the company's
   sector. A fertilizer, chip foundry, or telecom must **never** get bank
   language (NIM, cost of risk, loan growth) — this was the #1 quality defect.
4. **Fresh, or honestly stale.** Data age is detected and shown. Never present a
   live price beside a 3-week-old performance figure without flagging it.
5. **Internally consistent.** Revenue, EBITDA, and net income moves in the same
   period can't contradict (e.g. revenue −20%, EBITDA −26%, but net income +42%
   signals a base/alignment bug, not a real result).
6. **Complete or gracefully sparse.** Missing data shows "—" and is flagged; it
   never crashes and never silently fabricates.
7. **Fits.** No text overflow, no numbers spilling out of tables.

---

## 5. Data sources (free only) — and the hard constraints

You choose the providers; these are the ones that work and their realities.

| Source | Provides | Hard gotcha |
|---|---|---|
| **Yahoo Finance** (yfinance) | live price, market cap, 52-wk, some fundamentals | region-gated/404 for some non-US tickers; wrap every call defensively |
| **Investing.com** | price, **published** performance %, dividends, target/consensus, historical close series | **Cloudflare-blocks datacenter IPs.** Data can lag a few days. |
| **MarketScreener** | annual+quarterly financials, consensus rating/target, valuation multiples, peer sets, earnings calendar | **Cloudflare-blocks datacenter IPs.** Needs per-ticker slug resolution (ISIN/search). Values arrive in **varying unit scales**. |
| **Disclosed IR JSON** (your own curated store) | verified full-year actuals ("grounding") the LLM can cite | must be hand- or semi-auto-curated and kept current (see §6.5) |
| **LLM (e.g. Gemini)** | exec summary, highlights, catalysts, risks, watch-list | hallucinates numbers; "thinking" models truncate long JSON; needs validation + retry |

**The Cloudflare problem is central.** Live scraping from a cloud host gets HTTP
403. Viable solutions (pick one or combine): TLS-fingerprint impersonation
(Chrome via curl_cffi worked), a residential proxy, or a scheduled
**snapshot-refresh pipeline** that fetches from non-flagged IPs (e.g. CI
runners) and commits HTML/JSON the runtime falls back to. **Whatever you pick,
route _every_ fetch through _one_ hardened path** — a past bug was a single
data path (consensus) using a naive `requests` GET that bypassed the bypass, so
the consensus box came back empty only in production.

---

## 6. Domain rules you MUST encode (the irreplaceable knowledge)

### 6.1 Sector awareness is first-class
Classify every ticker into a sector family (bank / energy / materials / tech /
telco / industrial / consumer / healthcare / utility / REIT / …). The family
drives: which metrics appear in the slide-2 table, which "FY actuals" you ground
on, and — critically — the **LLM's vocabulary, levers, and few-shot examples**.
Maintain a per-family "playbook" (the operating levers, catalyst/risk/watch
exemplars) and inject the *right one* into every prompt, including any retry
prompts. Do not let bank framing be the default.

### 6.2 Numeric integrity
- Build a **whitelist of allowed numbers** from the fetched data + simple
  derivations; reject/flag any number in LLM prose that doesn't trace to it.
- **Cross-source reconcile** key figures (price, EPS, revenue) with a trust
  ladder and a confidence tier; surface disagreement rather than silently
  picking one.
- Sanity-bound every grounded metric (ROE, margins, CAR, growth rates) and drop
  out-of-range reads.

### 6.3 Currency
- **Reporting currency ≠ listing currency.** Alibaba (`9988.HK`) reports in CNY
  but lists in HKD; SMIC (`0981.HK`) reports in USD. Track both.
- **Peer market caps must unify to USD.** Your FX table must cover *every*
  currency in the universe (we got burned by a missing NOK → a peer rendered
  "NOK 125.8B" under a "MCAP (USD)" header and skewed the average). Prefer a
  live/maintained FX source over a hardcoded table.

### 6.4 Fiscal calendars & freshness
- Track **fiscal-year-end month** per ticker. Non-December years are common and
  break naive "FY2025" labeling: Indian banks and Alibaba end in **March**.
- Compute the **expected latest reported period** from (fiscal-year-end +
  reporting lag). Use it to (a) detect stale grounding and (b) avoid anchoring
  on a forward *estimate* column mislabeled as an actual.
- Display data age by class (live price vs forecast vs macro), truthfully.

### 6.5 Grounding (the FY-actuals store)
The LLM writes far better commentary when handed **verified full-year actuals**
to cite (e.g. "after FY2025's +29.9% net-profit growth"). Maintain a curated
store of these per ticker, on the sector schema. Two realities:
- It **doesn't scale by hand** across 500 names. Plan for semi-automated
  extraction from the financials feed **with a verification gate** (period must
  match the expected latest; values sanity-bounded; flagged as unverified until
  a human or a cross-check confirms) — auto-extracted source values *can be
  stale or mis-defined*, so never promote them blindly.
- It **goes stale** when the next year reports. Tie it to the fiscal calendar
  and re-extract.

### 6.6 Units
Financial values arrive in absolute units, thousands, millions, or billions —
varying by source, page, and exchange (e.g. Chinese A-shares print plain
millions; Gulf names print absolute with B/M suffixes). **Detect/normalize the
scale; never assume a fixed divisor.** Cross-check one metric against another of
known scale to calibrate.

### 6.7 Graceful degradation + quality gate
- Unknown ticker / missing source → a sparse but valid deck, never a crash.
- Compute a **per-ticker readiness score** (does it have grounded FY actuals,
  consensus, peers, a sector template, price history?) and expose it *before*
  generation so the analyst knows a thin name will yield a thin deck.

---

## 7. Architecture is yours — but respect these constraints

You decide stack, framework, data store, module boundaries, sync/async, caching.
Hard constraints only:

- **On-demand, single-ticker** at request time. (A *separate* scheduled job to
  warm snapshots/grounding is fine and probably necessary — but generation must
  not require the whole universe to be pre-built.)
- **Free data sources only.** No paid terminals required for the base product.
  (An optional override path for a user-uploaded broker file is a nice-to-have.)
- **One hardened fetch path** for any blocked source (§5).
- **Deterministic where it can be.** The numeric pipeline should be reproducible
  and unit-testable offline against fixtures; reserve the LLM for prose only.
- **Output format is fixed** (§3): 3-slide PPTX + provenance XLSX in one zip.

Open design questions you'll need to decide (we don't mandate answers):
- How to beat Cloudflare from your host (impersonation vs proxy vs snapshots).
- Pre-compute vs pure on-demand, and the caching strategy.
- How far to push auto-grounding vs curated grounding.
- Whether commentary is LLM-only, template-only, or hybrid with validation.
- Monorepo vs services; SQLite vs other.

---

## 8. Salvage list (verified assets worth porting, not rebuilding)

These represent real, checked work — reuse the *data*, re-implement the *code*:
- **`data/disclosed/*.json`** — ~16 tickers of hand-verified FY actuals
  (BKMB.OM, the four UAE banks, 2222.SR, 2020.SR, 2010.SR, 1180.SR, 9988.HK,
  0981.HK, ORDS.QA, 0700.HK, 1398.HK, ICICIBANK.NS). Web-verified against IR
  releases. Keep the numbers; you can change the schema.
- **The sector→metric schemas and the sector "playbook"** concept (per-family
  levers + exemplars) — the design that fixed the bank-contamination bug.
- **The FX/currency knowledge and the reporting-vs-listing-currency cases.**
- **The fiscal-year-end map** (which names are non-December).
- **The committed MarketScreener/Investing snapshots** (`data/marketscreener/`,
  `data/investing/`) as ready-made fetch fixtures for offline tests.
- **The ~500-name registry** (ticker → name, exchange, currency, peer set).

## 9. Explicitly out of scope (for v1)
- Real-time / intraday streaming.
- Paid data (Bloomberg/Refinitiv) as a base requirement.
- Multi-ticker batch decks in one request.
- Portfolio analytics, backtesting, trade ideas beyond the preview.

---

## 10. Definition of done (v1)
1. Type any of the ~16 grounded tickers → correct, sector-voiced, fully-sourced
   3-slide deck + provenance zip, in one click.
2. Type an ungrounded universe ticker → valid deck with consensus/price/peers and
   an honest "thin grounding" flag.
3. A non-bank deck contains **zero** bank vocabulary.
4. Peer table caps all in USD with a correct average.
5. Data-freshness and grounding-currency are shown truthfully.
6. Numeric pipeline passes an offline test suite against committed fixtures.
7. Deploys to the chosen cloud host and works for a live, uncached ticker
   (i.e. the Cloudflare problem is actually solved, not just locally faked).

---

*Treat §3, §4, and §6 as the contract. Everything else is guidance. Build it
clean.*
