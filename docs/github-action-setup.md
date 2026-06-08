# Daily cache refresh workflows (GitHub Actions)

Two workflows ship in this repo, both solving the same problem for two
different Cloudflare-blocked sources. Pick the one you're setting up:

* **Investing.com** — `.github/workflows/refresh-investing-cache.yml`
  (script: `scripts/refresh_investing_cache.py`). Snapshots write to
  `data/investing/<slug>__<kind>.json`. Setup instructions below.
* **MarketScreener** — `.github/workflows/refresh-marketscreener-cache.yml`
  (script: `scripts/refresh_marketscreener_cache.py`). Snapshots write
  to `data/marketscreener/ms_<safe-cache-slug>.html`. **See the
  "MarketScreener-specific notes" section at the bottom for what's
  different.**

The two workflows can run independently (different times, different
tickers, etc.) — they don't share state.

---

# Set up the daily Investing.com cache refresh (GitHub Actions)

## Why this exists

Cloudflare blocks Render's egress IPs from reaching Investing.com.
The deployed runtime can't fetch live data — it reads snapshots from
`data/investing/<slug>__<kind>.json`. Those snapshots stay fresh only
if someone re-runs the fetch periodically.

GitHub Actions runners use residential-ish IP ranges that aren't
currently Cloudflare-flagged for Investing.com. The workflow below
runs the refresh daily, commits the diff, and pushes — Render
auto-redeploys with the new data within ~5 minutes.

## One-time setup

### Step 1 — Grant the workflow permission to push

GitHub Actions can push to the repo by default, but `contents: write`
must be enabled on the workflow (already declared in the YAML) AND
the repo settings must allow it.

1. Repo → Settings → Actions → General
2. Under **Workflow permissions**, select **Read and write
   permissions**
3. Save

### Step 2 — Add the workflow file

Either:

**Option A (CLI):** copy `/tmp/refresh-investing-cache.yml` from
the local checkout into `.github/workflows/refresh-investing-cache.yml`,
commit, and push. Requires a Personal Access Token with `workflow`
scope.

**Option B (GitHub UI):**
1. Repo → Actions → "New workflow" → "Set up a workflow yourself"
2. Name the file `refresh-investing-cache.yml`
3. Paste the YAML below
4. Commit directly to `main`

### Step 3 — Verify

Repo → Actions → "Refresh Investing.com cache" → "Run workflow"
(branch: main). The first run should succeed in ~2-3 minutes and
commit any diff to `data/investing/`. Render redeploys automatically.

## The YAML

```yaml
name: Refresh Investing.com cache

on:
  schedule:
    - cron: "30 4 * * *"   # 04:30 UTC daily — ahead of MENA market open
  workflow_dispatch:

permissions:
  contents: write

concurrency:
  group: refresh-investing
  cancel-in-progress: true

jobs:
  refresh:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - name: Checkout main
        uses: actions/checkout@v4
        with:
          ref: main
          fetch-depth: 1

      - name: Set up Python 3.12
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install "curl_cffi>=0.7" requests

      - name: Run Investing.com refresh
        run: python -m scripts.refresh_investing_cache --delay 4

      - name: Commit and push if changed
        run: |
          if [ -z "$(git status --porcelain data/investing/)" ]; then
            echo "No snapshot changes — nothing to commit."
            exit 0
          fi
          git config user.name  "investing-cache-bot"
          git config user.email "investing-cache-bot@users.noreply.github.com"
          git add data/investing/
          git commit -m "chore(cache): daily Investing.com snapshot refresh"
          git push origin main
```

## Failure modes

- **GHA runner gets Cloudflare-blocked**: workflow exits 0 with no
  diff; existing snapshots stay live. Need to switch to a paid
  residential-IP proxy (ScrapingBee, Bright Data) — see the
  `scripts/refresh_investing_cache.py` script for the integration
  point.
- **A new ticker has no slug**: workflow logs "[skip] TICKER: no slug
  in _SLUGS — add it first". Edit `src/providers/probe_investing.py`
  to add the slug, then re-run.
- **Push rejected by branch protection**: the workflow uses
  `GITHUB_TOKEN` which bypasses branch protection for write actions
  configured under repo settings. If your branch protection blocks
  bot pushes, either grant `contents: write` to the workflow's token
  via repo settings, or switch the push to a dedicated PAT stored in
  `secrets.CACHE_PUSH_TOKEN`.

## Cost

Free. The job runs ~2 minutes/day on a public runner, well under the
GitHub Actions free-tier budget (3,000 minutes/month on private repos;
unlimited on public).

---

# MarketScreener-specific notes

The MS workflow follows the exact same shape as the Investing one above
— same `contents: write`, same daily cron, same commit-and-push. The
differences worth knowing:

## What it produces

Raw HTML files, one per ticker × page kind. For BKMB.OM you get a
dozen files like:

```
data/marketscreener/
├── ms_BKMB.OM_summary.html
├── ms_BKMB.OM_consensus.html
├── ms_BKMB.OM_finances.html       ← annual + quarterly forecasts
├── ms_BKMB.OM_valuation.html      ← P/E history, P/B, yield by year
├── ms_BKMB.OM_calendar.html
├── ms_BKMB.OM_ratings.html
├── ms_BKMB.OM_sector.html
├── ms_BKMB.OM_perf.html
├── ms_BKMB.OM_recommendations.html
├── ms_BKMB.OM_income_statement.html
├── ms_BKMB.OM_dividend_eps.html
└── ms_BKMB.OM_quarterly_results.html
```

These are **raw HTML**, not parsed JSON — the production parsers read
them via BeautifulSoup, same as a live fetch. That keeps the parser
logic single-pathed (no separate parse-on-refresh + serialize-to-JSON
step).

Filesize: ~150-400 KB per page, ~3 MB per ticker, ~30 MB for the
full 10-ticker panel. Repository size grows with each refresh but
git compresses the diffs well in practice (most of the page is the
same nav/footer markup).

## How Render picks the snapshots up

The runtime code in `src/providers/marketscreener_pages.py::_fetch_page`
always tries live first. If live returns HTTP 403, captcha, or a
network error, it then checks `data/marketscreener/ms_<safe>.html` and
parses that instead. The fallback path is **always on** — no env var
to set — because Render is the canonical user and serving stale data
is better than serving nothing.

You can confirm the fallback is firing by looking at the pipeline
logs: any step that uses MS data shows
`"(served from data/marketscreener/ snapshot)"` in its `errors` list
(it's listed under errors only because the live fetch failed —
the page still rendered successfully).

## Tickers covered

The refresh script reads `data/company_master.json` and refreshes every
ticker that has a non-empty `marketscreener_id`. Currently 17 tickers,
including the 10 production-panel names plus a few adjacents
(`BABA`, `7202.SR`, etc.). To restrict to just the panel:

```bash
python -m scripts.refresh_marketscreener_cache --panel
```

To add a new ticker: open `data/company_master.json`, find the row for
the ticker, set `"marketscreener_id"` to the slug from the company's
MS URL (the segment between `/quote/stock/` and `/`), commit, then run
the workflow manually from the Actions tab.

## Workflow trigger

Cron runs at **04:50 UTC** — 20 minutes after the Investing.com
refresh so both finish before the 05:30 panel pre-render Render cron.

You can also trigger it manually from the GitHub Actions UI with
optional inputs:

- `tickers` (string) — comma-separated, e.g. `BKMB.OM,OQEP.OM`
- `panel_only` (bool) — restrict to the 10-ticker panel

## Failure modes

- **GHA runner gets Cloudflare-blocked on MS too**: the script will
  log "no pages came back successfully" and exit code 2. Switch to
  a paid residential-IP proxy (ScrapingBee, Bright Data) in
  `scripts/refresh_marketscreener_cache.py::_refresh_ticker` — same
  integration point as Investing.com's fallback.
- **MarketScreener parser breaks after a site redesign**: the workflow
  still writes the raw HTML successfully but the downstream parsing
  may fail. Catch this by running `pytest tests/test_marketscreener_*`
  against the fresh snapshots locally before merging.
- **Repo size growth**: each refresh commits ~30 MB of HTML diffs. If
  this becomes a concern, switch the snapshot from HTML to extracted
  JSON (a one-day refactor — parse on the GHA runner instead of on
  Render).

## Cost

Free. The job runs ~10 minutes/day on a public runner (3-second delay
between fetches × 12 pages × 17 tickers ≈ 600 seconds plus
parse/commit overhead), well under the GitHub Actions free-tier
budget (3,000 minutes/month on private repos; unlimited on public).
