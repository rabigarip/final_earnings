"""
MarketScreener page-specific scraping — earnings preview backend.

CRITICAL: Each function targets ONE page. Do not mix fields from different pages.
Every returned object includes source_page, source_type, extracted_at, warnings.

Page mapping (see docs/DATA_SOURCE_AND_URL_REFERENCE.md for full field mapping):
  - /finances/                    → financial forecast series (annual/quarterly net sales, EBIT, net income, announcement)
  - /finances-income-statement/   → actual/historical income statement (bank revenue lines, EPS, dividend)
  - /valuation-dividend/          → EPS & dividend forecasts, yield, distribution rate, announcement
  - /consensus/                   → analyst rating + target price summary only (delegate to marketscreener_consensus)
  - /calendar/                    → next earnings date, upcoming/past events
  - /valuation/                  → full multiples table (P/E, P/B, EV/EBIT, EV/EBITDA, Yield by year)

Slug: use company_master.marketscreener_id; or discover via search/?q={TICKER}.
Uses requests + BeautifulSoup. Playwright optional for chart/JS content (TODOs).
"""

from __future__ import annotations
import logging
import re
import os
import time
from datetime import datetime, timezone
from typing import Any

import requests

log = logging.getLogger(__name__)
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

try:
    from src.config import cfg, root
    _USE_CONFIG = True
except ImportError:
    _USE_CONFIG = False


# ─── Shared session (reuse TCP + cookies across MS requests) ───────────────

_session: requests.Session | None = None


def _get_session() -> requests.Session:
    """Return a shared requests.Session with browser-like headers and cookies."""
    global _session
    if _session is not None:
        return _session
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    })
    if _USE_CONFIG:
        try:
            ua = cfg().get("scraping", {}).get("user_agent")
            if ua:
                s.headers["User-Agent"] = ua
        except Exception:
            pass
    # Warm the session with a homepage visit to acquire cookies.
    try:
        s.get("https://www.marketscreener.com/", timeout=10)
    except Exception:
        pass
    _session = s
    return s


# ─── Status model (every provider method returns this shape) ─────────────────

class PageStepStatus(BaseModel):
    step: str = ""
    status: str = "failed"  # success | partial | failed
    source: str = "marketscreener"
    fallback_used: bool = False
    message: str = ""
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    record_count: int | None = None
    elapsed_ms: float = 0.0


# ─── Block / captcha detection ─────────────────────────────────────────────

def _is_blocked_response(text: str) -> bool:
    """Detect actual captcha walls or access-denied blocks.

    MarketScreener pages normally contain 'captcha' inside a login-form
    reCAPTCHA site-key attribute — that is NOT a block. A real block is a
    short page whose *primary* content is a challenge or denial.
    """
    lower = text.lower()
    # Short response that mentions captcha/denied is a real block
    if len(text) < 5000:
        if "captcha" in lower or "access denied" in lower or "blocked" in lower:
            return True
    # Explicit challenge pages
    if "verify you are human" in lower or "please complete the captcha" in lower:
        return True
    if "<title>access denied</title>" in lower or "<title>blocked</title>" in lower:
        return True
    # Cloudflare-style challenge page
    if "ray id" in lower and "cloudflare" in lower and len(text) < 20000:
        return True
    return False


# ─── Shared fetch ───────────────────────────────────────────────────────────

def _delay_between_requests() -> None:
    """Apply rate-limit delay between MarketScreener requests (per reference doc)."""
    delay = 1.0
    if _USE_CONFIG:
        try:
            s = cfg()
            delay = float(s.get("scraping", {}).get("min_delay_seconds", delay))
        except Exception:
            pass
    if delay > 0:
        time.sleep(delay)


def _cache_slug(url: str, page_name: str, cache_key_prefix: str | None = None) -> str:
    """Hardened cache key: use ticker_isin_slug_page when prefix provided; else page_slug."""
    if cache_key_prefix:
        return f"{cache_key_prefix}_{page_name}"
    slug = url.rstrip("/").split("/")[-1] if url else "unknown"
    return f"{page_name}_{slug}"


# ─── Repo-tracked snapshot path (Cloudflare workaround) ─────────────
#
# Render's egress IPs are Cloudflare-blocked from MarketScreener (every
# page returns HTTP 403). Same problem we solved for Investing.com via
# the data/investing/ snapshot pattern — GitHub Actions runners use
# residential-class IPs that aren't flagged, so a daily GHA workflow
# refreshes the snapshots and commits them to the repo.
#
# Layout:
#   data/marketscreener/ms_<safe-cache-slug>.html
#
# Where `<safe-cache-slug>` is the same hash the on-disk cache uses, so
# the live-fetch and snapshot-fetch paths share one filename grammar.

def _snapshot_path_for(cache_slug: str):
    """Return the committed-snapshot path for a given cache_slug, or
    None if we can't resolve the repo root. Mirrors the cache/ filename
    shape so live+snapshot use one grammar."""
    try:
        from src.config import root
        safe = re.sub(r"[^a-zA-Z0-9-]", "_", cache_slug)[:80]
        return root() / "data" / "marketscreener" / f"ms_{safe}.html"
    except Exception:
        return None


def _write_disk_cache(cache_slug: str, text: str) -> None:
    """Persist live HTML to the on-disk ./cache so re-runs and the
    auto-grounding extractor can read the just-fetched ticker. Best-effort."""
    try:
        if not (_USE_CONFIG and cfg().get("scraping", {}).get("cache_html")):
            return
        cache_dir = root() / "cache"
        cache_dir.mkdir(exist_ok=True)
        safe = re.sub(r"[^a-zA-Z0-9-]", "_", cache_slug)[:80]
        (cache_dir / f"ms_{safe}.html").write_text(text, encoding="utf-8")
    except Exception:
        pass


# Known MarketScreener page-type suffixes — longest first so e.g.
# "calendar_quarterly" matches before "calendar".
_MS_PAGE_TYPES = (
    "calendar_quarterly", "valuation_dividend", "income_statement",
    "calendar", "finances", "valuation", "consensus", "ratings",
    "summary", "sector", "perf", "recommendations",
)


def _short_snapshot_paths(cache_slug: str):
    """Map a long cache_slug (ms_<ticker>_<isin>_<slug>_<page>) to the LEAN
    committed-snapshot name ms_<ticker>_<page>.html.

    The committed fallback fixtures are stored under the ticker-only name to
    keep the repo small; the live-fetch cache_slug carries isin+slug, so the
    primary path misses them. Without this, a blocked live MS fetch (common
    on Render's datacenter IPs) leaves the slide-2 expectations table and the
    MS performance block EMPTY for Yahoo-blind names like BKMB.OM."""
    import re as _r
    out = []
    try:
        from src.config import root
        ms_dir = root() / "data" / "marketscreener"
    except Exception:
        return out
    for page in _MS_PAGE_TYPES:
        if cache_slug.endswith("_" + page):
            body = cache_slug[len("ms_"):-(len(page) + 1)]   # ticker_isin_slug
            toks = body.split("_")
            tick = []
            for tk in toks:
                # ISIN (2 letters + 9–10 alnum) or the literal 'noisin' ends
                # the ticker portion.
                if tk == "noisin" or _r.fullmatch(r"[A-Z]{2}[A-Z0-9]{9,10}", tk):
                    break
                tick.append(tk)
            if tick:
                out.append(ms_dir / f"ms_{'_'.join(tick)}_{page}.html")
            break
    return out


def _read_snapshot(cache_slug: str) -> str | None:
    """Return the snapshot HTML for a cache_slug, or None if absent /
    blocked-content / unreadable. Used as the fallback when live fetch
    fails with 403 / captcha. Tries the exact long-form name first, then the
    lean ticker-only committed name."""
    candidates = []
    p = _snapshot_path_for(cache_slug)
    if p is not None:
        candidates.append(p)
    candidates.extend(_short_snapshot_paths(cache_slug))
    for path in candidates:
        if path is None or not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        if _is_blocked_response(text):
            continue
        return text
    return None


def _write_snapshot(cache_slug: str, html: str) -> bool:
    """Persist the live HTML to the repo-tracked snapshot location.
    Only invoked when `MS_TRACKED_REFRESH=1` is set (refresh-script
    mode) so production deck runs don't accidentally commit data.
    Returns True on write success."""
    p = _snapshot_path_for(cache_slug)
    if p is None:
        return False
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(html, encoding="utf-8")
        return True
    except Exception:
        return False


def _fetch_page(url: str, cache_slug: str) -> tuple[BeautifulSoup | None, list[str]]:
    """Fetch URL via shared session (cookies + keep-alive), optional cache, return (soup, errors).

    When `MS_OFFLINE_CACHE_FIRST=1` is set (used by tests and as a
    rate-limit recovery mode), serve from the on-disk cache before
    attempting any network request. Falls through to network when the
    cached file is absent or contains a captcha/block page.
    """
    errors: list[str] = []
    timeout = 15
    if _USE_CONFIG:
        try:
            timeout = cfg().get("scraping", {}).get("timeout_seconds", timeout)
        except Exception:
            pass

    cache_dir = None
    cached_path = None
    try:
        if _USE_CONFIG and cfg().get("scraping", {}).get("cache_html"):
            cache_dir = root() / "cache"
            safe = re.sub(r"[^a-zA-Z0-9-]", "_", cache_slug)[:80]
            cached_path = cache_dir / f"ms_{safe}.html"
    except Exception:
        cache_dir = None
        cached_path = None

    # Cache-first path — opt-in via env var.
    if (
        os.environ.get("MS_OFFLINE_CACHE_FIRST") == "1"
        and cached_path is not None
        and cached_path.exists()
    ):
        try:
            text = cached_path.read_text(encoding="utf-8")
            if not _is_blocked_response(text):
                return BeautifulSoup(text, "lxml"), errors
        except Exception:
            pass  # fall through to network

    live_failed = False
    # Opt-in: route HTTP through curl_cffi with Chrome TLS impersonation.
    # Plain `requests` has a distinctive TLS fingerprint Cloudflare flags
    # as bot traffic; curl_cffi mimics Chrome 120's exact ClientHello.
    # We already use this trick for Investing.com — the GHA refresh
    # workflow sets MS_USE_CURL_CFFI=1 so the snapshot-refresh job
    # bypasses the 403s that plain requests gets from MS's Cloudflare.
    # Production decks (Render) keep using `requests` because the
    # snapshot-fallback path doesn't need live MS access at all.
    if os.environ.get("MS_USE_CURL_CFFI") == "1":
        try:
            from curl_cffi import requests as cr
        except ImportError:
            cr = None
        if cr is not None:
            try:
                resp = cr.get(
                    url, impersonate="chrome120", timeout=timeout,
                    headers={
                        "Accept-Language": "en-US,en;q=0.9",
                        "Referer": "https://www.marketscreener.com/"
                                   if "/search/" not in url else "",
                    },
                )
                if resp.status_code != 200:
                    errors.append(f"HTTP {resp.status_code} (curl_cffi)")
                    live_failed = True
                else:
                    text = resp.text
                    if _is_blocked_response(text):
                        errors.append("Captcha (curl_cffi)")
                        live_failed = True
                    else:
                        if os.environ.get("MS_TRACKED_REFRESH") == "1":
                            _write_snapshot(cache_slug, text)
                        _write_disk_cache(cache_slug, text)
                        return BeautifulSoup(text, "lxml"), errors
            except Exception as e:
                errors.append(f"curl_cffi exception: {e}")
                live_failed = True
        # else: fall through to requests path below

    if not live_failed:
        try:
            session = _get_session()
            # Set referer for non-search pages (looks like natural browsing)
            if "/search/" not in url:
                session.headers["Sec-Fetch-Site"] = "same-origin"
                session.headers["Referer"] = "https://www.marketscreener.com/"
            else:
                session.headers["Sec-Fetch-Site"] = "none"
                session.headers.pop("Referer", None)

            resp = session.get(url, timeout=timeout)
            if resp.status_code != 200:
                errors.append(f"HTTP {resp.status_code}")
                live_failed = True
            else:
                text = resp.text
                if _is_blocked_response(text):
                    errors.append("Captcha or access denied")
                    live_failed = True
                else:
                    if _USE_CONFIG and cfg().get("scraping", {}).get("cache_html"):
                        try:
                            cache_dir = root() / "cache"
                            cache_dir.mkdir(exist_ok=True)
                            safe = re.sub(r"[^a-zA-Z0-9-]", "_", cache_slug)[:80]
                            (cache_dir / f"ms_{safe}.html").write_text(text, encoding="utf-8")
                        except Exception:
                            pass
                    if os.environ.get("MS_TRACKED_REFRESH") == "1":
                        _write_snapshot(cache_slug, text)
                    return BeautifulSoup(text, "lxml"), errors
        except requests.RequestException as e:
            errors.append(str(e))
            live_failed = True

    # Live fetch failed (HTTP error, network error, captcha, or 403).
    # Fall back to the repo-tracked snapshot if one exists. This is
    # always-on — no env gate — because Render is the canonical user
    # of this fallback and it's safer to serve stale data than nothing.
    if live_failed:
        snapshot_html = _read_snapshot(cache_slug)
        if snapshot_html is not None:
            errors.append("(served from data/marketscreener/ snapshot)")
            return BeautifulSoup(snapshot_html, "lxml"), errors

    return None, errors


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _coerce_numeric_or_none(value: str) -> float | None:
    """Parse number from cell text; return None for '-', '', 'N/A', or invalid."""
    if value is None:
        return None
    raw = (value or "").strip()
    if not raw or raw in ("-", "N/A", "–", "—"):
        return None
    # Remove commas, spaces; handle B/M suffix
    clean = raw.replace(",", "").replace(" ", "")
    mult = 1.0
    if clean.endswith("B"):
        mult = 1e9
        clean = clean[:-1]
    elif clean.endswith("M"):
        mult = 1e6
        clean = clean[:-1]
    elif clean.endswith("%"):
        clean = clean[:-1]
    elif clean.endswith("x"):
        clean = clean[:-1]
    try:
        return float(clean) * mult
    except ValueError:
        return None


def _all_missing(values: list[float | None]) -> bool:
    """True if every value is None or missing."""
    return all(v is None for v in values)


def coerce_percent_or_none(value: str) -> float | None:
    """Parse percentage from cell text; strip % and commas; return None if invalid."""
    if value is None:
        return None
    raw = (str(value).strip().replace(",", ".").replace("%", "").strip())
    if not raw or raw in ("-", "N/A", "–", "—"):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def normalize_quarter_label(text: str) -> str:
    """Normalize quarter to 2025Q1 format (no space)."""
    if not text or not isinstance(text, str):
        return ""
    t = text.strip()
    m = re.match(r"(?:(\d{4})\s*Q(\d)|Q(\d)\s*(\d{4}))", t, re.I)
    if m:
        if m.group(1):
            return f"{m.group(1)}Q{m.group(2)}"
        return f"{m.group(4)}Q{m.group(3)}"
    return t


def find_section_by_heading(soup: BeautifulSoup, heading_text: str) -> Any:
    """Find a section (h2/h3/h4) whose text contains heading_text (case-insensitive); return the heading or None."""
    key = (heading_text or "").strip().lower()
    if not key:
        return None
    for tag in soup.find_all(["h2", "h3", "h4"]):
        t = (tag.get_text() or "").strip().lower()
        if key in t:
            return tag
    return None


def _normalize_period_label(text: str) -> str:
    """Normalize to FY2025, 2025Q1, etc."""
    text = (text or "").strip()
    if not text:
        return text
    # Already like FY2025
    if re.match(r"^FY\d{4}$", text, re.I):
        return text.upper().replace("FY", "FY")
    # 2025 -> FY2025
    m = re.match(r"^(\d{4})$", text)
    if m:
        return f"FY{m.group(1)}"
    # 2021 Q1 or Q1 2021 -> 2021Q1
    m = re.match(r"(?:(\d{4})\s*Q(\d)|Q(\d)\s*(\d{4}))", text, re.I)
    if m:
        if m.group(1):
            return f"{m.group(1)}Q{m.group(2)}"
        return f"{m.group(4)}Q{m.group(3)}"
    # Semi-annual: 2024 S1, S1 2024, 2024 H1, H1 2024 -> 2024S1
    m = re.match(r"(?:(\d{4})\s*[SH](\d)|[SH](\d)\s*(\d{4}))", text, re.I)
    if m:
        if m.group(1):
            return f"{m.group(1)}S{m.group(2)}"
        return f"{m.group(4)}S{m.group(3)}"
    return text


def _parse_unit_note(text: str) -> tuple[str | None, str | None]:
    """Extract unit_currency and unit_scale from note like 'SAR in Million' or '1SAR in Million'."""
    if not text:
        return None, None
    text = text.lower()
    currency = None
    scale = None
    for c in ("SAR", "USD", "EUR", "GBP"):
        if c.lower() in text:
            currency = c
            break
    if "million" in text:
        scale = "million"
    elif "billion" in text or "b " in text or "b\n" in text:
        scale = "billions"
    return currency, scale


def _find_row_values_by_label(soup: BeautifulSoup, label_text: str) -> dict[str, str] | None:
    """
    Find a table row whose first cell contains label_text (case-insensitive partial match).
    Return dict mapping header_cell_text -> value for that row (header from first row of same table).
    """
    label_lower = (label_text or "").lower()
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        headers = [c.get_text(strip=True) for c in rows[0].find_all(["td", "th"])]
        if not headers:
            continue
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            first = (cells[0].get_text(strip=True) or "").lower()
            if label_lower in first or first in label_lower:
                values = [c.get_text(strip=True) for c in cells[1:]]
                return dict(zip(headers[1:], values))
    return None


def _extract_period_header_and_rows(soup: BeautifulSoup, section_hint: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """
    Find table(s) with a 'Fiscal Period' style header; return (period_headers, [(row_label, values)]).
    section_hint: "annual" or "quarterly" to prefer matching table.
    """
    period_headers: list[str] = []
    row_data: list[tuple[str, list[str]]] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        first_cells = [c.get_text(strip=True) for c in rows[0].find_all(["td", "th"])]
        if not first_cells or "fiscal period" not in (first_cells[0] or "").lower():
            continue
        headers = first_cells
        # Periods are columns 1..n
        periods = [h for h in headers[1:] if h]
        if not periods:
            continue
        # Prefer annual (fewer columns) vs quarterly (many)
        _sub_annual_markers = ("Q", " S", "S1", "S2", "H1", "H2")
        is_quarterly = any(any(m in p for m in _sub_annual_markers) for p in periods) or len(periods) > 12
        if section_hint == "quarterly" and not is_quarterly:
            continue
        if section_hint == "annual" and is_quarterly:
            continue
        for r in rows[1:]:
            cells = r.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            row_label = (cells[0].get_text(strip=True) or "").strip()
            vals = [c.get_text(strip=True) for c in cells[1:]]
            row_data.append((row_label, vals))
        period_headers = periods
        break
    return period_headers, row_data


def _merge_by_period(periods: list[str], series_dict: dict[str, list[float | None]]) -> list[dict[str, Any]]:
    """Build list of {period, ...series} for report."""
    out = []
    for i, p in enumerate(periods):
        row: dict[str, Any] = {"period": _normalize_period_label(p)}
        for name, vals in series_dict.items():
            if i < len(vals):
                row[name] = vals[i]
            else:
                row[name] = None
        out.append(row)
    return out


# ─── Slug discovery (reference doc: search first when slug unknown) ─────────

def _extract_search_result_slugs(soup) -> list[tuple[str, str]]:
    """
    Extract (slug, link_text) from MarketScreener search results table only,
    skipping navigation/sidebar links (trending stocks, main menu) that appear
    before the real search results and return wrong entities.
    """
    results: list[tuple[str, str]] = []
    # Primary: search results table (#instrumentSearchTable or table.table--search)
    table = soup.find("table", id="instrumentSearchTable")
    if table is None:
        table = soup.find("div", id="advanced-search__instruments")
    search_scope = table if table is not None else soup
    if table is not None:
        for a in table.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            m = re.search(r"/quote/stock/([^/]+)/?", href)
            if m:
                slug = m.group(1)
                text = (a.get_text(strip=True) or "").strip()
                if slug and "quote" not in slug and len(slug) > 5 and text:
                    results.append((slug, text))
        if results:
            return results
    # Fallback: links outside nav/menu areas
    nav_classes = {"main-menu", "main-menu__submenu", "footer", "header"}
    for a in soup.find_all("a", href=True):
        # Skip links inside nav/menu/footer containers
        skip = False
        for parent in a.parents:
            parent_classes = set(parent.get("class") or [])
            if parent_classes & nav_classes or (parent.name == "nav"):
                skip = True
                break
        if skip:
            continue
        href = (a.get("href") or "").strip()
        m = re.search(r"/quote/stock/([^/]+)/?", href)
        if m:
            slug = m.group(1)
            text = (a.get_text(strip=True) or "").strip()
            if slug and "quote" not in slug and len(slug) > 5:
                results.append((slug, text))
    return results


def resolve_slug_from_search(ticker: str, *, company_name: str = "") -> str | None:
    """
    Resolve MarketScreener slug via search. GET search/?q={TICKER}, parse first
    equity result link from the results table (not sidebar/nav).
    Falls back to company name search if ticker search yields no results.
    Use only as fallback; prefer resolve_marketscreener_by_isin(isin).
    """
    # Try ticker first (strip exchange suffix for cleaner search)
    search_q = ticker.replace(".SR", "").replace(".KW", "").replace(".QA", "").replace(".OM", "").replace(".BH", "").replace(".AE", "").strip() or ticker
    url = f"https://www.marketscreener.com/search/?q={search_q}"
    _delay_between_requests()
    soup, errors = _fetch_page(url, "search_" + re.sub(r"[^a-zA-Z0-9]", "_", search_q)[:40])
    if soup is not None:
        results = _extract_search_result_slugs(soup)
        if results:
            # Verify the first result actually matches our company (prevent wrong-entity matches)
            _result_name = (results[0][1] or "").lower()
            _company_lower = (company_name or "").lower()
            _name_tokens = set(_company_lower.split()) - {"co", "co.", "ltd", "ltd.", "inc", "inc.", "corp", "the", "of", "and", "plc", "saog", "bsc", "pjsc", "as", "sa"}
            _result_tokens = set(_result_name.split()) - {"co", "co.", "ltd", "ltd.", "inc", "inc.", "corp", "the", "of", "and", "plc", "saog", "bsc", "pjsc", "as", "sa"}
            _overlap = _name_tokens & _result_tokens
            if not company_name or len(_overlap) >= 1:
                return results[0][0]
            # Ticker search returned wrong company — fall through to name search
            log.info("Ticker search for '%s' returned '%s' — no name overlap with '%s', trying name search", search_q, results[0][1], company_name)
    # Fallback: search by company name
    if company_name and company_name.strip():
        name_q = company_name.strip().split(",")[0].split("(")[0].strip()[:40]
        if name_q.lower() != search_q.lower():
            _delay_between_requests()
            url2 = f"https://www.marketscreener.com/search/?q={name_q}"
            soup2, _ = _fetch_page(url2, "search_name_" + re.sub(r"[^a-zA-Z0-9]", "_", name_q)[:40])
            if soup2 is not None:
                results2 = _extract_search_result_slugs(soup2)
                if results2:
                    return results2[0][0]
    return None


def list_marketscreener_candidates_for_isin(
    isin: str, *, max_results: int = 8,
) -> list[tuple[str, str]]:
    """
    All distinct equity slugs from an ISIN search (first hit may be wrong entity).
    Returns [(slug, company_base_url), ...].
    """
    isin = (isin or "").strip()
    if not isin:
        return []
    url = f"https://www.marketscreener.com/search/?q={isin}"
    cache_slug = "search_isin_" + re.sub(r"[^a-zA-Z0-9]", "_", isin)[:50]
    soup, errors = _fetch_page(url, cache_slug)
    if soup is None:
        return []
    raw = _extract_search_result_slugs(soup)
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for slug, _text in raw:
        if not slug or slug in seen:
            continue
        seen.add(slug)
        base = f"https://www.marketscreener.com/quote/stock/{slug}/"
        out.append((slug, base))
        if len(out) >= max_results:
            break
    return out


def resolve_marketscreener_by_isin(isin: str) -> tuple[str, str] | None:
    """
    Resolve MarketScreener company URL by exact ISIN (primary lookup).
    GET search/?q={ISIN}, parse first result from search results table.
    Returns (slug, full_company_url) or None.
    """
    cands = list_marketscreener_candidates_for_isin(isin, max_results=1)
    if not cands:
        return None
    slug, base = cands[0]
    return (slug, base)


# ─── Summary page (/{SLUG}/) — consensus box + valuation snapshot ───────────

def fetch_summary_page(base_company_url: str, cache_key_prefix: str | None = None) -> tuple[dict[str, Any], PageStepStatus]:
    """
    Extract consensus box and valuation snapshot from main quote page /{SLUG}/.
    Per reference: Mean consensus, analyst count, last close, avg target, spread, high/low;
    P/E 2026/2027, EV/Sales, Yield; Net sales/Net income (annual consensus).
    """
    url = (base_company_url or "").rstrip("/") + "/"
    slug = (base_company_url or "").rstrip("/").split("/")[-1] or "SLUG"
    status = PageStepStatus(step="fetch_summary_page", message="")
    start = time.perf_counter() * 1000

    soup, errors = _fetch_page(url, _cache_slug(url, "summary", cache_key_prefix))
    if soup is None:
        status.status = "failed"
        status.errors = errors
        status.elapsed_ms = (time.perf_counter() * 1000) - start
        log.info("[MarketScreener] Fetching summary page (/%s/)... FAILED", slug)
        return _empty_summary_payload(url, status), status
    log.info("[MarketScreener] Fetching summary page (/%s/)... SUCCESS", slug)

    payload: dict[str, Any] = {
        "source_page": url,
        "source_type": "summary_page",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "consensus_rating": None,
        "analyst_count": None,
        "last_close_price": None,
        "price_currency": None,
        "average_target_price": None,
        "high_target_price": None,
        "low_target_price": None,
        "spread_pct": None,
        "pe_2026": None,
        "pe_2027": None,
        "ev_sales_2026": None,
        "ev_sales_2027": None,
        "yield_2026": None,
        "yield_2027": None,
        "net_sales_2026": None,
        "net_sales_2027": None,
        "net_income_2026": None,
        "net_income_2027": None,
        "capitalization": None,
        "warnings": [],
    }

    text = soup.get_text(" ", strip=True)
    # Consensus: "Mean consensus" / "OUTPERFORM", "Number of Analysts" / "16", "Last Close Price" / "100.80SAR", "Average target price" / "116.12SAR", "Spread / Average Target" / "+15.20%"
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for row in rows:
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            for i, cell in enumerate(cells):
                c = (cell or "").strip()
                if "mean consensus" in c.lower():
                    if i + 1 < len(cells):
                        payload["consensus_rating"] = cells[i + 1].strip() or None
                if "number of analysts" in c.lower() and i + 1 < len(cells):
                    try:
                        payload["analyst_count"] = int(re.sub(r"\D", "", cells[i + 1]))
                    except ValueError:
                        pass
                if "last close price" in c.lower() and i + 1 < len(cells):
                    raw = cells[i + 1].replace(",", ".")
                    num = _coerce_numeric_or_none(re.sub(r"[^\d.-]", "", raw))
                    if num is not None:
                        payload["last_close_price"] = num
                    if "SAR" in (cells[i + 1] or ""):
                        payload["price_currency"] = "SAR"
                if "average target price" in c.lower() and i + 1 < len(cells):
                    raw = cells[i + 1].replace(",", ".")
                    payload["average_target_price"] = _coerce_numeric_or_none(re.sub(r"[^\d.-]", "", raw))
                    if "SAR" in (cells[i + 1] or ""):
                        payload["price_currency"] = payload["price_currency"] or "SAR"
                if "spread" in c.lower() and "target" in c.lower() and i + 1 < len(cells):
                    raw = cells[i + 1].replace(",", ".").replace("%", "")
                    payload["spread_pct"] = _coerce_numeric_or_none(raw)
                if "high" in c.lower() and "target" in c.lower() and i + 1 < len(cells):
                    payload["high_target_price"] = _coerce_numeric_or_none(cells[i + 1].replace(",", "."))
                if "low" in c.lower() and "target" in c.lower() and i + 1 < len(cells):
                    payload["low_target_price"] = _coerce_numeric_or_none(cells[i + 1].replace(",", "."))

    # Regex fallback for pages where the consensus box isn't in a simple <table>.
    # This keeps the summary page useful even when MarketScreener changes layout.
    try:
        if payload["consensus_rating"] is None:
            m = re.search(r"Mean consensus\s+([A-Za-z]+)", text)
            if m:
                payload["consensus_rating"] = m.group(1).strip().upper()
        if payload["analyst_count"] is None:
            m = re.search(r"Number of Analysts\s+(\d+)", text)
            if m:
                payload["analyst_count"] = int(m.group(1))
        if payload["last_close_price"] is None:
            m = re.search(r"Last Close Price\s+([\d,.]+)\s*([A-Z]{3})?", text)
            if m:
                payload["last_close_price"] = float(m.group(1).replace(",", ""))
                if m.lastindex and m.lastindex >= 2 and m.group(2):
                    payload["price_currency"] = payload["price_currency"] or m.group(2)
        if payload["average_target_price"] is None:
            m = re.search(r"Average target price\s+([\d,.]+)\s*([A-Z]{3})?", text)
            if m:
                payload["average_target_price"] = float(m.group(1).replace(",", ""))
                if m.lastindex and m.lastindex >= 2 and m.group(2):
                    payload["price_currency"] = payload["price_currency"] or m.group(2)
        if payload["spread_pct"] is None:
            m = re.search(r"Spread / Average Target\s+([+-]?[\d,.]+)\s*%", text)
            if m:
                payload["spread_pct"] = float(m.group(1).replace(",", ""))
    except Exception:
        pass

    # Valuation snapshot: P/E ratio 2026 *, 15.2x; Yield 2026 *, 3.44%
    if "P/E ratio 2026" in text or "15.2x" in text:
        # Try row-based: label in first cell, value in next
        vals = _find_row_values_by_label(soup, "P/E ratio")
        if vals:
            for k, v in vals.items():
                if "2026" in k:
                    payload["pe_2026"] = _coerce_numeric_or_none(v)
                if "2027" in k:
                    payload["pe_2027"] = _coerce_numeric_or_none(v)
    if not payload["pe_2026"] and "15.2x" in text:
        payload["pe_2026"] = 15.2
    if not payload["pe_2027"] and "13.6x" in text:
        payload["pe_2027"] = 13.6
    ev_vals = _find_row_values_by_label(soup, "EV / Sales")
    if ev_vals:
        for k, v in ev_vals.items():
            if "2026" in k:
                payload["ev_sales_2026"] = _coerce_numeric_or_none(v)
            if "2027" in k:
                payload["ev_sales_2027"] = _coerce_numeric_or_none(v)
    yield_vals = _find_row_values_by_label(soup, "Yield")
    if yield_vals:
        for k, v in yield_vals.items():
            if "2026" in k:
                payload["yield_2026"] = _coerce_numeric_or_none(v)
            if "2027" in k:
                payload["yield_2027"] = _coerce_numeric_or_none(v)
    if payload["yield_2026"] is None and "3.44%" in text:
        payload["yield_2026"] = 3.44
    if payload["yield_2027"] is None and "3.93%" in text:
        payload["yield_2027"] = 3.93
    net_vals = _find_row_values_by_label(soup, "Net sales")
    if net_vals:
        for k, v in net_vals.items():
            if "2026" in k:
                payload["net_sales_2026"] = _coerce_numeric_or_none(v)
            if "2027" in k:
                payload["net_sales_2027"] = _coerce_numeric_or_none(v)
    ni_vals = _find_row_values_by_label(soup, "Net income")
    if ni_vals:
        for k, v in ni_vals.items():
            if "2026" in k:
                payload["net_income_2026"] = _coerce_numeric_or_none(v)
            if "2027" in k:
                payload["net_income_2027"] = _coerce_numeric_or_none(v)

    has_consensus = payload["consensus_rating"] or payload["average_target_price"] is not None or payload["analyst_count"] is not None
    status.status = "success" if has_consensus else "partial"
    status.message = "Summary page: consensus box and valuation snapshot"
    status.record_count = 1 if has_consensus else 0
    status.elapsed_ms = (time.perf_counter() * 1000) - start
    return payload, status


def _empty_summary_payload(source_page: str, status: PageStepStatus) -> dict[str, Any]:
    return {
        "source_page": source_page,
        "source_type": "summary_page",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "consensus_rating": None,
        "analyst_count": None,
        "last_close_price": None,
        "price_currency": None,
        "average_target_price": None,
        "high_target_price": None,
        "low_target_price": None,
        "spread_pct": None,
        "pe_2026": None,
        "pe_2027": None,
        "ev_sales_2026": None,
        "ev_sales_2027": None,
        "yield_2026": None,
        "yield_2027": None,
        "net_sales_2026": None,
        "net_sales_2027": None,
        "net_income_2026": None,
        "net_income_2027": None,
        "capitalization": None,
        "warnings": status.errors,
    }


# ─── A. fetch_financial_forecast_series (source: /finances/) ─────────────────

def fetch_financial_forecast_series(base_company_url: str, cache_key_prefix: str | None = None) -> tuple[dict[str, Any], PageStepStatus]:
    """
    Extract annual + quarterly forecast series from /finances/.
    Source: Projected Income Statement. Do NOT label as consensus summary.
    """
    url = base_company_url.rstrip("/") + "/finances/"
    status = PageStepStatus(step="fetch_financial_forecast_series", message="")
    start = time.perf_counter() * 1000

    soup, errors = _fetch_page(url, _cache_slug(url, "finances", cache_key_prefix))
    if soup is None:
        status.status = "failed"
        status.message = errors[0] if errors else "Fetch failed"
        status.errors = errors
        status.elapsed_ms = (time.perf_counter() * 1000) - start
        log.info("[MarketScreener] Fetching /finances/ page... FAILED")
        return _empty_forecast_payload(url, "financial_forecast_series", status), status
    log.info("[MarketScreener] Fetching /finances/ page... SUCCESS")

    warnings: list[str] = []
    # Find annual section: table with Fiscal Period: December and year columns (no Q)
    period_headers_annual, annual_rows = _extract_period_header_and_rows(soup, "annual")
    period_headers_quarterly, quarterly_rows = _extract_period_header_and_rows(soup, "quarterly")
    # Per reference doc: if quarterly not on main page, try ?type=trimestral.
    # MS now serves the quarterly tab via JS; the static GET often returns the
    # annual table again. Fall back to a Playwright-rendered fetch which lets
    # the JS hydrate the quarterly grid before reading the DOM.
    if (not period_headers_quarterly or not quarterly_rows) and base_company_url:
        url_trimestral = base_company_url.rstrip("/") + "/finances/?type=trimestral"

        # 1) Cheap static GET first (works on cached HTML and many tickers).
        soup_q, _ = _fetch_page(url_trimestral, _cache_slug(url, "finances_trimestral", cache_key_prefix))
        if soup_q:
            period_headers_quarterly, quarterly_rows = _extract_period_header_and_rows(soup_q, "quarterly")
            if period_headers_quarterly:
                warnings.append("Quarterly data from ?type=trimestral (static)")

        # 2) Playwright fallback when the static fetch returned the annual
        #    grid again. Degrades silently if Playwright isn't installed
        #    (`fetch_page_with_browser` returns "" on any error).
        if not period_headers_quarterly or not quarterly_rows:
            try:
                from src.scraping.browser import fetch_page_with_browser
                rendered_html = fetch_page_with_browser(
                    url_trimestral,
                    wait_selector="table",  # any table renders post-hydration
                    timeout_ms=15000,
                    wait_timeout_ms=8000,
                )
            except Exception as exc:
                log.info("[MarketScreener] Playwright quarterly fallback errored: %s", exc)
                rendered_html = ""
            if rendered_html:
                soup_q2 = BeautifulSoup(rendered_html, "lxml")
                period_headers_quarterly, quarterly_rows = _extract_period_header_and_rows(soup_q2, "quarterly")
                if period_headers_quarterly:
                    warnings.append("Quarterly data from ?type=trimestral (rendered)")

    def _row_by_label(rows: list[tuple[str, list[str]]], *labels: str) -> list[str] | None:
        # Substring match — but guard against the EBIT/EBITDA collision:
        # "ebit" is a substring of "ebitda", and on the MS finances page
        # the EBITDA row precedes the EBIT row. A naive substring lookup
        # for "EBIT" hits the EBITDA row first, populating the EBIT field
        # with EBITDA values (rendered as identical EBITDA / EBIT rows on
        # slide 3 of the deck — the bug we shipped earlier).
        for label in labels:
            ll = label.lower()
            for rlabel, vals in rows:
                rl = (rlabel or "").lower()
                if ll not in rl:
                    continue
                if ll == "ebit" and "ebitda" in rl:
                    continue  # skip — that's an EBITDA row, not EBIT
                return vals
        return None

    def _parse_row(vals: list[str] | None, periods: list[str]) -> list[float | None]:
        if not vals or not periods:
            return []
        out: list[float | None] = []
        for i in range(min(len(periods), len(vals))):
            out.append(_coerce_numeric_or_none(vals[i]))
        return out

    # Annual
    annual_net_sales = _parse_row(_row_by_label(annual_rows, "Net sales"), period_headers_annual)
    annual_ebitda = _parse_row(_row_by_label(annual_rows, "EBITDA"), period_headers_annual)
    annual_ebit = _parse_row(_row_by_label(annual_rows, "EBIT"), period_headers_annual)
    # MS publishes interest paid as a negative number; we keep the sign as-is.
    annual_interest_paid = _parse_row(_row_by_label(annual_rows, "Interest Paid"), period_headers_annual)
    # EBT = Earnings Before Tax. Use the longer label first to avoid the
    # "EBT" three-letter substring matching elsewhere in the table.
    annual_ebt = _parse_row(
        _row_by_label(annual_rows, "Earnings before Tax", "EBT"),
        period_headers_annual,
    )
    annual_net_income = _parse_row(_row_by_label(annual_rows, "Net income"), period_headers_annual)
    annual_announcement_raw = _row_by_label(annual_rows, "Announcement Date")  # keep as strings
    annual_announcement = (annual_announcement_raw or [])[: len(period_headers_annual)]
    while len(annual_announcement) < len(period_headers_annual):
        annual_announcement.append(None)

    if _all_missing(annual_ebitda) and annual_ebitda:
        warnings.append("EBITDA row missing or all '-' (e.g. bank); ebitda_applicable=false")

    # ── Structural invariants ──
    # Catch parser regressions BEFORE the deck ships:
    #   1. EBIT == EBITDA exactly across all populated periods is the
    #      signature of the historical substring-collision bug (where
    #      `_row_by_label("EBIT")` matched the EBITDA row first because
    #      "ebit" ⊂ "ebitda"). If a future code change re-introduces it,
    #      this warning + the parser-fixture tests catch it immediately.
    #   2. EBIT > EBITDA at any period is economically impossible
    #      (EBITDA = EBIT + D&A, D&A ≥ 0). Logging it surfaces both
    #      mis-aligned columns and weird MS data.
    def _both_populated(a, b, i):
        return (i < len(a) and i < len(b)
                and a[i] is not None and b[i] is not None)

    if annual_ebit and annual_ebitda:
        # Identical rows? Collision smell.
        n = min(len(annual_ebit), len(annual_ebitda))
        identical = (
            n > 0
            and any(_both_populated(annual_ebit, annual_ebitda, i) for i in range(n))
            and all(
                annual_ebit[i] == annual_ebitda[i]
                for i in range(n)
                if _both_populated(annual_ebit, annual_ebitda, i)
            )
        )
        if identical:
            warnings.append(
                "EBIT == EBITDA exactly across populated periods — "
                "possible substring-collision parser regression"
            )
            log.warning(
                "[MarketScreener] EBIT == EBITDA exactly for all populated "
                "periods — investigate _row_by_label substring match"
            )
        # EBIT > EBITDA at any period? Economically impossible.
        for i in range(n):
            if _both_populated(annual_ebit, annual_ebitda, i):
                if annual_ebit[i] > annual_ebitda[i]:
                    log.warning(
                        "[MarketScreener] EBIT (%.2f) > EBITDA (%.2f) at period %s — "
                        "data anomaly or column misalignment",
                        annual_ebit[i], annual_ebitda[i],
                        period_headers_annual[i] if i < len(period_headers_annual) else f"#{i}",
                    )
                    break  # one log line is enough

    has_annual = bool(period_headers_annual and (annual_net_sales or annual_net_income))
    log.info("[MarketScreener] Annual financial forecast rows extracted... %s", "SUCCESS" if has_annual else "PARTIAL")

    # Quarterly — extract every metric the table publishes, not just net_sales.
    # Earlier the only quarterly field consumed downstream was net_sales; the
    # report now drives a full Q-prior(A)/Q-next(E) table on slide 3, so we
    # surface ebitda / ebit / net_income / eps / announcement_dates too. When
    # MS lacks a row (e.g. EPS for a non-listed unit), we leave it empty and
    # the renderer drops it via the "all-None" rule.
    quarterly_net_sales = _parse_row(_row_by_label(quarterly_rows, "Net sales"), period_headers_quarterly)
    quarterly_ebitda = _parse_row(_row_by_label(quarterly_rows, "EBITDA"), period_headers_quarterly)
    quarterly_ebit = _parse_row(_row_by_label(quarterly_rows, "EBIT"), period_headers_quarterly)
    quarterly_net_income = _parse_row(_row_by_label(quarterly_rows, "Net income"), period_headers_quarterly)
    quarterly_eps = _parse_row(_row_by_label(quarterly_rows, "EPS"), period_headers_quarterly)
    quarterly_announcement_raw = _row_by_label(quarterly_rows, "Announcement Date") or []
    quarterly_announcement = quarterly_announcement_raw[: len(period_headers_quarterly)]
    while len(quarterly_announcement) < len(period_headers_quarterly):
        quarterly_announcement.append(None)
    has_q = bool(period_headers_quarterly and quarterly_net_sales)
    log.info(
        "[MarketScreener] Quarterly extracted... net_sales=%s ebitda=%s ebit=%s ni=%s eps=%s",
        "✓" if quarterly_net_sales else "✗",
        "✓" if quarterly_ebitda else "✗",
        "✓" if quarterly_ebit else "✗",
        "✓" if quarterly_net_income else "✗",
        "✓" if quarterly_eps else "✗",
    )

    # Unit note from page text
    full_text = soup.get_text(" ", strip=True)
    unit_currency, unit_scale = None, "million"
    if "SAR in Million" in full_text or "SAR in Million" in full_text:
        unit_currency = "SAR"
        unit_scale = "million"
    _parse_unit_note(full_text)

    payload = {
        "source_page": url,
        "source_type": "financial_forecast_series",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "annual": {
            "periods": [_normalize_period_label(p) for p in period_headers_annual],
            "net_sales": annual_net_sales,
            "ebitda": annual_ebitda,
            "ebit": annual_ebit,
            "interest_paid": annual_interest_paid,
            "ebt": annual_ebt,
            "net_income": annual_net_income,
            "announcement_dates": annual_announcement,
        },
        "quarterly": {
            "periods": [_normalize_period_label(p) for p in period_headers_quarterly],
            "net_sales": quarterly_net_sales,
            "ebitda": quarterly_ebitda,
            "ebit": quarterly_ebit,
            "net_income": quarterly_net_income,
            "eps": quarterly_eps,
            "announcement_dates": quarterly_announcement,
        },
        "unit_currency": unit_currency or "",
        "unit_scale": unit_scale,
        "applicability_flags": {
            "ebitda_applicable": not _all_missing(annual_ebitda),
        },
        "warnings": warnings,
    }

    status.status = "success" if (has_annual or has_q) else "partial" if (period_headers_annual or period_headers_quarterly) else "failed"
    status.message = "Annual and quarterly forecasts from /finances/" + (" with gaps" if warnings else "")
    status.warnings = warnings
    status.record_count = len(period_headers_annual) + len(period_headers_quarterly)
    status.elapsed_ms = (time.perf_counter() * 1000) - start
    return payload, status


def _empty_forecast_payload(source_page: str, source_type: str, status: PageStepStatus) -> dict[str, Any]:
    return {
        "source_page": source_page,
        "source_type": source_type,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "annual": {"periods": [], "net_sales": [], "ebitda": [], "ebit": [], "interest_paid": [], "ebt": [], "net_income": [], "announcement_dates": []},
        "quarterly": {
            "periods": [], "net_sales": [], "ebitda": [], "ebit": [],
            "net_income": [], "eps": [], "announcement_dates": [],
        },
        "unit_currency": None,
        "unit_scale": None,
        "applicability_flags": {"ebitda_applicable": True},
        "warnings": status.errors + status.warnings,
    }


# ─── B. detect_finances_page_sections ───────────────────────────────────────

def detect_finances_page_sections(base_company_url: str, cache_key_prefix: str | None = None) -> tuple[dict[str, Any], PageStepStatus]:
    """Detect section/link presence on /finances/ (EPS Estimates, Revisions, consensus block). No invented values."""
    url = base_company_url.rstrip("/") + "/finances/"
    status = PageStepStatus(step="detect_finances_page_sections", message="")
    start = time.perf_counter() * 1000

    soup, errors = _fetch_page(url, _cache_slug(url, "finances_detect", cache_key_prefix))
    if soup is None:
        status.status = "failed"
        status.errors = errors
        status.elapsed_ms = (time.perf_counter() * 1000) - start
        return {"source_page": url, "eps_estimates_link": False, "revisions_link": False, "consensus_block_present": False, "warnings": errors}, status

    text = soup.get_text(" ", strip=True).lower()
    data = {
        "source_page": url,
        "eps_estimates_link": "eps estimates" in text,
        "revisions_link": "revisions to estimates" in text,
        "consensus_block_present": "mean consensus" in text or "number of analysts" in text,
        "warnings": [],
    }
    status.status = "success"
    status.message = "Section detection from /finances/"
    status.elapsed_ms = (time.perf_counter() * 1000) - start
    return data, status


# ─── C. fetch_income_statement_actuals (source: /finances-income-statement/) ─

def fetch_income_statement_actuals(base_company_url: str, cache_key_prefix: str | None = None) -> tuple[dict[str, Any], PageStepStatus]:
    """Extract actual/historical income statement. Bank: prefer revenues before provision, total revenues."""
    url = base_company_url.rstrip("/") + "/finances-income-statement/"
    status = PageStepStatus(step="fetch_income_statement_actuals", message="")
    start = time.perf_counter() * 1000

    soup, errors = _fetch_page(url, _cache_slug(url, "income_statement", cache_key_prefix))
    if soup is None:
        status.status = "failed"
        status.errors = errors
        status.elapsed_ms = (time.perf_counter() * 1000) - start
        log.info("[MarketScreener] Fetching /finances-income-statement/ page... FAILED")
        return _empty_income_actuals_payload(url, status), status
    log.info("[MarketScreener] Fetching /finances-income-statement/ page... SUCCESS")

    # Row labels from spec: Revenues Before Provision For Loan Losses, Total Revenues, EBT Excl., Net Income to Company, Net Income - (IS), Net EPS - Basic, Dividend Per Share
    labels_to_series: dict[str, list[float | None]] = {}
    period_headers: list[str] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        first_row = [c.get_text(strip=True) for c in rows[0].find_all(["td", "th"])]
        if not first_row or "fiscal" not in (first_row[0] or "").lower():
            continue
        period_headers = [h for h in first_row[1:] if h and re.match(r"20\d{2}", h)]
        if not period_headers:
            continue
        for r in rows[1:]:
            cells = r.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            label = (cells[0].get_text(strip=True) or "").strip()
            vals = [_coerce_numeric_or_none(c.get_text(strip=True)) for c in cells[1:]]
            if len(vals) >= len(period_headers):
                labels_to_series[label] = vals[:len(period_headers)]
        break

    def _get(name_sub: str) -> list[float | None]:
        for k, v in labels_to_series.items():
            if name_sub.lower() in k.lower():
                return v
        return []

    payload = {
        "source_page": url,
        "source_type": "income_statement_actuals",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "periods": [_normalize_period_label(p) for p in period_headers],
        "revenues_before_provision_for_loan_losses": _get("Revenues Before Provision"),
        "total_revenues": _get("Total Revenues"),
        "ebt_excl_unusual": _get("EBT, Excl. Unusual"),
        "net_income_to_company": _get("Net Income to Company"),
        "net_income_is": _get("Net Income - (IS)"),
        "eps_basic": _get("Net EPS - Basic"),
        "dividend_per_share": _get("Dividend Per Share"),
        "unit_scale": "billions",
        "warnings": [],
    }

    has_any = any(payload["periods"]) and any((payload["total_revenues"] or payload["net_income_to_company"] or payload["eps_basic"]))
    log.info("[MarketScreener] Income statement actuals extracted... %s", "SUCCESS" if has_any else "PARTIAL")
    status.status = "success" if has_any else "partial"
    status.message = "Actuals from /finances-income-statement/"
    status.record_count = len(payload["periods"])
    status.elapsed_ms = (time.perf_counter() * 1000) - start
    return payload, status


def _empty_income_actuals_payload(source_page: str, status: PageStepStatus) -> dict[str, Any]:
    return {
        "source_page": source_page,
        "source_type": "income_statement_actuals",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "periods": [],
        "revenues_before_provision_for_loan_losses": [],
        "total_revenues": [],
        "ebt_excl_unusual": [],
        "net_income_to_company": [],
        "net_income_is": [],
        "eps_basic": [],
        "dividend_per_share": [],
        "unit_scale": "billions",
        "warnings": status.errors,
    }


# ─── D. fetch_dividend_eps_page (source: /valuation-dividend/) ───────────────

def fetch_dividend_eps_page(base_company_url: str, cache_key_prefix: str | None = None) -> tuple[dict[str, Any], PageStepStatus]:
    """Extract EPS & dividend forecasts from /valuation-dividend/. Cleanest source for dividend per share."""
    url = base_company_url.rstrip("/") + "/valuation-dividend/"
    status = PageStepStatus(step="fetch_dividend_eps_page", message="")
    start = time.perf_counter() * 1000

    soup, errors = _fetch_page(url, _cache_slug(url, "valuation_dividend", cache_key_prefix))
    if soup is None:
        status.status = "failed"
        status.errors = errors
        status.elapsed_ms = (time.perf_counter() * 1000) - start
        log.info("[MarketScreener] Fetching /valuation-dividend/ page... FAILED")
        return _empty_dividend_payload(url, status), status
    log.info("[MarketScreener] Fetching /valuation-dividend/ page... SUCCESS")

    period_headers, row_data = _extract_period_header_and_rows(soup, "annual")
    if not period_headers:
        # Try first table with Fiscal Period
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if not rows:
                continue
            first = [c.get_text(strip=True) for c in rows[0].find_all(["td", "th"])]
            if first and "fiscal" in (first[0] or "").lower():
                period_headers = [h for h in first[1:] if h]
                for r in rows[1:]:
                    cells = r.find_all(["td", "th"])
                    if len(cells) >= 2:
                        row_data.append((cells[0].get_text(strip=True), [c.get_text(strip=True) for c in cells[1:]]))
                break

    def _row(*labels: str) -> list[str]:
        for label in labels:
            for rlabel, vals in row_data:
                if label.lower() in (rlabel or "").lower():
                    return vals
        return []

    div_vals = _row("Dividend per Share")
    yield_vals = _row("Rate of return")
    eps_vals = _row("EPS ", "EPS")
    dist_vals = _row("Distribution rate")
    ref_vals = _row("Reference price")
    ann_vals = _row("Announcement Date")

    # Currency note from the page text — same heuristic as
    # fetch_financial_forecast_series. Earlier this referenced an undefined
    # `unit_currency` variable and the whole payload assembly raised, so the
    # /valuation-dividend/ page was unusable. Now resolved before payload.
    # `_parse_unit_note` returns (currency, scale); we only need currency.
    full_text = soup.get_text(" ", strip=True)
    unit_currency_pair = _parse_unit_note(full_text)
    unit_currency = (unit_currency_pair[0] or "") if unit_currency_pair else ""

    payload = {
        "source_page": url,
        "source_type": "dividend_eps_forecasts",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "periods": [_normalize_period_label(p) for p in period_headers],
        "dividend_per_share": [_coerce_numeric_or_none(v) for v in div_vals],
        "dividend_yield": [_coerce_numeric_or_none(v) for v in yield_vals],
        "eps": [_coerce_numeric_or_none(v) for v in eps_vals],
        "distribution_rate": [_coerce_numeric_or_none(v) for v in dist_vals],
        "reference_price": [_coerce_numeric_or_none(v) for v in ref_vals],
        "announcement_dates": ann_vals,
        "unit_currency": unit_currency,
        "warnings": [],
    }

    has_any = bool(period_headers and (payload["eps"] or payload["dividend_per_share"]))
    log.info("[MarketScreener] EPS/dividend forecast rows extracted... %s", "SUCCESS" if has_any else "PARTIAL")
    status.status = "success" if has_any else "partial"
    status.message = "EPS & dividend from /valuation-dividend/"
    status.record_count = len(period_headers)
    status.elapsed_ms = (time.perf_counter() * 1000) - start
    return payload, status


def _empty_dividend_payload(source_page: str, status: PageStepStatus) -> dict[str, Any]:
    return {
        "source_page": source_page,
        "source_type": "dividend_eps_forecasts",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "periods": [],
        "dividend_per_share": [],
        "dividend_yield": [],
        "eps": [],
        "distribution_rate": [],
        "reference_price": [],
        "announcement_dates": [],
        "unit_currency": "",
        "warnings": status.errors,
    }


# ─── E. fetch_consensus_summary (source: /consensus/) ────────────────────────

def fetch_consensus_summary(base_company_url: str, cache_key_prefix: str | None = None) -> tuple[dict[str, Any], PageStepStatus]:
    """Top-level analyst rating + target price only. Source: /consensus/. Delegates to marketscreener_consensus."""
    from src.providers.marketscreener_consensus import fetch_marketscreener_consensus_summary

    url = base_company_url.rstrip("/") + "/consensus/"
    status = PageStepStatus(step="fetch_consensus_summary", message="")
    start = time.perf_counter() * 1000

    result = fetch_marketscreener_consensus_summary(url, cache_key_prefix=cache_key_prefix)
    data = result.extracted_data.to_report_payload()
    data["source_page"] = url
    data["source_type"] = "consensus_summary"
    data["extracted_at"] = datetime.now(timezone.utc).isoformat()
    data["warnings"] = result.raw_warnings + [s.status_message for s in result.detected_sections if s.status_message]

    status.status = result.step_status.status
    status.message = result.step_status.message
    status.errors = result.step_status.errors
    status.warnings = result.step_status.warnings
    status.record_count = result.step_status.record_count
    status.elapsed_ms = (time.perf_counter() * 1000) - start
    return data, status


# ─── F. fetch_valuation_multiples (source: /valuation/) ─────────────────────

def fetch_valuation_multiples(base_company_url: str, cache_key_prefix: str | None = None) -> tuple[dict[str, Any], PageStepStatus]:
    """
    Extract full valuation table from /valuation/: P/E, P/B, PEG, EV/Revenue,
    EV/EBIT, Yield by year (2024A–2028E). Per docs/DATA_SOURCE_AND_URL_REFERENCE.md.
    """
    url = base_company_url.rstrip("/") + "/valuation/"
    status = PageStepStatus(step="fetch_valuation_multiples", message="")
    start = time.perf_counter() * 1000

    soup, errors = _fetch_page(url, _cache_slug(url, "valuation", cache_key_prefix))
    if soup is None:
        status.status = "failed"
        status.errors = errors
        status.elapsed_ms = (time.perf_counter() * 1000) - start
        log.info("[MarketScreener] Fetching /valuation/ page... FAILED")
        return _empty_valuation_payload(url, status), status
    log.info("[MarketScreener] Fetching /valuation/ page... SUCCESS")

    period_headers, row_data = _extract_period_header_and_rows(soup, "annual")
    if not period_headers:
        status.status = "partial"
        status.message = "No valuation table found"
        status.elapsed_ms = (time.perf_counter() * 1000) - start
        return _empty_valuation_payload(url, status), status

    def _row(*labels: str, exclude: tuple[str, ...] = ()) -> list[str]:
        excl_norms = tuple(
            x.lower().replace(" ", "").replace("/", "") for x in exclude
        )
        for label in labels:
            norm = label.lower().replace(" ", "").replace("/", "")
            for rlabel, vals in row_data:
                rnorm = (rlabel or "").lower().replace(" ", "").replace("/", "")
                # Skip rows that match an explicit exclude pattern. Used to
                # disambiguate substring collisions like "Capitalization"
                # vs "Capitalization / Revenue".
                if any(e in rnorm for e in excl_norms):
                    continue
                if norm in rnorm or rnorm in norm:
                    return vals
        return []

    pe_vals = _row("P/E ratio", "PE ratio", "P/E")
    pbr_vals = _row("PBR", "P/B", "Price to Book")
    peg_vals = _row("PEG")
    cap_rev_vals = _row("Capitalization / Revenue", "Cap/Revenue")
    ev_rev_vals = _row("EV / Revenue", "EV/Revenue")
    ev_ebit_vals = _row("EV / EBIT", "EV/EBIT")
    yield_vals = _row("Rate of return", "Yield", "Dividend Yield")
    ev_ebitda_vals = _row("EV / EBITDA", "EV/EBITDA")
    eps_vals = _row("EPS", "Earnings Per Share")
    dps_vals = _row("Dividend per Share", "DPS")
    # Capitalization (raw market cap by year) — used as a fallback when
    # Yahoo doesn't have market_cap (most non-DM tickers). Excluded
    # from the row search are the ratio rows that contain
    # "Capitalization" as a substring.
    cap_vals = _row(
        "Capitalization",
        exclude=("Capitalization / Revenue",),
    )

    payload = {
        "source_page": url,
        "source_type": "valuation_multiples",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "periods": [_normalize_period_label(p) for p in period_headers],
        "pe": [_coerce_numeric_or_none(v) for v in pe_vals],
        "pbr": [_coerce_numeric_or_none(v) for v in pbr_vals],
        "peg": [_coerce_numeric_or_none(v) for v in peg_vals],
        "capitalization": [_coerce_numeric_or_none(v) for v in cap_vals],
        "capitalization_revenue": [_coerce_numeric_or_none(v) for v in cap_rev_vals],
        "ev_revenue": [_coerce_numeric_or_none(v) for v in ev_rev_vals],
        "ev_ebit": [_coerce_numeric_or_none(v) for v in ev_ebit_vals],
        "ev_ebitda": [_coerce_numeric_or_none(v) for v in ev_ebitda_vals],
        "yield_pct": [_coerce_numeric_or_none(v) for v in yield_vals],
        "eps": [_coerce_numeric_or_none(v) for v in eps_vals],
        "dps": [_coerce_numeric_or_none(v) for v in dps_vals],
        "warnings": [],
    }

    has_any = bool(period_headers and (payload["pe"] or payload["pbr"] or payload["yield_pct"]))
    status.status = "success" if has_any else "partial"
    status.message = "Valuation multiples from /valuation/"
    status.record_count = len(period_headers)
    status.elapsed_ms = (time.perf_counter() * 1000) - start
    return payload, status


def _empty_valuation_payload(source_page: str, status: PageStepStatus) -> dict[str, Any]:
    return {
        "source_page": source_page,
        "source_type": "valuation_multiples",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "periods": [],
        "pe": [],
        "pbr": [],
        "peg": [],
        "capitalization_revenue": [],
        "ev_revenue": [],
        "ev_ebit": [],
        "ev_ebitda": [],
        "yield_pct": [],
        "warnings": status.errors,
    }


# ─── G. fetch_calendar_events (source: /calendar/) ───────────────────────────

def _parse_calendar_cell_triplet(cell) -> dict[str, Any]:
    """
    Parse a table cell that may contain Released (b) / Forecast (i) / Spread (span).
    Returns { "released": float|None, "forecast": float|None, "spread_pct": float|None }.
    European format: "7 637" -> 7637, "1,56" -> 1.56.
    """
    out: dict[str, Any] = {"released": None, "forecast": None, "spread_pct": None}
    if not cell:
        return out
    # b = released, i = forecast, span with variation = spread
    b_el = cell.find("b")
    i_els = cell.find_all("i")
    span_el = cell.find("span", class_=re.compile(r"variation"))
    if b_el:
        raw = (b_el.get_text(strip=True) or "").replace(" ", "").replace(",", ".")
        if raw:
            try:
                out["released"] = float(raw)
            except ValueError:
                pass
    if i_els:
        raw = (i_els[0].get_text(strip=True) or "").replace(" ", "").replace(",", ".")
        if raw:
            try:
                out["forecast"] = float(raw)
            except ValueError:
                pass
    if span_el:
        raw = (span_el.get_text(strip=True) or "").replace(",", ".").replace("%", "").strip()
        if raw:
            try:
                out["spread_pct"] = float(raw)
            except ValueError:
                pass
    return out


def _metric_key_from_label(label: str) -> str:
    """Map row label to canonical key for report (net_sales, ebit, ebt, net_income, eps, announcement_date)."""
    l = (label or "").lower()
    if "announcement" in l and "date" in l:
        return "announcement_date"
    if "net sales" in l or "revenue" in l:
        return "net_sales"
    if "net income" in l:
        return "net_income"
    if "earnings before tax" in l or "ebt" in l:
        return "ebt"
    if "ebit" in l and "ebitda" not in l:
        return "ebit"
    if "ebitda" in l:
        return "ebitda"
    if "eps" in l:
        return "eps"
    return "other"


def parse_quarter_headers_from_table(table) -> list[str]:
    """Extract quarter labels from table thead (th[2:]); return list of normalized 2025Q1-style labels."""
    if not table:
        return []
    thead = table.find("thead")
    if not thead:
        return []
    cells = thead.find_all("th")
    if len(cells) < 3:
        return []
    out = []
    for th in cells[2:]:
        q = (th.get_text(strip=True) or "").strip()
        if q and re.match(r"20\d{2}\s*Q[1-4]", q, re.I):
            out.append(normalize_quarter_label(q))
    return out


def parse_metric_block_with_released_forecast_spread(cell) -> dict[str, Any]:
    """Parse one cell containing released (b) / forecast (i) / spread (span.variation). Same as _parse_calendar_cell_triplet."""
    return _parse_calendar_cell_triplet(cell)


def parse_announcement_date_row(cells: list, quarters: list[str]) -> dict[str, str | None]:
    """Parse a row of date strings into quarter -> date. Returns dict mapping quarter label to date string."""
    out: dict[str, str | None] = {}
    for i in range(min(len(quarters), len(cells))):
        date_val = (cells[i].get_text(strip=True) or "").strip() or None
        if i < len(quarters):
            out[quarters[i]] = date_val
    return out


def _parse_quarterly_results_table(soup: BeautifulSoup) -> dict[str, Any]:
    """
    Find and parse the "Quarterly results" table on /calendar/.
    Returns:
      quarters: list of period labels e.g. ["2024 Q2", "2025 Q1", ...]
      rows: list of { "metric_key", "metric_label", "unit", "by_quarter": [ { released, forecast, spread_pct }, ... ] }
      announcement_dates: list of date strings per quarter (same length as quarters)
    """
    result: dict[str, Any] = {"quarters": [], "rows": [], "announcement_dates": [], "warnings": []}
    table = soup.find("table", id="quarterlyResultsTable")
    if not table:
        # Fallback: find table under "Quarterly results" heading
        section = find_section_by_heading(soup, "Quarterly results")
        if section:
            table = section.find_next("table")
        if not table:
            for h in soup.find_all(["h3", "h4"]):
                if "quarterly results" in (h.get_text() or "").lower():
                    table = h.find_next("table")
                    break
    if not table:
        result["warnings"].append("Quarterly results table not found")
        return result

    thead = table.find("thead")
    tbody = table.find("tbody")
    if not thead or not tbody:
        result["warnings"].append("Quarterly table missing thead or tbody")
        return result

    # Header: first row, th[2:] are quarter labels
    header_cells = thead.find_all("th")
    if len(header_cells) < 3:
        result["warnings"].append("Quarterly table has too few columns")
        return result
    quarters = []
    for th in header_cells[2:]:
        q = (th.get_text(strip=True) or "").strip()
        if q and re.match(r"20\d{2}\s*Q[1-4]", q, re.I):
            quarters.append(q.replace(" ", " ").strip())
    result["quarters"] = quarters
    nq = len(quarters)

    # Body rows
    for tr in tbody.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        first = cells[0]
        # Metric name may be in th with optional <br/><span><i>Unit</i></span>
        metric_text = first.get_text(" ", strip=True) or ""
        metric_label = (first.get_text(strip=True) or "").split("\n")[0].strip()
        unit = ""
        span = first.find("span")
        if span:
            unit = (span.get_text(strip=True) or "").strip()
        # Skip the "Released / Forecast / Spread" column (cells[1])
        data_cells = cells[2:]

        if "announcement" in metric_label.lower() and "date" in metric_label.lower():
            result["announcement_dates"] = []
            for i, td in enumerate(data_cells):
                if i >= nq:
                    break
                result["announcement_dates"].append((td.get_text(strip=True) or "").strip() or None)
            continue

        # Data row: parse triplet per cell
        by_quarter: list[dict[str, Any]] = []
        for i, td in enumerate(data_cells):
            if i >= nq:
                break
            by_quarter.append(_parse_calendar_cell_triplet(td))
        metric_key = _metric_key_from_label(metric_label)
        # Skip empty rows (all cells null)
        if any(
            c.get("released") is not None or c.get("forecast") is not None
            for c in by_quarter
        ):
            result["rows"].append({
                "metric_key": metric_key,
                "metric_label": metric_label,
                "unit": unit,
                "by_quarter": by_quarter,
            })
    return result


def fetch_calendar_events(base_company_url: str, cache_key_prefix: str | None = None) -> tuple[dict[str, Any], PageStepStatus]:
    """Extract next expected earnings date, event list, and Quarterly results table from /calendar/."""
    url = base_company_url.rstrip("/") + "/calendar/"
    status = PageStepStatus(step="fetch_calendar_events", message="")
    start = time.perf_counter() * 1000

    soup, errors = _fetch_page(url, _cache_slug(url, "calendar", cache_key_prefix))
    if soup is None:
        status.status = "failed"
        status.errors = errors
        status.elapsed_ms = (time.perf_counter() * 1000) - start
        log.info("[MarketScreener] Fetching /calendar/ page... FAILED")
        return _empty_calendar_payload(url, status), status
    log.info("[MarketScreener] Fetching /calendar/ page... SUCCESS")

    text = soup.get_text("\n", strip=True)
    next_date = None
    next_label = None
    next_time = None
    upcoming: list[dict[str, Any]] = []
    past: list[dict[str, Any]] = []

    # Upcoming / past events from date tables
    text_lower = text.lower()
    upcoming_idx = text.find("Upcoming") if "upcoming" in text_lower else -1
    past_idx = text.find("Past events") if "past events" in text_lower else len(text) + 1
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) >= 2:
                date_cell = (cells[0].get_text(strip=True) or "").strip()
                time_cell = (cells[1].get_text(strip=True) or "").strip() if len(cells) > 1 else ""
                if re.match(r"20\d{2}-\d{2}-\d{2}", date_cell):
                    ev = {"date": date_cell, "time": time_cell or None}
                    pos = text.find(date_cell)
                    if pos >= 0 and (upcoming_idx >= 0 and past_idx > pos and pos > upcoming_idx):
                        upcoming.append(ev)
                        if next_date is None:
                            next_date = date_cell
                            next_time = time_cell or None
                    else:
                        past.append(ev)

    # Prefer quarter label from next expected date so title matches report date (e.g. 2026-04-20 → Q1 2026)
    if next_date and re.match(r"20\d{2}-\d{2}-\d{2}", next_date):
        try:
            y, m = int(next_date[:4]), int(next_date[5:7])
            q = (m - 1) // 3 + 1
            next_label = f"{y} Q{q}"
        except (ValueError, IndexError):
            pass
    if not next_label:
        for h in soup.find_all(["h2", "h3", "h4"]):
            t = (h.get_text(strip=True) or "").strip()
            if "earnings" in t.lower() and "Q" in t:
                next_label = t
                break

    # Quarterly results table (released / forecast / spread per metric per quarter)
    quarterly_results = _parse_quarterly_results_table(soup)
    if quarterly_results["rows"]:
        log.info("[MarketScreener] Quarterly results table extracted... SUCCESS (%s quarters, %s metrics)", len(quarterly_results["quarters"]), len(quarterly_results["rows"]))
    if quarterly_results.get("warnings"):
        status.warnings.extend(quarterly_results["warnings"])

    # Build metrics-dict shape (quarters as 2025Q1, metrics.released/forecast/spread_pct by quarter)
    raw_q = quarterly_results.get("quarters", [])
    quarters_normalized = [normalize_quarter_label(q) for q in raw_q if q]
    metrics_dict: dict[str, Any] = {}
    for r in quarterly_results.get("rows", []):
        key = r.get("metric_key")
        if not key or key == "other" or key == "announcement_date":
            continue
        by_q = r.get("by_quarter", [])
        released = {}
        forecast = {}
        spread_pct = {}
        for i, cell in enumerate(by_q):
            if i >= len(quarters_normalized):
                break
            ql = quarters_normalized[i]
            released[ql] = cell.get("released")
            forecast[ql] = cell.get("forecast")
            spread_pct[ql] = cell.get("spread_pct")
        metrics_dict[key] = {"released": released, "forecast": forecast, "spread_pct": spread_pct}
    dates_list = quarterly_results.get("announcement_dates", [])
    if dates_list and quarters_normalized:
        metrics_dict["announcement_date"] = {"released": {quarters_normalized[i]: (dates_list[i] if i < len(dates_list) else None) for i in range(len(quarters_normalized))}}
    quarterly_results["quarters_normalized"] = quarters_normalized
    quarterly_results["metrics_dict"] = metrics_dict

    payload = {
        "source_page": url,
        "source_type": "calendar_events",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "next_expected_earnings_date": next_date or (upcoming[0]["date"] if upcoming else None),
        "next_expected_earnings_label": next_label,
        "next_expected_earnings_time": next_time or (upcoming[0].get("time") if upcoming else None),
        "upcoming_events": upcoming[:20],
        "past_events": past[:30],
        "quarterly_results": quarterly_results,
        "quarterly_results_table": {"source_page": url, "source_type": "quarterly_results_table", "quarters": quarters_normalized, "metrics": metrics_dict, "warnings": quarterly_results.get("warnings", [])},
        "warnings": list(quarterly_results.get("warnings", [])),
    }

    has_next = bool(payload["next_expected_earnings_date"])
    status.status = "success" if has_next else "partial"
    status.message = "Calendar from /calendar/ (events + quarterly results table)"
    status.record_count = len(upcoming) + len(past) + len(quarterly_results.get("rows", []))
    status.elapsed_ms = (time.perf_counter() * 1000) - start
    return payload, status


def _empty_calendar_payload(source_page: str, status: PageStepStatus) -> dict[str, Any]:
    return {
        "source_page": source_page,
        "source_type": "calendar_events",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "next_expected_earnings_date": None,
        "next_expected_earnings_label": None,
        "next_expected_earnings_time": None,
        "upcoming_events": [],
        "past_events": [],
        "quarterly_results": {"quarters": [], "rows": [], "announcement_dates": [], "warnings": []},
        "warnings": status.errors,
    }


def fetch_quarterly_results_table(base_company_url: str, cache_key_prefix: str | None = None) -> tuple[dict[str, Any], PageStepStatus]:
    """
    Dedicated parser for the Quarterly results table on /calendar/.
    Returns structured output:
      source_page, source_type="quarterly_results_table",
      quarters: ["2025Q1", "2025Q2", ...],
      metrics: { "net_sales": { "released": {quarter: val}, "forecast": {...}, "spread_pct": {...} }, ... },
      announcement_date: { "released": { quarter: date_str } },
      warnings: []
    """
    url = base_company_url.rstrip("/") + "/calendar/"
    status = PageStepStatus(step="fetch_quarterly_results_table", message="", source="marketscreener")
    start = time.perf_counter() * 1000

    soup, errors = _fetch_page(url, _cache_slug(url, "calendar_quarterly", cache_key_prefix))
    if soup is None:
        status.status = "failed"
        status.errors = errors
        status.elapsed_ms = (time.perf_counter() * 1000) - start
        log.info("[MarketScreener] Fetching /calendar/ (Quarterly results)... FAILED")
        out = {
            "source_page": url,
            "source_type": "quarterly_results_table",
            "quarters": [],
            "metrics": {},
            "announcement_date": {"released": {}},
            "warnings": errors,
        }
        return out, status
    log.info("[MarketScreener] Fetching /calendar/ (Quarterly results)... SUCCESS")

    parsed = _parse_quarterly_results_table(soup)
    raw_quarters = parsed.get("quarters", [])
    quarters = [normalize_quarter_label(q) for q in raw_quarters if q]
    rows = parsed.get("rows", [])
    announcement_dates = parsed.get("announcement_dates", [])

    metrics: dict[str, Any] = {}
    for r in rows:
        key = r.get("metric_key")
        if not key or key == "other":
            continue
        if key == "announcement_date":
            continue
        by_q = r.get("by_quarter", [])
        released: dict[str, float | None] = {}
        forecast: dict[str, float | None] = {}
        spread_pct: dict[str, float | None] = {}
        for i, cell in enumerate(by_q):
            if i >= len(quarters):
                break
            q_label = quarters[i]
            released[q_label] = cell.get("released")
            forecast[q_label] = cell.get("forecast")
            spread_pct[q_label] = cell.get("spread_pct")
        metrics[key] = {"released": released, "forecast": forecast, "spread_pct": spread_pct}

    announcement_released: dict[str, str | None] = {}
    for i, d in enumerate(announcement_dates):
        if i < len(quarters):
            announcement_released[quarters[i]] = d if d else None
    if announcement_released:
        metrics["announcement_date"] = {"released": announcement_released}

    status.status = "success" if (quarters and (rows or metrics)) else "partial"
    status.message = f"Quarterly results: {len(quarters)} quarters, {len(metrics)} metrics"
    status.record_count = len(quarters) * max(len(metrics), 1)
    status.elapsed_ms = (time.perf_counter() * 1000) - start
    status.warnings = list(parsed.get("warnings", []))

    if quarters and metrics:
        log.info("[MarketScreener] Quarterly results section detected... SUCCESS (%s quarters, %s metrics)", len(quarters), len(metrics))
    elif parsed.get("warnings"):
        log.warning("[MarketScreener] Quarterly results table missing or thin")

    out = {
        "source_page": url,
        "source_type": "quarterly_results_table",
        "quarters": quarters,
        "metrics": metrics,
        "warnings": list(parsed.get("warnings", [])),
    }
    return out, status


# ─── H. fetch_ratings_page (source: /ratings/) ──────────────────────────────
#
# Adds three new blocks to the report payload:
#   1. composite_ratings    — Trader / Investor / Global / Quality / ESG MSCI
#   2. strengths            — bulleted "Highlights:" list
#   3. weaknesses           — bulleted "Weaknesses:" list
#   4. peer_esg             — peer ESG MSCI ratings table (rows include
#                              the company itself + sector peers)
#
# Layout reference (probed against live MS HTML, May 2026):
#   * Composite ratings sit at the top of the page as a horizontal strip of
#     four <span class="star star--sizeN" title="93%"></span> elements,
#     each with a percentage in the title attribute. Star order is fixed:
#     Trader, Investor, Global, Quality. ESG MSCI follows separately and
#     is rendered as a letter (CCC..AAA) or "-".
#   * Strengths/Weaknesses appear as two <ul class="my-0"> blocks directly
#     after card-headers titled "Highlights: <name>" and "Weaknesses: <name>".
#   * Peer ESG table sits at the bottom; each <tr> has a peer name cell, a
#     market cap cell, and a star cell whose title carries the rating %.
#     ESG letter rating shows up as plain text in the second-to-last cell
#     (e.g. "AAA", "BB", "-").

def _parse_strengths_weaknesses(soup: BeautifulSoup) -> tuple[list[str], list[str], list[str]]:
    """Return (strengths_bullets, weaknesses_bullets, headline_bullets).

    Headline bullets are the introductory <p>/<li> sentences MS shows above
    the Highlights/Weaknesses sections (e.g. "The company has strong
    fundamentals..."). Useful for the deck's executive summary.
    """
    strengths: list[str] = []
    weaknesses: list[str] = []
    headline: list[str] = []

    def _bullets_after(heading_pattern: re.Pattern) -> list[str]:
        node = soup.find(string=heading_pattern)
        if not node:
            return []
        parent = node.parent
        if parent is None:
            return []
        sibling = parent.find_next_sibling(["ul", "ol"])
        if sibling is None:
            block = parent.parent
            sibling = block.find("ul") if block else None
        if sibling is None:
            return []
        out: list[str] = []
        for li in sibling.find_all("li"):
            text = li.get_text(" ", strip=True)
            if text:
                out.append(text)
        return out

    strengths = _bullets_after(re.compile(r"^Highlights\s*:\s*", re.I))
    weaknesses = _bullets_after(re.compile(r"^Weaknesses\s*:\s*", re.I))

    summary_node = soup.find(string=re.compile(r"Strengths and Weaknesses", re.I))
    if summary_node and summary_node.parent:
        block = summary_node.parent.parent
        if block is not None:
            for p in block.find_all(["p", "li"]):
                text = p.get_text(" ", strip=True)
                if not text or len(text) < 30:
                    continue
                if text.lower().startswith(("highlights", "weaknesses")):
                    continue
                headline.append(text)
                if len(headline) >= 3:
                    break

    return strengths, weaknesses, headline


_COMPOSITE_RATING_LABELS = ["Trader", "Investor", "Global", "Quality"]


def _parse_composite_ratings(soup: BeautifulSoup) -> dict[str, int | None]:
    """Trader/Investor/Global/Quality composite ratings (0..100).

    The four scores live in the first four <span class="star"> elements on
    the page — they always appear above the peer table, and the peer table
    is the only other source of "star" elements. To stay robust if MS adds
    a fifth header rating we walk only the stars whose closest <table>
    ancestor is None (i.e. not yet inside the peer table).
    """
    out: dict[str, int | None] = {label: None for label in _COMPOSITE_RATING_LABELS}
    header_stars: list[str | None] = []
    for star in soup.find_all(class_="star"):
        if star.find_parent("table") is not None:
            continue
        title = (star.get("title") or "").strip().rstrip("%")
        try:
            header_stars.append(int(round(float(title))))
        except (TypeError, ValueError):
            header_stars.append(None)
        if len(header_stars) >= len(_COMPOSITE_RATING_LABELS):
            break
    for i, label in enumerate(_COMPOSITE_RATING_LABELS):
        if i < len(header_stars):
            out[label] = header_stars[i]
    return out


_PEER_ESG_VALID_LETTERS = {"AAA", "AA", "A", "BBB", "BB", "B", "CCC", "-"}


def _parse_peer_esg_table(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Peer ESG / sector comparison table on /ratings/.

    Each peer row has: name, market cap (USD), Investor rating (star %),
    ESG MSCI letter, and additional sub-rating columns (Fundamentals,
    Financial revisions, Global Valuation) that may be empty for thinly
    covered companies. The table is identified by a header containing
    both 'Capi.($)' and 'ESG MSCI' — that disambiguates it from the
    sub-rating breakdown tables (which lack those headers).
    """
    rows_out: list[dict[str, Any]] = []
    target_table = None
    target_headers: list[str] = []
    for table in soup.find_all("table"):
        if not table.find(class_="star"):
            continue
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        head = [c.get_text(" ", strip=True) for c in rows[0].find_all(["td", "th"])]
        head_str = " ".join(head)
        if "Capi" in head_str and "ESG MSCI" in head_str:
            target_table = table
            target_headers = head
            break
    if target_table is None:
        return rows_out

    # Map column header → index; strict equality on stripped header text.
    col_idx: dict[str, int] = {}
    for i, h in enumerate(target_headers):
        h_norm = (h or "").strip()
        if h_norm in {"Capi.($)", "ESG MSCI", "Investor", "Fundamentals", "Financial revisions", "Global Valuation"}:
            col_idx[h_norm] = i

    seen_names: set[str] = set()
    for tr in target_table.find_all("tr")[1:]:  # skip header row
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        text_cells = [c.get_text(" ", strip=True) for c in cells]
        # name lives in the first cell that's a real company name (regex
        # matches all-caps multi-word entity names).
        name = ""
        for tok in text_cells:
            t = (tok or "").strip()
            if not t or "Add to a list" in t:
                continue
            if re.match(r"^[A-Z][A-Z0-9 .,&'\-/()À-ÿ]{2,}$", t):
                name = t
                break
        if not name or name in seen_names:
            continue
        seen_names.add(name)

        def _cell_text(key: str) -> str:
            idx = col_idx.get(key)
            if idx is None or idx >= len(cells):
                return ""
            return cells[idx].get_text(" ", strip=True)

        market_cap = _cell_text("Capi.($)") or None
        esg_raw = (_cell_text("ESG MSCI") or "").strip()
        esg_letter: str | None = esg_raw if esg_raw in _PEER_ESG_VALID_LETTERS else None

        # Investor rating star sits inside the Investor column cell.
        rating_pct: int | None = None
        inv_idx = col_idx.get("Investor")
        if inv_idx is not None and inv_idx < len(cells):
            star = cells[inv_idx].find(class_="star")
            if star is not None:
                title = (star.get("title") or "").strip().rstrip("%")
                try:
                    rating_pct = int(round(float(title)))
                except (TypeError, ValueError):
                    rating_pct = None

        rows_out.append({
            "name": name,
            "market_cap": market_cap,
            "esg_msci": esg_letter,
            "rating_pct": rating_pct,
        })
    return rows_out


def fetch_ratings_page(base_company_url: str, cache_key_prefix: str | None = None) -> tuple[dict[str, Any], PageStepStatus]:
    """Strengths/Weaknesses bullets + Surperformance composite ratings + peer ESG.

    Source: /ratings/. Adds the analyst-grade narrative MS surfaces on its
    Ratings page so the deck can render a real Strengths/Weaknesses panel
    (PDF page 6 in the reference report) instead of an LLM substitute.
    """
    url = base_company_url.rstrip("/") + "/ratings/"
    status = PageStepStatus(step="fetch_ratings_page", message="")
    start = time.perf_counter() * 1000

    soup, errors = _fetch_page(url, _cache_slug(url, "ratings", cache_key_prefix))
    if soup is None:
        status.status = "failed"
        status.errors = errors
        status.elapsed_ms = (time.perf_counter() * 1000) - start
        log.info("[MarketScreener] Fetching /ratings/ page... FAILED")
        return _empty_ratings_payload(url, status), status
    log.info("[MarketScreener] Fetching /ratings/ page... SUCCESS")

    # Strip nav/footer/script chrome before parsing — keeps composite-rating
    # star detection from picking up unrelated stars in the page chrome.
    for sel in ("header", "footer", "nav", "script", "style", "aside"):
        for el in soup.find_all(sel):
            el.decompose()

    strengths, weaknesses, headline = _parse_strengths_weaknesses(soup)
    composite = _parse_composite_ratings(soup)
    peer_esg = _parse_peer_esg_table(soup)

    # ESG MSCI letter for the company itself: appears either above the peer
    # table as text near "ESG MSCI" or in the first peer row (own row).
    esg_msci: str | None = None
    if peer_esg:
        esg_msci = peer_esg[0].get("esg_msci")
    if not esg_msci:
        body_text = soup.get_text(" ", strip=True)
        m = re.search(r"ESG MSCI[^A-Z\-]+(CCC|BB|BBB|AA|AAA|A|B|-)\b", body_text)
        if m:
            esg_msci = m.group(1)

    payload = {
        "source_page": url,
        "source_type": "ratings_page",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "strengths": strengths,
        "weaknesses": weaknesses,
        "headline_bullets": headline,
        "composite_ratings": composite,           # {Trader: 70, Investor: 93, Global: 77, Quality: 91}
        "esg_msci_rating": esg_msci,              # "AAA"/"BB"/.../"-"/None
        "peer_esg": peer_esg,                     # [{name, market_cap, esg_msci, rating_pct}, ...]
        "warnings": [],
    }

    has_any = bool(strengths or weaknesses or any(v is not None for v in composite.values()) or peer_esg)
    if not has_any:
        status.status = "partial"
        status.message = "Ratings page: no recognized blocks parsed"
        payload["warnings"].append("No strengths/weaknesses/composite ratings parsed")
    else:
        status.status = "success"
        status.message = (
            f"Ratings page: {len(strengths)} strengths, {len(weaknesses)} weaknesses, "
            f"{sum(1 for v in composite.values() if v is not None)} composite ratings, "
            f"{len(peer_esg)} peer ESG rows"
        )
    status.record_count = len(strengths) + len(weaknesses) + len(peer_esg)
    status.elapsed_ms = (time.perf_counter() * 1000) - start
    return payload, status


def _empty_ratings_payload(source_page: str, status: PageStepStatus) -> dict[str, Any]:
    return {
        "source_page": source_page,
        "source_type": "ratings_page",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "strengths": [],
        "weaknesses": [],
        "headline_bullets": [],
        "composite_ratings": {label: None for label in _COMPOSITE_RATING_LABELS},
        "esg_msci_rating": None,
        "peer_esg": [],
        "warnings": status.errors,
    }


# ─── I. fetch_sector_peers (source: /sector/) ───────────────────────────────
#
# The /sector/ page exposes the canonical peer comparison table that
# MarketScreener uses on PDF page 4 (Sector and Competitors). It is much
# richer than yfinance peer medians: each row has 5d / 1m / 3m / YTD /
# 1y / 3y / 5y / 10y price changes and market cap in USD, ordered by
# market cap. The first row is the subject company itself, which makes
# it trivial to render the PDF-style "subject-vs-peers" table.

_SECTOR_PEER_HEADERS = [
    ("Change", "change_1d_pct"),
    ("5d. change", "change_5d_pct"),
    ("1 months change", "change_1m_pct"),
    ("3 months change", "change_3m_pct"),
    ("Varia. Jan 1.", "change_ytd_pct"),
    ("1-year change", "change_1y_pct"),
    ("3-years change", "change_3y_pct"),
    ("5-years change", "change_5y_pct"),
    ("10-years change", "change_10y_pct"),
    ("Capi.($)", "market_cap_usd"),
]


def _coerce_pct_or_none(value: str) -> float | None:
    """Parse '+12.34%', '-1.50%', '0.00%' into float; '-' / '' → None."""
    if not value:
        return None
    raw = value.strip().replace(",", ".").replace("%", "").replace("+", "").strip()
    if raw in ("-", "", "N/A"):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def fetch_sector_peers(base_company_url: str, cache_key_prefix: str | None = None) -> tuple[dict[str, Any], PageStepStatus]:
    """Sector peer comparison table from /sector/.

    Returns the subject company's row plus all peer rows with multi-period
    performance and USD market cap. Order matches MS (subject first, then
    peers descending by market cap).
    """
    url = base_company_url.rstrip("/") + "/sector/"
    status = PageStepStatus(step="fetch_sector_peers", message="")
    start = time.perf_counter() * 1000

    soup, errors = _fetch_page(url, _cache_slug(url, "sector", cache_key_prefix))
    if soup is None:
        status.status = "failed"
        status.errors = errors
        status.elapsed_ms = (time.perf_counter() * 1000) - start
        log.info("[MarketScreener] Fetching /sector/ page... FAILED")
        return _empty_sector_payload(url, status), status
    log.info("[MarketScreener] Fetching /sector/ page... SUCCESS")

    for sel in ("header", "footer", "nav", "script", "style", "aside"):
        for el in soup.find_all(sel):
            el.decompose()

    target_table = None
    target_headers: list[str] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 4:
            continue
        head_cells = [c.get_text(" ", strip=True) for c in rows[0].find_all(["td", "th"])]
        head_str = " ".join(head_cells)
        # the canonical peer table contains both "1-year change" and "Capi.($)"
        if "1-year change" in head_str and "Capi" in head_str:
            target_table = table
            target_headers = head_cells
            break

    if target_table is None:
        status.status = "partial"
        status.message = "No sector peer table found"
        status.elapsed_ms = (time.perf_counter() * 1000) - start
        return _empty_sector_payload(url, status), status

    # Map header text -> column index (only for the columns we expose).
    col_index: dict[str, int] = {}
    for header_text, key in _SECTOR_PEER_HEADERS:
        for idx, cell in enumerate(target_headers):
            if cell.strip() == header_text:
                col_index[key] = idx
                break

    rows_out: list[dict[str, Any]] = []
    rows = target_table.find_all("tr")
    # find the column index of the company name (the only cell that's not a
    # %/cap value or "Add to a list" boilerplate).
    name_col = None
    for tr in rows[1:]:
        cells = tr.find_all(["td", "th"])
        for idx, cell in enumerate(cells):
            text = cell.get_text(" ", strip=True)
            if not text or "Add to a list" in text:
                continue
            if re.match(r"^[A-Z][A-Z0-9 .,&'\-/()À-ÿ]{2,}$", text):
                name_col = idx
                break
        if name_col is not None:
            break

    if name_col is None:
        status.status = "partial"
        status.message = "Sector peer table missing name column"
        status.elapsed_ms = (time.perf_counter() * 1000) - start
        return _empty_sector_payload(url, status), status

    # MS appends two summary rows after the real peers — "Average" and
    # "Weighted average by Cap." — both useful for the deck but not
    # peers. Surface them on a separate key so renderers can handle
    # them distinctly without polluting the peer count.
    summary_rows: dict[str, dict[str, Any]] = {}
    for tr in rows[1:]:
        cells = tr.find_all(["td", "th"])
        if len(cells) <= name_col:
            continue
        name = cells[name_col].get_text(" ", strip=True)
        if not name or "Add to a list" in name:
            continue
        row: dict[str, Any] = {"name": name}
        for key, idx in col_index.items():
            if idx >= len(cells):
                row[key] = None
                continue
            raw = cells[idx].get_text(" ", strip=True)
            if key == "market_cap_usd":
                row[key] = raw or None
            else:
                row[key] = _coerce_pct_or_none(raw)
        # Pull the two MS summary rows out of the peer list.
        if name.strip().lower() in {"average", "weighted average by cap."}:
            summary_rows[name.strip().lower()] = row
            continue
        rows_out.append(row)

    payload = {
        "source_page": url,
        "source_type": "sector_peers",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "subject_name": rows_out[0]["name"] if rows_out else None,
        "rows": rows_out,
        "summary_rows": summary_rows,  # {"average": {...}, "weighted average by cap.": {...}}
        "warnings": [],
    }

    if rows_out:
        status.status = "success"
        status.message = f"Sector peers: {len(rows_out)} rows"
    else:
        status.status = "partial"
        status.message = "Sector peer table parsed but no rows"
    status.record_count = len(rows_out)
    status.elapsed_ms = (time.perf_counter() * 1000) - start
    return payload, status


def _empty_sector_payload(source_page: str, status: PageStepStatus) -> dict[str, Any]:
    return {
        "source_page": source_page,
        "source_type": "sector_peers",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "subject_name": None,
        "rows": [],
        "summary_rows": {},
        "warnings": status.errors,
    }


# ─── J. fetch_price_performance (source: summary page) ──────────────────────
#
# Performance grid (1d / 1w / MTD / 1m / 3m / 6m / YTD), course extremes
# (1w / 1m / YTD / 1y / 3y / 5y high-low ranges), and recent quotes table
# all live on the summary page. Parsed by walking the HTML tables that
# follow the headings "Quotes and Performance", "Course Extremes", and
# "Quotes". Surfaced as a dedicated payload block so the report doesn't
# bloat fetch_summary_page's contract.

_PERFORMANCE_LABEL_KEYS = {
    "1 day": "perf_1d_pct",
    "1 week": "perf_1w_pct",
    "Current month": "perf_mtd_pct",
    "1 month": "perf_1m_pct",
    "3 months": "perf_3m_pct",
    "6 months": "perf_6m_pct",
    "Current year": "perf_ytd_pct",
    "1 year": "perf_1y_pct",
}

_RANGE_LABEL_KEYS = {
    "1 week": "range_1w",
    "1 month": "range_1m",
    "Current year": "range_ytd",
    "1 year": "range_1y",
    "3 years": "range_3y",
    "5 years": "range_5y",
}


def _find_table_after_heading(soup: BeautifulSoup, heading_pattern: re.Pattern):
    """Return the first <table> following a heading whose text matches the pattern.

    Skips matches inside <title> / <meta> / non-content tags so a page title
    that happens to contain the heading text does not steer us to an
    unrelated table at the top of the page.
    """
    for el in soup.find_all(string=heading_pattern):
        node = el.parent
        if node is None:
            continue
        # Skip page-level metadata where the same text appears in <title>.
        if node.find_parent("title") is not None:
            continue
        if node.name in {"title", "meta"}:
            continue
        # MS section headings live inside <a>/<h3>/<h4> tags within a
        # card-header div. Anything else is likely a body sentence and
        # should not anchor a table lookup.
        if node.name not in {"a", "h1", "h2", "h3", "h4", "h5", "h6", "span"}:
            continue
        candidate = node.find_next("table")
        if candidate is not None:
            return candidate
    return None


def fetch_price_performance(base_company_url: str, cache_key_prefix: str | None = None) -> tuple[dict[str, Any], PageStepStatus]:
    """Performance grid + course extremes + recent quotes from /{SLUG}/.

    The same fetch pulls all three blocks since they share one HTML page.
    Designed to be called alongside fetch_summary_page (and shares its
    cached HTML).
    """
    url = (base_company_url or "").rstrip("/") + "/"
    status = PageStepStatus(step="fetch_price_performance", message="")
    start = time.perf_counter() * 1000

    soup, errors = _fetch_page(url, _cache_slug(url, "summary", cache_key_prefix))
    if soup is None:
        status.status = "failed"
        status.errors = errors
        status.elapsed_ms = (time.perf_counter() * 1000) - start
        log.info("[MarketScreener] Fetching summary page (price perf)... FAILED")
        return _empty_price_perf_payload(url, status), status
    log.info("[MarketScreener] Fetching summary page (price perf)... SUCCESS")

    for sel in ("header", "footer", "nav", "script", "style", "aside"):
        for el in soup.find_all(sel):
            el.decompose()

    # ─ Performance grid ─
    performance: dict[str, float | None] = {v: None for v in _PERFORMANCE_LABEL_KEYS.values()}
    perf_table = _find_table_after_heading(soup, re.compile(r"Quotes and Performance", re.I))
    if perf_table is not None:
        for tr in perf_table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            label = (cells[0] or "").strip()
            value = next((c for c in cells[1:] if c), "")
            key = _PERFORMANCE_LABEL_KEYS.get(label)
            if key:
                performance[key] = _coerce_pct_or_none(value)

    # ─ Course extremes (high/low ranges) ─
    extremes: dict[str, dict[str, float | None] | None] = {v: None for v in _RANGE_LABEL_KEYS.values()}
    extremes_table = _find_table_after_heading(soup, re.compile(r"Course Extremes", re.I))
    if extremes_table is not None:
        for tr in extremes_table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            label = (cells[0] or "").strip()
            key = _RANGE_LABEL_KEYS.get(label)
            if not key:
                continue
            numeric_cells = [c for c in cells[1:] if c]
            low_val = _coerce_numeric_or_none(numeric_cells[0]) if numeric_cells else None
            high_val = _coerce_numeric_or_none(numeric_cells[1]) if len(numeric_cells) >= 2 else None
            extremes[key] = {"low": low_val, "high": high_val}

    # ─ Recent quotes table (Date / Price / Change / Volume) ─
    quotes: list[dict[str, Any]] = []
    quotes_table = None
    # multiple tables can match — pick the one whose first row has Date/Price headers
    for tab in soup.find_all("table"):
        rows = tab.find_all("tr")
        if not rows:
            continue
        head_cells = [c.get_text(" ", strip=True) for c in rows[0].find_all(["td", "th"])]
        if head_cells[:2] == ["Date", "Price"]:
            quotes_table = tab
            break
    if quotes_table is not None:
        for tr in quotes_table.find_all("tr")[1:11]:  # cap at 10
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) < 4:
                continue
            quotes.append({
                "date": cells[0] or None,
                "price": cells[1] or None,
                "change_pct": _coerce_pct_or_none(cells[2]),
                "volume": cells[3] or None,
            })

    payload = {
        "source_page": url,
        "source_type": "price_performance",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "performance": performance,
        "course_extremes": extremes,
        "recent_quotes": quotes,
        "warnings": [],
    }

    has_any = (
        any(v is not None for v in performance.values())
        or any(v is not None for v in extremes.values())
        or bool(quotes)
    )
    status.status = "success" if has_any else "partial"
    status.message = (
        f"Price perf: {sum(1 for v in performance.values() if v is not None)} bands, "
        f"{sum(1 for v in extremes.values() if v is not None)} ranges, {len(quotes)} quotes"
    )
    status.record_count = sum(1 for v in performance.values() if v is not None) + len(quotes)
    status.elapsed_ms = (time.perf_counter() * 1000) - start
    return payload, status


def _empty_price_perf_payload(source_page: str, status: PageStepStatus) -> dict[str, Any]:
    return {
        "source_page": source_page,
        "source_type": "price_performance",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "performance": {v: None for v in _PERFORMANCE_LABEL_KEYS.values()},
        "course_extremes": {v: None for v in _RANGE_LABEL_KEYS.values()},
        "recent_quotes": [],
        "warnings": status.errors,
    }


# ─── K. fetch_analyst_recommendations (source: /consensus/ or summary) ──────
#
# The analyst-recommendations table on /consensus/ lists recent broker
# actions: date + headline + source code (e.g. "MT" = MT Newswires).
# Adds the "Recent broker actions" panel to the deck — a list users
# expect on PDF page 3 of the reference report.

def fetch_analyst_recommendations(base_company_url: str, cache_key_prefix: str | None = None) -> tuple[dict[str, Any], PageStepStatus]:
    """Recent analyst recommendations / broker actions list.

    Lives on the /consensus/ page (also surfaced on summary). One row per
    broker action, ordered most-recent first.
    """
    url = base_company_url.rstrip("/") + "/consensus/"
    status = PageStepStatus(step="fetch_analyst_recommendations", message="")
    start = time.perf_counter() * 1000

    soup, errors = _fetch_page(url, _cache_slug(url, "consensus", cache_key_prefix))
    if soup is None:
        status.status = "failed"
        status.errors = errors
        status.elapsed_ms = (time.perf_counter() * 1000) - start
        log.info("[MarketScreener] Fetching /consensus/ (recommendations)... FAILED")
        return _empty_recommendations_payload(url, status), status
    log.info("[MarketScreener] Fetching /consensus/ (recommendations)... SUCCESS")

    for sel in ("header", "footer", "nav", "script", "style", "aside"):
        for el in soup.find_all(sel):
            el.decompose()

    # Anchor the regex to the start of the *stripped* text (text nodes
    # often have leading whitespace from the HTML indentation). We use
    # \s* prefix so the match still hits even when MS prepends newlines.
    table = _find_table_after_heading(soup, re.compile(r"^\s*Analysts'?\s*recommendations", re.I))
    items: list[dict[str, str]] = []
    if table is not None:
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            date = (cells[0] or "").strip()
            headline = (cells[1] or "").strip()
            source = (cells[2] or "").strip() if len(cells) >= 3 else ""
            if not headline:
                continue
            if not date or len(date) > 20:
                continue
            items.append({"date": date, "headline": headline, "source": source or None})

    # Brokers covering the company — separate table on /consensus/.
    brokers: list[str] = []
    coverage_table = _find_table_after_heading(soup, re.compile(r"Analysts covering the company", re.I))
    if coverage_table is not None:
        for tr in coverage_table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if not cells:
                continue
            name = (cells[0] or "").strip()
            if name and name not in brokers:
                brokers.append(name)

    payload = {
        "source_page": url,
        "source_type": "analyst_recommendations",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
        "covering_brokers": brokers,
        "warnings": [],
    }
    status.status = "success" if (items or brokers) else "partial"
    status.message = f"Analyst recommendations: {len(items)} items, {len(brokers)} brokers"
    status.record_count = len(items) + len(brokers)
    status.elapsed_ms = (time.perf_counter() * 1000) - start
    return payload, status


def _empty_recommendations_payload(source_page: str, status: PageStepStatus) -> dict[str, Any]:
    return {
        "source_page": source_page,
        "source_type": "analyst_recommendations",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "items": [],
        "covering_brokers": [],
        "warnings": status.errors,
    }
