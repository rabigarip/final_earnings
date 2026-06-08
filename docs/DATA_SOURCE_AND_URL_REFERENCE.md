# Earnings Preview Agent — Data Source & URL Path Reference

Target: Al Rajhi Bank (1120.SR) as example; generalize with `{SLUG}` and `{TICKER}`.

---

## MARKETSCREENER (PRIMARY SOURCE)

**URL structure:** Base `https://www.marketscreener.com/quote/stock/{SLUG}/`

The **{SLUG}** is a URL-safe company name + numeric ID (e.g. `AL-RAJHI-BANKING-AND-INVE-6497957`).

**Discovery:** Search first to find the slug if unknown:
- Search URL: `https://www.marketscreener.com/search/?q={TICKER}` (e.g. `?q=1120`)
- Extract the slug from the redirect or first result link.
- In this project, the slug is stored in `data/company_master.json` as `marketscreener_id`.

---

### Page 1: Summary (quote snapshot, consensus box)

| URL | Data |
|-----|------|
| `/{SLUG}/` | Capitalization, EV, Free Float %; P/E 2026*/2027*; EV/Sales; Yield; Net sales/Net income (annual consensus); **Analysts' Consensus box**: Mean consensus, Number of Analysts, Last Close, Average target, Spread %, High/Low target. |

**Memo fields:** Header (price, consensus, target, spread); valuation snapshot; annual consensus FY26E/FY27E.

---

### Page 2: Consensus / Analysts' opinion

| URL | Data |
|-----|------|
| `/{SLUG}/consensus/` | Evolution of Average Target chart; **Analyst Consensus Detail** (Buy/Outperform/Hold/Underperform/Sell counts); EPS Estimates chart; **Quarterly revenue – Rate of surprise** (actual vs estimate, beat/miss); Consensus revision (18m); Analyst recommendations; List of covering analysts. |

**Memo fields:** Consensus detail (rating split); revenue surprise history; analyst coverage; EPS trajectory.

---

### Page 3: Estimates revisions

| URL | Data |
|-----|------|
| `/{SLUG}/revisions/` | EPS and revenue revision trends (1M, 3M, 6M, 12M). **Note:** This project uses `consensus-revisions` for estimate tables; `/revisions/` may differ. |

**Memo fields:** Estimate momentum commentary.

---

### Page 4: Financials (income statement, annual + quarterly)

| URL | Data |
|-----|------|
| `/{SLUG}/finances/` | **Annual:** Net sales, EBIT, Net income, Change %, Announcement dates; **Items per share:** EPS, DPS, BVPS, Cash flow per share, Shares outstanding. **Quarterly (toggle or same page):** Net sales per quarter (Q1–Q4 26E), EBIT, Net income (often N/A for banks), Announcement dates. |

**Memo fields:** FY reported results; annual consensus (EPS, DPS, NI, Sales); **Q1 consensus revenue** (from quarterly table); Q1 net income often **derived** for banks (label as such).

**Quarterly tab:** May require `?type=trimestral` or Playwright to click "Quarterly". This codebase parses both annual and quarterly tables from the same `/finances/` HTML when present.

---

### Page 5: Valuation

| URL | Data |
|-----|------|
| `/{SLUG}/valuation/` | Full valuation table by year (2024A–2028E): P/E, P/B, PEG, EV/Revenue, EV/EBIT, EV/EBITDA, EV/FCF, FCF Yield, DPS, Yield, EPS. P/E and Yield evolution charts (5Y average). |

**Memo fields:** Consensus multiples table (P/E, P/B, EV/EBIT, Yield, PEG); 5Y avg P/E.

**This project:** Uses `/valuation-dividend/` for EPS, DPS, yield, distribution rate. Full `/valuation/` multiples table is not yet scraped.

---

### Page 6: Company profile

| URL | Data |
|-----|------|
| `/{SLUG}/company/` | Business description, sector, industry, executives, shareholders, country, exchange. |

**Memo fields:** Header (region, sector); company description.

---

### Page 7: Calendar

| URL | Data |
|-----|------|
| `/{SLUG}/calendar/` | Next earnings date (e.g. "04-29 - Q1 2026 Earnings Release"); dividend dates; AGM dates. |

**Memo fields:** "Expected Report: 29 April 2026" in header.

**This project:** `src/providers/marketscreener_pages.py` → `fetch_calendar_events()`.

---

## YAHOO FINANCE (FALLBACK)

Saudi ticker format: `1120.SR`. US: `AAPL`, `MSFT`, etc.

**Library:** `yfinance` → `yf.Ticker("1120.SR")`.

| Endpoint | Key fields | Memo use |
|----------|------------|----------|
| `stock.info` | currentPrice, previousClose, marketCap, trailingPE, forwardPE, dividendYield, targetMeanPrice, targetHighPrice, targetLowPrice, recommendationKey, numberOfAnalystOpinions, earningsDate, sector, industry | Price, target, 52wk, mcap, sector; valuation snapshot |
| `stock.financials` / `stock.quarterly_financials` | Total Revenue, Net Income, EBIT, Basic EPS | FY/quarterly actuals |
| `stock.earnings_dates` | Past/future earnings dates, EPS estimate/actual | Expected report date; EPS surprise |
| `stock.recommendations` / `stock.analyst_price_targets` | Analyst actions, target prices | Coverage, consensus |

**Limitations for Saudi (1120.SR):** earnings_dates may be sparse; no consensus EPS/revenue estimates; no forward valuation grid. Hence MarketScreener is primary.

---

## SCRAPING STRATEGY (RECOMMENDED ORDER)

1. **Resolve slug** — Search `https://www.marketscreener.com/search/?q={TICKER}` or use `company_master.json`.
2. **Summary** — `GET /{SLUG}/` → consensus box, valuation snapshot.
3. **Consensus** — `GET /{SLUG}/consensus/` → analyst detail, surprise, brokers.
4. **Financials (annual)** — `GET /{SLUG}/finances/` → income statement, per-share table.
5. **Financials (quarterly)** — Same page or `?type=trimestral` / Playwright toggle → Q1 consensus revenue.
6. **Valuation** — `GET /{SLUG}/valuation/` → multiples by year.
7. **Calendar** — `GET /{SLUG}/calendar/` → next earnings date.
8. **Fallback** — If MarketScreener fails, use `yfinance` for price, financials, earnings_dates.

---

## FIELD MAPPING: MEMO → SOURCE → PATH

| Memo field | Primary (MarketScreener) | Fallback (yfinance) |
|------------|---------------------------|----------------------|
| Company name | /{SLUG}/ page title | stock.info["shortName"] |
| Ticker | /{SLUG}/ page title | stock.info["symbol"] |
| Sector / Industry | /{SLUG}/company/ | stock.info["sector"/"industry"] |
| Last close | /{SLUG}/ consensus box | stock.info["previousClose"] |
| Market cap | /{SLUG}/ valuation grid | stock.info["marketCap"] |
| Mean consensus | /{SLUG}/ consensus box | stock.info["recommendationKey"] |
| Number of analysts | /{SLUG}/ consensus box | stock.info["numberOfAnalystOpinions"] |
| Avg / High / Low target | /{SLUG}/ consensus box | stock.info["targetMeanPrice"] etc. |
| Spread to target | /{SLUG}/ consensus box | (target - price) / price |
| Buy/Hold/Sell split | /{SLUG}/consensus/ detail | stock.recommendations |
| Covering brokers | /{SLUG}/consensus/ | N/A |
| Next earnings date | /{SLUG}/calendar/ | stock.earnings_dates |
| Revenue surprise history | /{SLUG}/consensus/ surprise chart | stock.earnings_dates (EPS only) |
| Consensus Q1 revenue | /{SLUG}/finances/ quarterly table | N/A |
| FY Net sales / NI (A+E) | /{SLUG}/finances/ | stock.financials |
| EPS / DPS (A+E) | /{SLUG}/finances/ or /valuation-dividend/ | stock.info, financials |
| P/E, P/B, EV/EBIT, Yield | /{SLUG}/valuation/ | stock.info (partial) |
| Q4/Q1 actuals | /{SLUG}/finances/ quarterly | stock.quarterly_financials |
| Guidance / Investment view / Risks | Not on MarketScreener | N/A (editorial or LLM) |

---

## IMPLEMENTATION NOTES

1. **Management guidance** — Not available from scraping. Options: manual input, earnings-call transcript scrape, or LLM extraction from transcript.
2. **Q1 net income consensus** — Often missing for banks. Derive (e.g. FY26E NI / 4, seasonality-adjusted) and **label as "Derived"** in the memo.
3. **Investment view / Risks** — Editorial or LLM from scraped data; keep template bullets if no LLM.
4. **Rate limiting** — Use delays (2–3 s), rotate user agents, cache responses (see `config/settings.toml`).
5. **Quarterly tab** — Try `?type=trimestral` or `?fperiod=quarterly`; else Playwright for JS toggle.
6. **Locale** — Use `www.marketscreener.com`; regional subdomains (sa., uk.) contain same data.

---

## PROJECT ALIGNMENT

| Reference page | Current implementation |
|----------------|------------------------|
| /{SLUG}/ | fetch_summary_page() — consensus box + valuation snapshot; fallback when /consensus/ fails. |
| /{SLUG}/consensus/ | `marketscreener_consensus.py` + `marketscreener_pages.fetch_consensus_summary()`. |
| /{SLUG}/finances/ | `fetch_financial_forecast_series()` (annual + quarterly; tries `?type=trimestral` if quarterly missing). |
| /{SLUG}/valuation-dividend/ | `marketscreener_pages.fetch_dividend_eps_page()`. |
| /{SLUG}/valuation/ | `fetch_valuation_multiples()` — P/E, P/B, PEG, EV/Revenue, EV/EBIT, Yield by year. |
| /{SLUG}/finances-income-statement/ | `marketscreener_pages.fetch_income_statement_actuals()`. |
| /{SLUG}/calendar/ | `marketscreener_pages.fetch_calendar_events()`. |
| Slug discovery | `company_master.json` or `resolve_slug_from_search(ticker)` when ID missing. Rate limit: `min_delay_seconds` between requests. |
