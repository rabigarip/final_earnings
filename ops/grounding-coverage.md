# Grounding coverage — operations runbook

Goal: every ticker an analyst types gets a **grounded** deck (real FY actuals
driving slide 2 + commentary), not a thin one. This is the scale loop around
the auto-grounding extractor.

## The funnel (run the cockpit to see live numbers)

```
python3 scripts/coverage_report.py        # summary + queues
python3 scripts/coverage_report.py --full # every non-grounded ticker
```

Each registry ticker is in one stage:

| Stage | Meaning | Action |
|-------|---------|--------|
| GROUNDED | `data/disclosed/<t>.json` exists (verified actuals) | done |
| PROMOTABLE | staging candidate passes the promotion gate | `promote_grounding.py <t>` |
| REVIEW | staging candidate blocked by the gate (reason shown) | human fixes period / fills IR |
| EXTRACT | has an MS snapshot, not yet extracted | `extract_grounding.py --all-cached` |
| BARE | **no MS snapshot** | refresh snapshots (see bottleneck) |

## The loop

```
# 1. extract every cached ticker into data/disclosed/_staging/ (gitignored)
python3 scripts/extract_grounding.py --all-cached

# 2. see what's ready / blocked
python3 scripts/coverage_report.py

# 3a. promote a reviewed candidate (you web-checked the headline vs IR)
python3 scripts/promote_grounding.py 2010.SR EMAAR.AE --reviewed

# 3b. OR batch-promote everything that passes the gate (reviewed=false:
#     MS-sourced, gate-validated, NOT yet IR-verified — clearly labeled)
python3 scripts/promote_grounding.py --auto --dry-run   # preview first
python3 scripts/promote_grounding.py --auto

# 4. commit the new groundings
git add data/disclosed/*.json && git commit && git push
```

## Trust model

- The extractor is **deterministic** (no LLM) and **sanity-gated**; eval shows
  **~90% field agreement with hand-verified gold on December-FYE tickers**
  (`scripts/eval_grounding_extractor.py`).
- The **promotion gate** enforces: status=auto_unverified, confidence=high
  (December FYE), all values in sanity bounds, and **period currency** — the
  anchor FY must equal what the reporting calendar says is the latest expected
  filing. This automatically rejects stale snapshots (last year's column),
  forward-estimate anchors, and non-December-FY label offsets.
- The gate validates *shape, period, bounds* — NOT that the value equals the
  IR headline. MS line items can diverge (e.g. a stale snapshot showed Aramco
  FY2025 net income 350bn vs the actual 392.5bn). So:
  - `--reviewed` promotions are marked `_provenance.reviewed=true` (you checked).
  - `--auto` promotions are marked `reviewed=false` with a note — strictly
    better than a thin deck, but should get a human pass. The deck's
    provenance.xlsx + scorecard surface the unverified status.

## The bottleneck: snapshot (slug) coverage — the #1 scale lever

`coverage_report.py` shows most of the 506-ticker universe as **BARE**. Reason:
MarketScreener is Cloudflare-blocked from Render, so the runtime reads
committed snapshots in `data/marketscreener/`, refreshed by the GHA
`refresh-marketscreener-cache.yml`. **That refresh only covers tickers with a
curated `marketscreener_id` in `data/company_master.json` (~17).** Registry
tickers without a curated MS slug can't be snapshotted, so they can't be
extracted or grounded.

To scale grounding to the universe, in priority order:

1. **Widen MS slug coverage.** Add `marketscreener_id` (+ `isin`) for more
   registry tickers to `company_master.json`, or teach
   `scripts/refresh_marketscreener_cache.py` to resolve slugs at refresh time
   via the runtime resolver in `src/services/fetch_marketscreener_pages.py`
   (ISIN → search). This is the gating step — everything downstream is ready.
2. **Refresh only the grounding pages** (`finances-income-statement`,
   `finances`, `valuation-dividend`) for the universe — 3 pages, not 10 — and
   **shard** across scheduled runs to stay under the 45-min GHA timeout.
3. **Auto-grow grounding** by chaining extract + `--auto` promote after the
   refresh (see `ops/grow-grounding.workflow.yml`).

## Verify a single ticker end to end

```
python3 scripts/extract_grounding.py <TICKER>          # -> staging
cat data/disclosed/_staging/<TICKER>.json              # check provenance/needs_ir
python3 scripts/promote_grounding.py <TICKER> --reviewed
python3 -c "from src.services.deck_scorecard import score_ticker as s; print(s('<TICKER>'))"
```
