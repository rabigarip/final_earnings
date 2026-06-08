"""Build data/tickers.json from data/em_500_tickers.csv (+ analyst additions).

Deterministic, no external API calls. Run once; commit the output. Re-run
when the source CSV updates (typically when the index composition changes).

Design choices that matter downstream:

1. **Cross-listing dedup.** Group rows by a normalized company name
   ("Industrial and Commercial Bank of China" → "icbc"). The highest-
   market-cap ticker in each group becomes `is_canonical=True`; siblings
   are listed in the canonical's `siblings` array. The dashboard hides
   non-canonical tickers by default so the analyst sees ~300 unique
   companies instead of 500 cross-listing duplicates.

2. **BDR / SIC detection.** Brazilian `.SA` tickers whose names match
   US-listed equities (NVIDIA, Apple, Alphabet, etc.) AND whose ticker
   ends in `34.SA` or `35.SA` are flagged as BDRs. Same heuristic for
   Mexican `.MX` tickers whose names match foreign equities (the .MX
   universe in our CSV is purely SICs). For these, the renderer should
   pull fundamentals from `underlying_ticker` and display BDR-specific
   price/liquidity on slide 1.

3. **Currency scale.** South African `JNB` exchange tickers trade in ZAc
   (cents — 1/100 ZAR). Without `currency_unit_scale: 100`, the displayed
   prices will be 100x wrong. Other tickers use scale 1.

4. **Template family.** Maps sector + name patterns to one of ~10 deck
   template families (bank, insurance, energy, industrial, tech, retail,
   reit, utility, telco, materials, healthcare, other). This lets the
   LLM prompt + slide-2 estimates table + slide-3 peer-column schema
   dispatch on the family without hard-coded "if bank else" sprinkled
   through the renderer.

5. **Peer set defaults.** Top-N by market-cap-USD within the same
   template_family AND same region (so SABIC's peers are GCC chemicals,
   not US chemicals). Overridable per-ticker via a separate manual
   override file (future).

Output: data/tickers.json (one record per ticker, ~500 entries, ~300KB).
"""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# ──────────────────────── INPUTS / OUTPUTS ────────────────────────

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "data" / "em_500_tickers.csv"
OUT_PATH = ROOT / "data" / "tickers.json"

# Analyst-requested additions beyond the CSV. Bank Muscat is already in
# the test data; Spinneys (the UAE retailer, listed 2024) is new.
ANALYST_ADDITIONS = [
    {"ticker": "BKMB.OM", "country": "Oman",
     "company_name": "Bank Muscat SAOG", "exchange": "MSX",
     "market_cap": 3_100_000_000, "currency": "OMR"},
    {"ticker": "SPINNEYS.AE", "country": "United Arab Emirates",
     "company_name": "Spinneys 1961 Holding PLC", "exchange": "DFM",
     "market_cap": 4_500_000_000, "currency": "AED"},
]


# ──────────────────────── HEURISTICS ──────────────────────────────

# Exchange → ISO2 country (CSV's `country` field is human-readable; we
# also need a code for IMF/WB macro lookups).
COUNTRY_ISO2 = {
    "China": "CN", "Hong Kong": "HK", "India": "IN", "Brazil": "BR",
    "South Africa": "ZA", "Mexico": "MX", "Saudi Arabia": "SA",
    "Qatar": "QA", "Kuwait": "KW", "Oman": "OM",
    "United Arab Emirates": "AE",
}

# Currency unit scale — 100 for ZAc (cents); base unit otherwise.
CURRENCY_UNIT_SCALE = {
    "ZAc": 100,      # South African cents → ZAR
    # All others: 1 (display in the currency as quoted).
}

# Exchange-suffix → ticker country (override CSV `country` for
# cross-listings like BHG.JO which is BHP Group, an Australian company
# secondary-listed in Johannesburg).
EXCHANGE_TO_COUNTRY = {
    "SS": "CN", "SZ": "CN", "HK": "HK", "NS": "IN", "BO": "IN",
    "SA": "BR", "JO": "ZA", "MX": "MX", "SR": "SA", "QA": "QA",
    "KW": "KW", "OM": "OM", "AE": "AE",
}

# Sector / industry classification from company name patterns. Ordered
# — first match wins. Use word boundary only at START of token; end is
# unbounded so plural / suffix forms (Pharmaceuticals, Holdings) match.
SECTOR_PATTERNS = [
    # Banks (include synonyms — Chartered, Sumitomo, etc.)
    (r"\b(bank|banking|banque|banco|bancorp|chartered|sberbank)",
        ("Financials", "Diversified Banks")),
    # Insurance — Manulife, Prudential, AIA, PICC all here
    (r"\b(insurance|assurance|life ins|reinsurance|bupa|manulife|prudential|aia group|outsurance|ping an|sanlam|sun life|discovery limited|sbi life|sbilife|cooperative ins)",
        ("Financials", "Insurance")),
    # Finance companies / NBFCs (Bajaj Finance, Shriram, KFH)
    (r"\b(bajaj fin|shriram fin|finance limited|finance company|finance house|finance ltd|kuwait finance)",
        ("Financials", "Diversified Financials")),
    # Asset / wealth management / securities / exchanges
    (r"\b(asset management|asset mgmt|wealth|securities|capital markets|brokers?|brokerage|exchanges|clearing|tadawul|nse|bse)",
        ("Financials", "Capital Markets")),
    # Oil & gas (deliberately excludes 'petrochem' — that string matches
    # 'petrochemical', a chemicals-industry indicator. Specific energy
    # names are listed explicitly: PetroChina, Aramco, Sinopec, etc.)
    (r"\b(petroleum|oil and|oil &|oil company|natural gas|gas company|energy company|petrochina|aramco|exxon|chevron|cnooc|sasol|sinopec|reliance industries|ongc|iocl|indian oil|coal india|yanbu national)",
        ("Energy", "Integrated Oil & Gas")),
    # Refining / downstream
    (r"\b(refining|refinery|luberef|rabigh)", ("Energy", "Oil & Gas Refining")),
    # Mining / metals (incl. gold, copper, aluminium)
    (r"\b(mining|gold|copper|aluminum|aluminium|steel|iron|metals|coal|nickel|platinum|kumba|sibanye|harmony|gold fields|valterra|northam|hindalco|tata steel|jsw steel|jindal|vedanta|hindustan zinc|zijin|northern rare earth|shandong gold|jiangxi copper|cmoc)",
        ("Materials", "Metals & Mining")),
    # Chemicals
    (r"\b(chemical|chemic|fertili[zs]er|agri-nutrients|petrochemical|nutrient|sabic|asian paints|sasol limited|wanhua|saudi kayan|advanced petrochemical|saudi chemical|baofeng|sahara international)",
        ("Materials", "Chemicals")),
    # Cement
    (r"\b(cement|concrete|ultracem|ultratech|grasim)", ("Materials", "Construction Materials")),
    # Telco
    (r"\b(telecom|telecommunications?|telefon|telecom corp|telephon|airtel|vodacom|etisalat|saudi telecom|china mobile|jio|mtn group|ooredoo|tower corp|tower corporation|mobile telecom)",
        ("Communication Services", "Integrated Telecom")),
    # Internet / media / digital
    (r"\b(tencent|alibaba|baidu|netease|jd\.com|meituan|naspers|prosus|kuaishou|netflix|meta platforms|alphabet|trip\.com|pop mart|nongfu spring|tencent music|kuaishou|mbc group|saudi research and media|jahez|americana restaurants|seera|swiggy|zomato|eternal limited|hithink royalflush|east money|naspers limited|swvl)",
        ("Communication Services", "Interactive Media & Services")),
    # Semiconductors
    (r"\b(semiconductor|optical|opto|integrated circuits|chip|wafer|cambricon|hua hong|smic|tsmc|micron|broadcom|nvidia|advanced micro|asml|metax|hygon|montage|gigadevice|innolight|eoptolink|tfc optical|amec|moore threads)",
        ("Information Technology", "Semiconductors")),
    # Software / IT services / consumer tech
    (r"\b(software|technology|tata consultancy|infosys|wipro|hcltech|oracle|microsoft|sap|elm company|arabian internet|al moammar|palantir|xiaomi|lens technology|adobe|service now|crm|salesforce|rasan|swvl)",
        ("Information Technology", "Software & Services")),
    # Apparel / sporting / lifestyle (Anta, Fast Retailing/Uniqlo)
    (r"\b(anta sports|fast retailing|uniqlo|nike|adidas|on running|lululemon|lululemon athletica|li ning|li-ning)",
        ("Consumer Discretionary", "Apparel & Luxury Goods")),
    # Auto
    (r"\b(motor|automobile|automotive|byd|tesla|toyota|geely|hyundai|maruti|mahindra|chrysler|ford|gm|seres|fuyao glass|sany heavy|weichai|eicher|nio|xpeng|tata motors|jaguar)",
        ("Consumer Discretionary", "Automobiles")),
    # Tobacco
    (r"\b(tobacco|cigarette|itc limited|altria|british american tobacco|philip morris)",
        ("Consumer Staples", "Tobacco")),
    # Food / staples
    (r"\b(food|nutrition|dairy|beverage|brewer|distill|spirits|wuliangye|moutai|kweichow|haitian|almarai|savola|americana restaurants|fen wine|nongfu|nestle|nestlé|hindustan unilever|haier smart|midea|gree|first milling|modern mills|savola group|catrion|al rajhi food|salt lake|qinghai salt)",
        ("Consumer Staples", "Food, Beverage & Tobacco")),
    # Pharma / healthcare
    (r"\b(pharma|pharmaceutical|drug|biotech|biome|biolog|hospital|medical|medicine|healthcare|health|abbvie|astrazeneca|lilly|jamjoom|hengrui|sun pharm|divis lab|wuxi|hansoh|beone|nahdi|al-dawaa|aldawaa|jabal omar|dr\.|specialized medical|mouwasat|dallah|hammadi|saudi pharm|fakeeh|leejam sports|leejam|knowledge atlas|nuobikan|mind ray|mindray)",
        ("Health Care", "Pharmaceuticals & Biotech")),
    # Real estate / property / REITs
    (r"\b(real estate|properties|property|reit|sun hung kai|swire pacific|swire|mtr corporation|emaar|jabal omar|dar al arkan|knowledge economic|makkah construction|umm al qura|seera holding|china resources land|crescent point|knowledge economic city|emaar economic|retal|arabian centres|dar al majed|alramz)",
        ("Real Estate", "Real Estate")),
    # Utilities (power, water, gas distribution)
    (r"\b(power|electric|hydropower|nuclear|water|tap water|utility|utilities|saudi energy|cgn power|hua hua|huaneng|china yangtze|nari technology|clp holdings|adani power|ntpc|powergrid|alkhorayef|miahona|national gas|industrialization)",
        ("Utilities", "Electric Utilities")),
    # Transport / logistics / airlines
    (r"\b(railway|airline|airlines|shipping|logistics|transport|ports|aviation|cosco|cargo|s\.f\.|sf holding|jahez|flynas|saudi ground|interglobe aviation|indigo|tvs motor|adani ports|hindustan aeronautics|cssc|crrc|aldrees petroleum|automotive services|united international transportation|aviation industry|china state construction|china railway|leejam)",
        ("Industrials", "Transportation")),
    # Defense / aerospace
    (r"\b(defen[cs]e|aerospace|military|aircraft|aviation industry|avic|hindustan aeronautics|bharat electronics)",
        ("Industrials", "Aerospace & Defense")),
    # Industrials / construction / capital goods
    (r"\b(industries|industrial|construction|aerospace|machinery|equipment|heavy industry|china state construction|cssc|crrc|sany|larsen|toubro|tools|engineering|electrical industries|east pipes|riyadh cables|astra industrial|elec ind|al-babtain|saudi industrial)",
        ("Industrials", "Capital Goods")),
    # Education
    (r"\b(education|educational|learning|university|college|ataa|knowledge|al masar al shamil)",
        ("Consumer Discretionary", "Education Services")),
    # Diversified conglomerates
    (r"\b(berkshire|hathaway|holdings|holding|group|conglomerate|adani enterprises|ck hutchison|hutchison|reliance group)",
        ("Industrials", "Diversified")),
]

# Per-ticker manual overrides for cases the pattern matcher misses
# (truncated names, edge cases). Analyst-editable; commit changes.
MANUAL_SECTOR_OVERRIDES: dict[str, tuple[str, str]] = {
    "1288.HK":   ("Financials", "Diversified Banks"),    # truncated "ABC" = Agricultural Bank of China H-share
    "4335.HK":   ("Information Technology", "Semiconductors"),  # Intel HK
    "002475.SZ": ("Information Technology", "Hardware"),  # Luxshare Precision (Apple supplier)
    "0267.HK":   ("Industrials", "Diversified"),         # CITIC Limited
    "2714.HK":   ("Consumer Staples", "Food, Beverage & Tobacco"),  # Muyuan Foods (hog producer)
    "BAJAJ-AUTO.NS": ("Consumer Discretionary", "Automobiles"),
    "BAJAJ-AUTO.BO": ("Consumer Discretionary", "Automobiles"),
    "DMART.NS":  ("Consumer Discretionary", "Specialty Retail"),
    "DMART.BO":  ("Consumer Discretionary", "Specialty Retail"),
    "0762.HK":   ("Communication Services", "Integrated Telecom"),  # China Unicom HK
    "002050.SZ": ("Industrials", "Capital Goods"),       # Sanhua Intelligent Controls
    "002384.SZ": ("Information Technology", "Hardware"),  # Dongshan Precision
    "601066.SS": ("Financials", "Capital Markets"),      # CSC Financial
    "000063.SZ": ("Information Technology", "Hardware"),  # ZTE Corp
    "0763.HK":   ("Information Technology", "Hardware"),  # ZTE HK
    "DIVISLAB.NS": ("Health Care", "Pharmaceuticals & Biotech"),
    "DIVISLAB.BO": ("Health Care", "Pharmaceuticals & Biotech"),
    "HCLTECH.NS":  ("Information Technology", "Software & Services"),
    "HCLTECH.BO":  ("Information Technology", "Software & Services"),
    "TITAN.NS":  ("Consumer Discretionary", "Apparel & Luxury Goods"),
    "TITAN.BO":  ("Consumer Discretionary", "Apparel & Luxury Goods"),
    # Saudi small / mid caps
    "4190.SR":   ("Consumer Discretionary", "Specialty Retail"),  # Jarir Marketing
    "4090.SR":   ("Real Estate", "Real Estate"),            # Taiba Investment
    "2381.SR":   ("Energy", "Oil & Gas Services"),         # Arabian Drilling
    "4003.SR":   ("Consumer Discretionary", "Specialty Retail"),  # United Electronics
    "4071.SR":   ("Communication Services", "Advertising"),  # Arabian Contracting Services (out-of-home advertising)
    "4001.SR":   ("Consumer Staples", "Food, Beverage & Tobacco"),  # Al-Othaim Markets
    "4084.SR":   ("Financials", "Capital Markets"),        # Derayah Financial
    "6010.SR":   ("Consumer Staples", "Food, Beverage & Tobacco"),  # NADEC
    "4150.SR":   ("Real Estate", "Real Estate"),           # Arriyadh Development
    "4165.SR":   ("Consumer Staples", "Personal Products"),  # Al Majed Oud (perfume retail)
    "2040.SR":   ("Materials", "Construction Materials"),  # Saudi Ceramic
    "1831.SR":   ("Industrials", "Commercial Services"),   # Maharah HR
    "1302.SR":   ("Industrials", "Capital Goods"),         # Bawan
    "4192.SR":   ("Consumer Discretionary", "Specialty Retail"),  # Al-Saif Stores
    # JSE / South Africa
    "ANH.JO":    ("Consumer Staples", "Food, Beverage & Tobacco"),   # AB InBev
    "ANG.JO":    ("Materials", "Metals & Mining"),                   # AngloGold Ashanti
    "AGL.JO":    ("Materials", "Metals & Mining"),                   # Anglo American
    "GLN.JO":    ("Materials", "Metals & Mining"),                   # Glencore
    "S32.JO":    ("Materials", "Metals & Mining"),                   # South32
    "CFR.JO":    ("Consumer Discretionary", "Apparel & Luxury Goods"),  # Richemont
    "FSR.JO":    ("Financials", "Diversified Banks"),               # FirstRand
    "BID.JO":    ("Industrials", "Commercial Services"),            # Bid Corp (food distribution)
    "BHG.JO":    ("Materials", "Metals & Mining"),                  # BHP secondary listing
    # SABIC parent = chemicals (not generic "industries")
    "2010.SR":   ("Materials", "Chemicals"),
    # Spinneys = grocery / specialty retail
    "SPINNEYS.AE": ("Consumer Discretionary", "Specialty Retail"),
}

# Per-ticker cross-listing overrides — companies whose normalized
# Chinese name differs from their HK name (1288 = "ABC", 601288 =
# "Agricultural Bank of China") and a few other cases where the name-
# normalization heuristic doesn't catch the pair.
MANUAL_CROSS_LISTINGS: dict[str, str] = {
    # H-share ticker → A-share canonical group identifier
    "1288.HK": "agricultural bank china",       # 601288.SS group
    "1398.HK": "industrial commercial bank china",  # 601398.SS
    "939.HK": "china construction bank",
    "0939.HK": "china construction bank",
    "3988.HK": "china bank",   # Bank of China — generic match
    # Lots more could go here; this is the bootstrap set.
}

def classify_sector(name: str, ticker: str = "") -> tuple[str, str]:
    """Return (sector, industry) tuple. Order: manual override → pattern
    match → fallback ('Other', 'Other')."""
    if ticker and ticker in MANUAL_SECTOR_OVERRIDES:
        return MANUAL_SECTOR_OVERRIDES[ticker]
    n = name.lower()
    for pat, (sec, ind) in SECTOR_PATTERNS:
        if re.search(pat, n, re.I):
            return sec, ind
    return ("Other", "Other")


# Sector → template family. The renderer + LLM prompt dispatch on this.
SECTOR_TO_TEMPLATE = {
    "Diversified Banks": "bank",
    "Insurance": "insurance",
    "Capital Markets": "financial_services",
    "Diversified Financials": "financial_services",
    "Integrated Oil & Gas": "energy",
    "Oil & Gas Refining": "energy",
    "Metals & Mining": "materials",
    "Chemicals": "materials",
    "Construction Materials": "materials",
    "Integrated Telecom": "telco",
    "Interactive Media & Services": "tech",
    "Semiconductors": "tech",
    "Software & Services": "tech",
    "Automobiles": "industrial",
    "Apparel & Luxury Goods": "retail",
    "Specialty Retail": "retail",
    "Personal Products": "consumer_staples",
    "Hardware": "tech",
    "Commercial Services": "industrial",
    "Advertising": "tech",
    "Oil & Gas Services": "energy",
    "Food, Beverage & Tobacco": "consumer_staples",
    "Tobacco": "consumer_staples",
    "Pharmaceuticals & Biotech": "healthcare",
    "Real Estate": "reit",
    "Electric Utilities": "utility",
    "Transportation": "industrial",
    "Capital Goods": "industrial",
    "Aerospace & Defense": "industrial",
    "Education Services": "consumer_discretionary",
    "Diversified": "industrial",
    "Other": "other",
}


def template_family(industry: str) -> str:
    return SECTOR_TO_TEMPLATE.get(industry, "other")


# Normalize company names for cross-listing dedup. Strip legal suffixes,
# punctuation, case-fold; keep the meaningful tokens.
LEGAL_SUFFIX_RE = re.compile(
    r"\b(co\.?|company|corp\.?|corporation|ltd\.?|limited|inc\.?|incorporated|"
    r"plc|p\.?l\.?c\.?|sa|sa de cv|saog|s\.a\.|s\.p\.a\.|n\.v\.|nv|ag|gmbh|"
    r"q\.p\.s\.c\.|p\.s\.q\.c\.|k\.s\.c\.p|k\.s\.c\.|holdings?|group|"
    r"the|of)\b",
    re.I,
)
PUNCT_RE = re.compile(r"[^a-z0-9]+")

def normalize_company(name: str) -> str:
    n = name.lower()
    n = LEGAL_SUFFIX_RE.sub(" ", n)
    n = PUNCT_RE.sub(" ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


# ──────────────────────── BDR / SIC HEURISTIC ──────────────────────

# Known foreign companies whose Brazilian BDRs / Mexican SICs appear in
# the CSV. The home-exchange ticker for the underlying. When the CSV
# has a `.SA` or `.MX` row matching one of these names AND no Brazil/
# Mexico business is plausibly behind the name, flag as DR.
DR_UNDERLYING = {
    # name_normalized → home-exchange ticker
    "nvidia": "NVDA",
    "apple": "AAPL",
    "alphabet": "GOOGL",
    "microsoft": "MSFT",
    "amazon": "AMZN",
    "taiwan semiconductor manufacturing": "TSM",
    "meta platforms": "META",
    "broadcom": "AVGO",
    "tesla": "TSLA",
    "berkshire hathaway": "BRK.B",
    "walmart": "WMT",
    "eli lilly": "LLY",
    "jpmorgan chase": "JPM",
    "exxon mobil": "XOM",
    "astrazeneca": "AZN",
    "visa": "V",
    "johnson johnson": "JNJ",
    "asml": "ASML",
    "micron technology": "MU",
    "oracle": "ORCL",
    "mastercard": "MA",
    "costco wholesale": "COST",
    "abbvie": "ABBV",
    "netflix": "NFLX",
    "chevron": "CVX",
    "palantir technologies": "PLTR",
    "bank america": "BAC",
    "procter gamble": "PG",
    "home depot": "HD",
    "intel": "INTC",
    "manulife financial": "MFC",
    "standard chartered": "STAN.L",
    "prudential": "PRU.L",
    "anheuser busch inbev": "ABI.BR",
    "british american tobacco": "BATS.L",
    "glencore": "GLEN.L",
    "anglo american": "AAL.L",
    "richemont": "CFR.SW",
    "bhp": "BHP.AX",
    "fast retailing": "9983.T",
    "hsbc holdings": "HSBA.L",
}

# Known sector / industry for each DR underlying. Used when the BDR/SIC
# row's company name doesn't trigger our pattern matcher (e.g. "Apple
# Inc." has no keyword in any sector regex).
UNDERLYING_SECTORS = {
    "NVDA":  ("Information Technology", "Semiconductors"),
    "AAPL":  ("Information Technology", "Software & Services"),
    "GOOGL": ("Communication Services", "Interactive Media & Services"),
    "MSFT":  ("Information Technology", "Software & Services"),
    "AMZN":  ("Consumer Discretionary", "Specialty Retail"),
    "TSM":   ("Information Technology", "Semiconductors"),
    "META":  ("Communication Services", "Interactive Media & Services"),
    "AVGO":  ("Information Technology", "Semiconductors"),
    "TSLA":  ("Consumer Discretionary", "Automobiles"),
    "BRK.B": ("Industrials", "Diversified"),
    "WMT":   ("Consumer Staples", "Food, Beverage & Tobacco"),
    "LLY":   ("Health Care", "Pharmaceuticals & Biotech"),
    "JPM":   ("Financials", "Diversified Banks"),
    "XOM":   ("Energy", "Integrated Oil & Gas"),
    "AZN":   ("Health Care", "Pharmaceuticals & Biotech"),
    "V":     ("Financials", "Capital Markets"),
    "JNJ":   ("Health Care", "Pharmaceuticals & Biotech"),
    "ASML":  ("Information Technology", "Semiconductors"),
    "MU":    ("Information Technology", "Semiconductors"),
    "ORCL":  ("Information Technology", "Software & Services"),
    "MA":    ("Financials", "Capital Markets"),
    "COST":  ("Consumer Staples", "Specialty Retail"),
    "ABBV":  ("Health Care", "Pharmaceuticals & Biotech"),
    "NFLX":  ("Communication Services", "Interactive Media & Services"),
    "CVX":   ("Energy", "Integrated Oil & Gas"),
    "PLTR":  ("Information Technology", "Software & Services"),
    "BAC":   ("Financials", "Diversified Banks"),
    "PG":    ("Consumer Staples", "Food, Beverage & Tobacco"),
    "HD":    ("Consumer Discretionary", "Specialty Retail"),
    "INTC":  ("Information Technology", "Semiconductors"),
    "MFC":   ("Financials", "Insurance"),
    "STAN.L": ("Financials", "Diversified Banks"),
    "PRU.L": ("Financials", "Insurance"),
    "ABI.BR": ("Consumer Staples", "Food, Beverage & Tobacco"),
    "BATS.L": ("Consumer Staples", "Tobacco"),
    "GLEN.L": ("Materials", "Metals & Mining"),
    "AAL.L": ("Materials", "Metals & Mining"),
    "CFR.SW": ("Consumer Discretionary", "Apparel & Luxury Goods"),
    "BHP.AX": ("Materials", "Metals & Mining"),
    "9983.T": ("Consumer Discretionary", "Apparel & Luxury Goods"),
    "HSBA.L": ("Financials", "Diversified Banks"),
}


def detect_dr(ticker: str, name_norm: str, country: str) -> str | None:
    """Return underlying ticker if this row is a depositary receipt,
    else None. Heuristic: ticker exchange suffix is `.SA` (Brazilian)
    or `.MX` (Mexican) AND the normalized company name matches a known
    foreign-listed company. We don't flag genuine Brazilian/Mexican
    companies (Vale, Itaú, América Móvil, etc.)."""
    if not ticker.endswith((".SA", ".MX")):
        return None
    # Walk the DR underlying map. Match on substring of the normalized
    # name to handle "alphabet inc" matching "alphabet".
    for key, underlying in DR_UNDERLYING.items():
        if key in name_norm:
            return underlying
    return None


# ──────────────────────── MARKET CAP USD APPROXIMATION ─────────────

# Spot-ish FX rates for USD normalization. Used only for ranking/sorting
# in the dashboard — not for any deck content. Approximate is fine.
FX_TO_USD = {
    "USD": 1.0,
    "CNY": 1/7.24, "HKD": 1/7.79, "INR": 1/83.5, "BRL": 1/5.80,
    "ZAR": 1/18.5, "ZAc": 1/1850.0,   # ZAc = ZAR/100
    "MXN": 1/17.0, "SAR": 1/3.75, "QAR": 1/3.64, "KWF": 1/0.31,
    "OMR": 1/0.385, "AED": 1/3.673,
}


# ──────────────────────── BUILD ────────────────────────────────────

def main() -> int:
    rows = []
    with CSV_PATH.open() as f:
        reader = csv.DictReader(f)
        for r in reader:
            if not r.get("ticker"):
                continue
            rows.append(r)
    # Add analyst-requested additions if not already present.
    seen_tickers = {r["ticker"] for r in rows}
    for add in ANALYST_ADDITIONS:
        if add["ticker"] not in seen_tickers:
            rows.append({**add, "as_of_date": "2026-05-27"})

    # First pass — enrich every row with sector/industry/template/currency/iso2/dr.
    enriched: list[dict] = []
    for r in rows:
        ticker = r["ticker"].strip()
        name = r["company_name"].strip()
        country = r["country"].strip()
        currency = (r.get("currency") or "").strip()
        try:
            mcap_local = float(r["market_cap"])
        except (ValueError, TypeError):
            mcap_local = 0.0
        # Exchange suffix override (BHG.JO is BHP, Australian).
        suffix = ticker.split(".")[-1] if "." in ticker else ""
        iso2 = EXCHANGE_TO_COUNTRY.get(suffix) or COUNTRY_ISO2.get(country) or ""
        # DRs inherit sector from the underlying. Look up by the
        # underlying ticker's known sector, since the BDR/SIC name
        # ("Apple Inc.") doesn't trigger our patterns.
        name_norm = normalize_company(name)
        underlying = detect_dr(ticker, name_norm, country)
        if underlying and underlying in UNDERLYING_SECTORS:
            sector, industry = UNDERLYING_SECTORS[underlying]
        else:
            sector, industry = classify_sector(name, ticker)
        tfam = template_family(industry)
        name_norm = normalize_company(name)
        underlying = detect_dr(ticker, name_norm, country)
        is_dr = bool(underlying)
        currency_scale = CURRENCY_UNIT_SCALE.get(currency, 1)
        # USD-normalize for ranking.
        fx = FX_TO_USD.get(currency, None)
        mcap_usd = (mcap_local * fx) if (fx and mcap_local) else None
        enriched.append({
            "_ticker": ticker, "_name": name, "_name_norm": name_norm,
            "_country": country, "_iso2": iso2, "_currency": currency,
            "_currency_scale": currency_scale, "_sector": sector,
            "_industry": industry, "_template_family": tfam,
            "_mcap_local": mcap_local, "_mcap_usd": mcap_usd,
            "_is_dr": is_dr, "_underlying": underlying, "_exchange": r.get("exchange", ""),
        })

    # Second pass — group by normalized name. The highest-mcap-USD ticker
    # in each group is canonical. Skip DRs from canonical eligibility —
    # they should never be canonical for the underlying name.
    groups: dict[str, list[dict]] = defaultdict(list)
    for e in enriched:
        if e["_is_dr"]:
            # DRs join their underlying's group via the underlying name.
            group_key = normalize_company(e["_underlying"])
        else:
            group_key = e["_name_norm"]
        groups[group_key].append(e)

    # Pick canonical per group.
    canonical_by_group: dict[str, str] = {}
    for gkey, members in groups.items():
        # Prefer non-DR canonical. Among non-DRs, max market_cap_usd.
        non_drs = [m for m in members if not m["_is_dr"]]
        pool = non_drs if non_drs else members
        canonical = max(pool, key=lambda m: (m["_mcap_usd"] or 0))
        canonical_by_group[gkey] = canonical["_ticker"]

    # Third pass — emit final records with siblings + canonical pointers.
    by_ticker = {e["_ticker"]: e for e in enriched}
    out: list[dict] = []
    for e in enriched:
        gkey = (normalize_company(e["_underlying"]) if e["_is_dr"]
                else e["_name_norm"])
        canon = canonical_by_group[gkey]
        siblings = [m["_ticker"] for m in groups[gkey] if m["_ticker"] != e["_ticker"]]
        out.append({
            "ticker": e["_ticker"],
            "company_name": e["_name"],
            "exchange": e["_exchange"],
            "exchange_country": e["_iso2"],
            "currency": e["_currency"],
            "currency_unit_scale": e["_currency_scale"],
            "reporting_currency": e["_currency"],   # default; ADRs in USD overridden later
            "sector": e["_sector"],
            "industry": e["_industry"],
            "template_family": e["_template_family"],
            "market_cap_local": e["_mcap_local"],
            "market_cap_usd": round(e["_mcap_usd"], 2) if e["_mcap_usd"] else None,
            "is_canonical": (e["_ticker"] == canon),
            "company_group": gkey,
            "siblings": sorted(siblings),
            "is_depositary_receipt": e["_is_dr"],
            "underlying_ticker": e["_underlying"],
            "dr_fundamentals_source": ("underlying" if e["_is_dr"] else None),
            "peer_set": [],                # filled in pass 4
            "providers": {
                "yfinance": _yf_support(e["_exchange"], e["_iso2"]),
                "marketscreener": "supported",
                "investing": "supported",
                "bloomberg_ticker": None,
            },
            "ir_portal_url": None,
            "disclosure_feed": None,
            "fiscal_year_end_month": _fy_end_month(e["_iso2"]),
            "active": True,
            "notes": "",
        })

    # Pass 4 — peer sets. For each canonical, top-N by mcap-USD within
    # same template_family + same region (close-by countries: GCC group,
    # India group, China-HK group, Brazil-Mexico DR group, ZA group).
    REGION_GROUPS = {
        "SA": "GCC", "QA": "GCC", "KW": "GCC", "OM": "GCC", "AE": "GCC", "BH": "GCC",
        "CN": "CN-HK", "HK": "CN-HK",
        "IN": "IN",
        "ZA": "ZA",
        "BR": "AMER-DR", "MX": "AMER-DR",
    }
    canonical_records = [r for r in out if r["is_canonical"]]
    for rec in out:
        region = REGION_GROUPS.get(rec["exchange_country"], "OTHER")
        tfam = rec["template_family"]
        # Same template + same region + canonical + not self.
        same_region = [c for c in canonical_records
                        if c["template_family"] == tfam
                        and REGION_GROUPS.get(c["exchange_country"], "OTHER") == region
                        and c["ticker"] != rec["ticker"]]
        same_region.sort(key=lambda c: c["market_cap_usd"] or 0, reverse=True)
        peers = [c["ticker"] for c in same_region[:7]]
        # If same-region pool is thin (<5 peers), backfill from same
        # template family globally so we always have a meaningful comp
        # set. Indian energy and Mexican retail are the most common
        # under-supplied regions in the current universe.
        if len(peers) < 5:
            global_pool = [c for c in canonical_records
                            if c["template_family"] == tfam
                            and c["ticker"] != rec["ticker"]
                            and c["ticker"] not in peers]
            global_pool.sort(key=lambda c: c["market_cap_usd"] or 0, reverse=True)
            peers.extend(c["ticker"] for c in global_pool[: 7 - len(peers)])
        rec["peer_set"] = peers

    # Pass 5 — known IR portals + Bloomberg tickers for tickers we've already encountered.
    KNOWN_IR = {
        "BKMB.OM": "https://www.bankmuscat.om/en/investorrelations/Pages/Quarterly-Reports.aspx",
        "SPINNEYS.AE": "https://www.spinneys.com/en-ae/investor-relations/",
        "2020.SR": "https://www.sabic.com/en/investors",
        "2222.SR": "https://www.aramco.com/en/investors/",
        "1120.SR": "https://www.alrajhibank.com.sa/en/investor-relations",
    }
    KNOWN_BBG = {
        "BKMB.OM": "BKMB OM Equity",
        "SPINNEYS.AE": "SPINNEYS UH Equity",
        "2020.SR": "2020 AB Equity",
        "2222.SR": "2222 AB Equity",
    }
    for rec in out:
        if rec["ticker"] in KNOWN_IR:
            rec["ir_portal_url"] = KNOWN_IR[rec["ticker"]]
        if rec["ticker"] in KNOWN_BBG:
            rec["providers"]["bloomberg_ticker"] = KNOWN_BBG[rec["ticker"]]

    # Sort: canonical-first then market-cap-desc.
    out.sort(key=lambda r: (not r["is_canonical"], -(r["market_cap_usd"] or 0)))

    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"Wrote {len(out)} ticker records to {OUT_PATH}")
    print(f"  Unique companies (canonical): {sum(1 for r in out if r['is_canonical'])}")
    print(f"  Depositary receipts (DRs):    {sum(1 for r in out if r['is_depositary_receipt'])}")
    by_family = defaultdict(int)
    for r in out:
        if r["is_canonical"]:
            by_family[r["template_family"]] += 1
    print("  Canonical by template family:")
    for fam, n in sorted(by_family.items(), key=lambda kv: -kv[1]):
        print(f"    {fam:<22} {n}")
    return 0


def _yf_support(exchange: str, iso2: str) -> str:
    """Yahoo Finance coverage heuristic by exchange.

    Verified gaps:
      * Oman MSX (.OM)         — `BKMB.OM` returns HTTP 404 from yfinance.
                                  Confirmed no Yahoo coverage; mark as
                                  "unsupported" to skip the live_quote
                                  attempt and the 1-2s fetch latency.
      * Kuwait KSE (.KW)       — similar; few tickers covered.
      * Shanghai/Shenzhen A-shares — yfinance has some coverage but
        many tickers 404 individually. "partial" is the honest tag.
      * Saudi (.SR) / Qatar (.QA) / UAE (.AE) — coverage is hit-and-
        miss per ticker; pipeline attempts the call and falls through
        on failure. Marking these "partial" so the live_quote gate
        still tries them (some big names like 2222.SR work).
    """
    if exchange in ("SHH", "SHZ"):
        return "partial"
    if iso2 in ("OM", "KW", "BH"):
        return "unsupported"
    if iso2 in ("SA", "QA", "AE"):
        return "partial"
    return "supported"


def _fy_end_month(iso2: str) -> int:
    """Fiscal year-end month by jurisdiction default. Indian companies
    typically report Apr-Mar (FY ends Mar = month 3). Everyone else
    defaults to calendar (Dec = 12)."""
    return 3 if iso2 == "IN" else 12


if __name__ == "__main__":
    sys.exit(main())
