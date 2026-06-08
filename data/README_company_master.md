# Bulk-adding companies to company_master.json

To add many companies (e.g. 100–1500+), use a **CSV file** and the seed script instead of editing JSON by hand.

## 1. Prepare a CSV

Create a CSV with a header row. Column names are case-sensitive.

**Required columns** (every row must have these):

| Column          | Example   | Description                    |
|-----------------|-----------|--------------------------------|
| ticker          | 2222.SR   | Yahoo Finance ticker          |
| company_name    | Saudi Aramco | Short name                 |
| exchange        | Tadawul   | Exchange name                  |
| country         | SA        | ISO country (e.g. SA, US)      |
| currency        | SAR       | Trading currency               |

**Optional columns** (can be empty):

| Column             | Example                    | Description                    |
|--------------------|----------------------------|--------------------------------|
| company_name_long  | Saudi Arabian Oil Company  | Full legal name                |
| isin               | SA14TG012N13               | **Recommended** – used for MS resolution |
| marketscreener_id  | ARAMCO-103505448           | MarketScreener slug (or leave blank) |
| zawya_slug         |                            | Zawya identifier               |
| sector             | Energy                     | Sector                         |
| industry           | Oil & Gas Integrated       | Industry                       |
| is_bank            | 0                          | 1/0 or true/false             |
| notes              |                            | Free text                      |

You can export this from Excel, a database, or a data provider (Bloomberg, Refinitiv, exchange list, etc.). Save as UTF-8 CSV.

**Example `new_companies.csv`:**

```csv
ticker,company_name,exchange,country,currency,isin,company_name_long,marketscreener_id,zawya_slug,sector,industry,is_bank,notes
2222.SR,Saudi Aramco,Tadawul,SA,SAR,SA14TG012N13,Saudi Arabian Oil Company,ARAMCO-103505448,,Energy,Oil & Gas Integrated,0,
1180.SR,SNB Bank,Tadawul,SA,SAR,SA0007879105,Saudi National Bank,,,,,1,
```

## 2. Run the seed script

From the project root:

```bash
# Merge CSV into existing company_master.json (updates existing tickers, adds new)
python3 -m scripts.seed_company_master data/new_companies.csv

# See what would change without writing (dry run)
python3 -m scripts.seed_company_master data/new_companies.csv --dry-run

# Only add new tickers; never overwrite existing rows
python3 -m scripts.seed_company_master data/new_companies.csv --append-only

# Write to a different file (e.g. to review before replacing master)
python3 -m scripts.seed_company_master data/new_companies.csv -o data/company_master_merged.json
```

## 3. Re-seed the database

After updating `company_master.json`, re-seed the SQLite DB so the pipeline sees the new companies:

```bash
python3 -m src.main --init-db
```

## Tips for 1500+ companies

1. **ISIN** – Include ISIN when you have it. The pipeline can resolve MarketScreener from ISIN; you don’t need to fill `marketscreener_id` for every row.
2. **marketscreener_id** – Can be left empty; the first time you run a report for that ticker, the pipeline will try to resolve it by ISIN (with validation).
3. **Batches** – You can run the script multiple times with different CSVs (e.g. by exchange or source). Use `--append-only` for later batches if you don’t want to overwrite existing rows.
4. **Validation** – Use `--dry-run` and inspect the printed JSON, or write to `-o data/company_master_merged.json` and diff before replacing `company_master.json`.
