# Earnings Research — note for the research team (pilot)

**What it does.** On the **Generate** tab, type any ticker (e.g. `1180.SR`,
`BKMB.OM`, `2222.SR`, `0700.HK`) → in ~30–90s you get a 3-slide PowerPoint
(Snapshot · Thesis & Expectations · Valuation & Positioning) plus an Excel
provenance file showing where every number came from. Headline numbers come
from Yahoo Finance live, with Investing.com / MarketScreener as fallback; the
analyst prose (investment thesis, catalysts, key risks, what-to-watch) is
written by Gemini, grounded in those numbers and sector-aware (a bank reads
like a bank, an oil & gas name like one). **Dashboard** shows upcoming
earnings (next 14 days), recent decks, and the universe by market cap;
**Calendar** shows reporting dates by market and sector.

**Please flag to us:** any wrong number, any sector-mismatched language in the
prose, or any ticker that errors **twice in a row**.

**Known limitations (expected, not bugs):**

- **Transient "retrying…":** the first click after an idle period or right
  after a deploy may briefly retry — it recovers on its own. If a generate ever
  errors, just click **Generate** again.
- **Gulf history charts:** Yahoo-blind Gulf names (Oman `.OM`, some `.BH`/`.QA`)
  produce full prose and headline numbers, but their slide-3 *history charts*
  can be blank — that time-series data isn't reachable from the server's IP yet.
  (Fast-follow fix planned.)
- **Calendar coverage:** reporting dates are most complete for India, China and
  Saudi names; Gulf/Oman dates are sparse (data-provider limitation), so the
  Calendar/Dashboard may look empty when filtered to those markets.
- **Heavy batch use:** generating many decks back-to-back can briefly hit the
  Gemini rate limit; those decks fall back to deterministic template prose.
  Spacing requests out avoids it.
