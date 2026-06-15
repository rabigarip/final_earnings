# Deploy to Render — click-by-click runbook

The blueprint now runs **LLM-on** (`DISABLE_LLM=0`): Gemini writes the analyst
prose (investment thesis, catalysts, risks, what-to-watch) with sector-specific
playbooks grounded in the numbers. **This requires a `GEMINI_API_KEY`** — set it
in the Render dashboard (step 4 below). If you skip the key, the deck still
generates: it degrades gracefully to the deterministic, sector-voiced template
prose and never blocks on the LLM. Everything else the deck needs — Yahoo
(price/consensus/estimates/charts), Investing/MarketScreener (committed-snapshot
fallback), grounding, peers — is in the repo and needs no key.

## 1. One-time: connect the repo
You already have the code at `github.com/rabigarip/final_earnings`. Make sure
you can sign in to Render with that GitHub account.

## 2. Deploy with the Blueprint (~1 min of clicking, ~5–10 min build)
1. Go to **https://dashboard.render.com** → **New +** → **Blueprint**.
2. Pick the **final_earnings** repo → Render reads `render.yaml` and shows one
   service: **final-earnings-api** (a Python web service).
3. It defaults to the **Starter** plan ($7/mo). Starter is recommended — a full
   numbers-deck takes ~30–70s and Render's *free* tier hard-caps requests at 30s.
   (You *can* pick Free to try it, but expect occasional 502s on slower tickers.)
4. **Env vars:** `DISABLE_LLM=0`, `REFRESH_ON_RENDER=1`, `MS_USE_CURL_CFFI=1`,
   and the `/tmp` paths are all preset. **Set `GEMINI_API_KEY`** to your key so
   the deck gets the Gemini analyst prose (Environment tab → `GEMINI_API_KEY`).
   A free-tier key works: https://aistudio.google.com/app/apikey → *Create API
   key in new project*. (Leave it blank to ship template prose instead — the
   deck still generates fine.)
5. Click **Apply / Create**. Watch the build log: it runs `pip install` then
   uses the committed `static/` frontend. First build is a few minutes.

## 3. Verify it's live
When the service shows **Live**, open its URL (e.g.
`https://final-earnings-api.onrender.com`):
- `GET /health` → `{"status":"ok"}`
- The home page loads the search UI.
- Type a ticker (e.g. **1180.SR** or **BKMB.OM**) → Generate → download the `.zip`
  (a `.pptx` + `.provenance.xlsx`). First request may be slower (cold start +
  on-demand refresh).

**Send me the URL** and I'll run the full flow against it (health → autocomplete
→ generate → bundle) and confirm Cloudflare/curl_cffi behaviour from Render's IP.

## 4. (Optional) Turn the LLM back OFF
The blueprint ships LLM-on. To run fully number-first with zero LLM cost:
Render → your service → **Environment** → set `DISABLE_LLM` = `1` → Save. The
deck then uses the deterministic, sector-voiced template prose (no Gemini call).

## 5. (Optional) Keep fallback snapshots fresh
The `.github/workflows/` refresh jobs keep the Investing/MarketScreener
snapshots current from non-blocked IPs. Enable them once: GitHub repo →
**Settings → Actions → General** → *Allow all actions* **and** *Workflow
permissions → Read and write*.

## Notes
- The runtime DB lives in `/tmp` and is re-seeded from `data/company_master.json`
  on every boot (Render's project dir is read-only) — expected and handled.
- Investing/MarketScreener may be Cloudflare-403'd from Render's datacenter IP;
  the deck falls back to the committed snapshots + the Yahoo backbone, so it
  still fills. That's exactly the path I want to confirm live.
