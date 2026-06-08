# Bloomberg consensus upload

The Bloomberg-sourced consensus replaces Investing.com / yfinance forward estimates as the canonical source for analyst-driven fields. Once uploaded, the data flows through `canonical_store` exactly like any other provider — the deck's slide 2 footnote flips from "Consensus: Investing" to "Consensus: Bloomberg" and confidence tiers move up.

## Why this exists

The Stage-1 / Stage-2 free-source stack gave us:

| Free source | Coverage | Reliability |
|---|---|---|
| Investing.com | 10/10 panel for next-Q + target + rating | Cloudflare-gated, occasional regex drift |
| yfinance `earnings_estimate` | 8/10 for FY1/FY2 | None for Oman (`.OM` not on Yahoo) |
| MarketScreener | 10/10 for FY estimates | Slow + entity-mapping issues (ADCB ↔ Aldar) |

For an institutional deck, **Bloomberg's BEST consensus is the right primary source**. This path lets an analyst export from BBG once, upload, and let the deck pipeline use it.

## File format

**Long-form CSV / Excel** — one row per (ticker, period, metric). Columns:

| Column | Required | Example |
|---|---|---|
| `ticker` | yes | `2222.SR` (matches `company_master.ticker`) |
| `period_type` | yes | `ANNUAL`, `QUARTERLY`, `TARGET`, `RATING` |
| `period_label` | yes when applicable | `FY2026`, `FY2027`, `Q2_2026`, blank for TARGET/RATING |
| `metric` | yes | `EPS`, `REVENUE`, `EBITDA`, `NET_INCOME`, `TARGET_PRICE`, `RATING`, `DIVIDEND_PER_SHARE` |
| `mean` | yes | `1.85` |
| `low` | optional | `1.55` |
| `high` | optional | `2.20` |
| `median` | optional | `1.83` |
| `std_dev` | optional | `0.19` |
| `num_estimates` | optional | `18` |
| `buy_count` | only when metric=`RATING` | `9` |
| `hold_count` | only when metric=`RATING` | `9` |
| `sell_count` | only when metric=`RATING` | `0` |
| `as_of_date` | optional | `2026-05-01` |
| `currency` | optional | `SAR`, `USD`, `HKD` |

Empty cells are fine — only `ticker`, `period_type`, `metric`, `mean` are mandatory.

### Sample (one ticker × all metrics)

```csv
ticker,period_type,period_label,metric,mean,low,high,num_estimates,as_of_date,currency
2222.SR,ANNUAL,FY2026,EPS,1.85,1.55,2.20,18,2026-05-01,SAR
2222.SR,ANNUAL,FY2027,EPS,1.92,1.45,2.35,16,2026-05-01,SAR
2222.SR,QUARTERLY,Q2_2026,EPS,0.48,0.41,0.55,12,2026-05-01,SAR
2222.SR,ANNUAL,FY2026,REVENUE,1820000000000,1700000000000,1950000000000,16,2026-05-01,SAR
2222.SR,QUARTERLY,Q2_2026,REVENUE,460000000000,420000000000,490000000000,12,2026-05-01,SAR
2222.SR,TARGET,,TARGET_PRICE,32.50,27.00,38.00,18,2026-05-01,SAR
2222.SR,RATING,,RATING,2.5,,,18,2026-05-01,
```

For the RATING row, also include `buy_count`, `hold_count`, `sell_count`:
```csv
2222.SR,RATING,,RATING,2.5,,,18,2026-05-01,,9,9,0
```

### Bloomberg field mapping (typical BEST exports)

| Bloomberg field | Maps to (period_type, metric) |
|---|---|
| `BEST_EPS` (BF1, BF2, current Q) | ANNUAL/QUARTERLY, EPS |
| `BEST_SALES` | ANNUAL/QUARTERLY, REVENUE |
| `BEST_EBITDA` | ANNUAL/QUARTERLY, EBITDA |
| `BEST_NET_INCOME` | ANNUAL/QUARTERLY, NET_INCOME |
| `BEST_TARGET_PRICE` | TARGET, TARGET_PRICE |
| `EQY_REC_CONS` + `TOT_BUY_REC` / `TOT_HOLD_REC` / `TOT_SELL_REC` | RATING, RATING |
| `BEST_DPS` | ANNUAL, DIVIDEND_PER_SHARE |

## Two upload paths

### Path A — CLI / file drop (local workflow)

1. Save the CSV to `data/bloomberg/consensus.csv`.
2. Run `python -m scripts.daily_refresh --cadence=weekly --only bloomberg --tickers <TICKER>` (or `--tickers` blank for all in file).
3. Re-render decks: `python -m scripts.render_panel_decks --skip-refresh --tickers <TICKER>`.

### Path B — Web upload (hosted)

1. Visit the landing page → click **Upload Bloomberg consensus** card.
2. Drag-and-drop a CSV or XLSX (same schema as above).
3. Server parses, writes per-ticker observations into `coverage_observations` under `provider='bloomberg'`, runs reconcile + re-renders the affected ticker(s).
4. UI shows: **N tickers updated**, with links to the regenerated decks.

## Trust ladder placement

`bloomberg` ranks **above** all free sources for forward fields:

```
ir_pdf > bloomberg > investing > marketscreener > yahoo
```

For non-forward fields (current price, market cap, balance sheet), exchange providers and IR PDFs still win.

## Coverage / observability

`/api/jabal/{ticker}` returns a `sources` array. After a Bloomberg upload for that ticker, `bloomberg` appears in the array and the slide 2 footnote reads:

> Next print: 14 Aug 2026 (period Q2 2026)  ·  Consensus: Bloomberg  ·  18 analysts covering

If the upload covers only some metrics, mixed-source decks are fine — slide 2 EPS rows might be Bloomberg-canonical while the dividend yield stays MS-canonical.
