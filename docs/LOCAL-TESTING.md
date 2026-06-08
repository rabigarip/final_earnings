# Local testing — quality and pipeline

Use this to test report quality and pipeline behaviour locally before deploying.

---

## 1. Clean environment (optional but recommended)

Start from a clean state so outputs and DB are predictable:

```bash
# From repo root
rm -rf cache/* outputs/*

# Re-init DB from company_master.json (ensures sector/industry etc. are current)
python -m src.main --init-db
```

`cache/` and `outputs/` are gitignored; deleting them does not affect the repo.

---

## 2. Run the pipeline (skip LLM for speed)

Faster runs without Gemini (uses sector fallback for Investment View):

```bash
# Single ticker
python -m src.main --ticker 2222.SR --mode preview --skip-llm

# A few sector coverage tickers
python -m src.main --ticker 2222.SR --mode preview --skip-llm   # Oil & gas
python -m src.main --ticker 1322.SR --mode preview --skip-llm   # Mining
python -m src.main --ticker 2010.SR --mode preview --skip-llm   # Materials/Chemicals
python -m src.main --ticker 000063.SZ --mode preview --skip-llm # Technology / Communication
```

With LLM (needs `GEMINI_API_KEY` in `.env`):

```bash
python -m src.main --ticker 2222.SR --mode preview
```

---

## 3. Where to find outputs

- **Memos:** `outputs/` (or `REPORT_OUTPUT_DIR` if set). Each run writes a `.docx` (e.g. `{ticker}_preview_*.docx`).
- **DB:** `data/earnings.db` (or `DATABASE_PATH`). Run history and `memo_path` are in `pipeline_runs`.

---

## 4. Quality checklist (Investment View fallback)

When using `--skip-llm`, the memo’s Investment View comes from the **sector fallback**. Confirm:

| Ticker   | Sector / type        | Paragraph 2 should mention |
|----------|----------------------|----------------------------|
| 2222.SR  | Oil & gas            | production volumes, realized prices, lifting costs, capex |
| 1322.SR  | Mining / metals      | production, commodity prices, costs |
| 2010.SR  | Basic Materials      | volume, realized price, utilization, feedstock |
| 1120.SR  | Bank                 | NIM, loan growth, asset quality |
| 000063.SZ| Technology / comms   | revenue mix, margins, guidance, product cycles |

- Memo should include the line **"Generated with earnings-research v0.1.0"** (or current version from `config/settings.toml`).
- No instruction-style text (e.g. no "Focus on...", "Do not use...").

---

## 5. Run the test suite

```bash
pytest tests/ -v
```

Some tests need Yahoo/MarketScreener (e.g. `test_payload_isolation`, `test_smoke`). If a ticker is not found or the network is unavailable, those tests may fail; that is expected when offline or when the provider returns no data.

---

## 6. Local API (optional)

```bash
uvicorn src.api:app --reload --port 8000
```

Then: `http://localhost:8000/docs`, `http://localhost:8000/health` (returns `version`), and create reports via the API or the frontend (if built into `static/`).
