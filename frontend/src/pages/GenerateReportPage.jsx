import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import BloombergUpload from "../components/BloombergUpload";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "";

function extractFilename(contentDisposition, fallback) {
  if (!contentDisposition) return fallback;
  const m = /filename\*?=(?:UTF-8''|")?([^\";]+)/i.exec(contentDisposition);
  if (!m || !m[1]) return fallback;
  return decodeURIComponent(m[1].replace(/"/g, "").trim());
}

function toErrorText(value) {
  if (value == null || value === "") return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) {
    return value.map(toErrorText).filter(Boolean).join("; ");
  }
  if (typeof value === "object") {
    if (typeof value.msg === "string") return value.msg;
    if (typeof value.message === "string") return value.message;
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function formatApiErrorDetail(detail) {
  if (detail == null || detail === "") return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => toErrorText(typeof d === "string" ? d : d?.msg ?? d?.message ?? d))
      .filter(Boolean)
      .join("; ");
  }
  if (typeof detail === "object") {
    const parts = [];
    const sum = toErrorText(detail.summary);
    if (sum) parts.push(sum);
    if (Array.isArray(detail.reasons) && detail.reasons.length) {
      for (const r of detail.reasons) {
        const t = toErrorText(r);
        if (t) parts.push(t);
      }
    }
    if (parts.length) return parts.join(" ");
    if (typeof detail.error === "string" && detail.error.trim()) {
      return detail.error.trim();
    }
    const m = toErrorText(detail.message);
    if (m) return m;
    try {
      return JSON.stringify(detail);
    } catch {
      return "Request failed";
    }
  }
  return String(detail);
}

function errorToUserMessage(e) {
  if (e == null) return "Unexpected error";
  // Browser network failures surface as a bare "Failed to fetch" TypeError —
  // translate to something the analyst can act on (it's almost always the
  // server briefly redeploying or a transient network blip, not a real bug).
  if (e instanceof TypeError && /fetch/i.test(e.message || "")) {
    return "Could not reach the server (it may be redeploying). Please try again in a moment.";
  }
  if (typeof e === "string") return e;
  if (e instanceof Error && typeof e.message === "string") return e.message;
  if (typeof e === "object" && typeof e.message === "string") return e.message;
  return toErrorText(e);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// A bare fetch() throws "Failed to fetch" on any transient network drop
// (Render swapping instances on deploy, a cold edge, a Wi-Fi blip). For a
// 30–90s generate that's a jarring first impression. warmServer() pings
// /health first so the common "server waking up" case fails fast and
// recovers, and postJsonWithRetry() retries the actual call once on a
// network-level error before giving up.
async function warmServer() {
  try {
    await fetch(`${API_BASE}/health`, { cache: "no-store" });
  } catch {
    /* ignore — the real call below will surface any persistent outage */
  }
}

async function postJsonWithRetry(url, body, { retries = 1, backoffMs = 2500, onRetry } = {}) {
  let lastErr;
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    try {
      return await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    } catch (e) {
      // Only retry network-level failures (TypeError). An HTTP error response
      // resolves the promise (createRes.ok handles it) and never lands here.
      lastErr = e;
      if (attempt < retries) {
        if (onRetry) onRetry(attempt + 1, retries);
        await sleep(backoffMs);
      }
    }
  }
  throw lastErr;
}

export default function GenerateReportPage() {
  const [ticker, setTicker] = useState("");
  const [skipLlm, setSkipLlm] = useState(false);
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [suggestions, setSuggestions] = useState([]);
  const [suggestOpen, setSuggestOpen] = useState(false);
  const [highlight, setHighlight] = useState(-1);
  const [suggestLoading, setSuggestLoading] = useState(false);
  // After a successful report generation we stash the run-id + filename
  // so the user can grab the provenance sidecar separately (without
  // re-running the pipeline). Cleared on every new generate-click.
  const [lastReport, setLastReport] = useState(null); // {runId, filename, ticker}
  const blurCloseTimer = useRef(null);
  const searchAbortRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(
    () => () => {
      if (blurCloseTimer.current) clearTimeout(blurCloseTimer.current);
      if (searchAbortRef.current) searchAbortRef.current.abort();
    },
    [],
  );

  const disabled = useMemo(() => loading || !ticker.trim(), [loading, ticker]);

  useEffect(() => {
    const q = ticker;
    setHighlight(-1);
    const id = setTimeout(async () => {
      if (searchAbortRef.current) searchAbortRef.current.abort();
      const ctrl = new AbortController();
      searchAbortRef.current = ctrl;
      setSuggestLoading(true);
      try {
        const res = await fetch(
          `${API_BASE}/api/tickers/search?q=${encodeURIComponent(q.trim())}`,
          { signal: ctrl.signal },
        );
        const data = await res.json().catch(() => ({}));
        setSuggestions(Array.isArray(data.results) ? data.results : []);
      } catch (e) {
        if (e?.name !== "AbortError") setSuggestions([]);
      } finally {
        if (searchAbortRef.current === ctrl) setSuggestLoading(false);
      }
    }, 200);
    return () => clearTimeout(id);
  }, [ticker]);

  const pickSuggestion = useCallback((row) => {
    if (!row?.ticker) return;
    setTicker(row.ticker);
    setSuggestOpen(false);
    setHighlight(-1);
    inputRef.current?.focus();
  }, []);

  const openSuggestions = useCallback(() => {
    if (blurCloseTimer.current) clearTimeout(blurCloseTimer.current);
    setSuggestOpen(true);
  }, []);

  const scheduleCloseSuggestions = useCallback(() => {
    blurCloseTimer.current = setTimeout(() => {
      setSuggestOpen(false);
      setHighlight(-1);
    }, 150);
  }, []);

  const generateAndDownload = async () => {
    setError("");
    setStatus("Waking the server...");
    setLoading(true);
    setLastReport(null);
    try {
      const tk = ticker.trim().toUpperCase();
      await warmServer();
      setStatus("Generating report (30–90s)...");
      const createRes = await postJsonWithRetry(
        `${API_BASE}/api/reports`,
        { ticker: tk, skip_llm: skipLlm },
        {
          onRetry: (n, total) =>
            setStatus(`Network hiccup — retrying (${n}/${total})...`),
        },
      );
      if (!createRes.ok) {
        const err = await createRes.json().catch(() => ({}));
        const msg = toErrorText(
          formatApiErrorDetail(err?.detail) || "Failed to generate report",
        );
        throw new Error(msg);
      }

      const created = await createRes.json();
      const runId = created?.report?.id;
      if (!runId) {
        throw new Error("Report generated but run id is missing");
      }
      // Pass filename through so download works even if DB persist failed.
      const dlFilename = created?.report?.filename;

      setStatus("Downloading report bundle (deck + provenance)...");
      // BUNDLE endpoint — returns a .zip with both .pptx + .provenance.xlsx
      // in a single response. Atomic vs the prior two-click flow that
      // could lose the .xlsx if Render's container restarted between
      // clicks (free-tier ephemeral /tmp).
      const bundleRes = await fetch(
        `${API_BASE}/api/reports/${runId}/bundle?t=${Date.now()}`,
      );
      if (!bundleRes.ok) {
        // Fall back to legacy per-file downloads when bundle endpoint
        // is missing (deploy lag between backend + frontend).
        const dlParams = new URLSearchParams({ t: String(Date.now()) });
        if (dlFilename) dlParams.set("filename", dlFilename);
        const dlRes = await fetch(
          `${API_BASE}/api/reports/${runId}/download?${dlParams.toString()}`,
        );
        if (!dlRes.ok) {
          const err = await dlRes.json().catch(() => ({}));
          throw new Error(
            toErrorText(formatApiErrorDetail(err?.detail) || "Download failed"),
          );
        }
        const blob = await dlRes.blob();
        const filename = extractFilename(
          dlRes.headers.get("content-disposition"),
          `${tk}_preview.pptx`,
        );
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url; a.download = filename;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 100);
        setStatus(`Done. Downloaded ${filename} (provenance unavailable — bundle endpoint missing)`);
        setLastReport({ runId, filename: dlFilename || filename, ticker: tk });
        return;
      }

      const blob = await bundleRes.blob();
      const filename = extractFilename(
        bundleRes.headers.get("content-disposition"),
        `${tk}_bundle.zip`,
      );
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 100);
      setStatus(`Done. Downloaded ${filename} (deck + provenance.xlsx)`);
      setLastReport({ runId, filename: dlFilename, ticker: tk });
    } catch (e) {
      setError(errorToUserMessage(e));
      setStatus("");
    } finally {
      setLoading(false);
    }
  };

  const downloadProvenance = async () => {
    if (!lastReport?.runId) return;
    setError("");
    try {
      const params = new URLSearchParams({
        type: "provenance",
        t: String(Date.now()),
      });
      if (lastReport.filename) params.set("filename", lastReport.filename);
      const res = await fetch(
        `${API_BASE}/api/reports/${lastReport.runId}/download?${params.toString()}`,
      );
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(
          toErrorText(formatApiErrorDetail(err?.detail))
            || "Provenance sidecar not available for this run",
        );
      }
      const blob = await res.blob();
      const filename = extractFilename(
        res.headers.get("content-disposition"),
        `${lastReport.ticker}_provenance.xlsx`,
      );
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 100);
    } catch (e) {
      setError(errorToUserMessage(e));
    }
  };

  return (
    <div className="min-h-[70vh] px-4">
      <BloombergUpload onUploaded={() => {}} />
      <div className="flex items-center justify-center pt-6 pb-8">
        <div className="w-full max-w-xl bg-slate-900 border border-slate-800 rounded-xl p-6">
        <h1 className="text-2xl font-semibold mb-2">Generate a report</h1>
        <p className="text-sm text-slate-400 mb-6">
          Enter any symbol (e.g. 1180.SR, BKMB.OM, 2222.SR) to run an
          on-demand preview through the full provider stack (~30–90s).
        </p>

        <label className="block text-sm mb-2 text-slate-300" htmlFor="ticker-input">
          Ticker
        </label>
        <div className="relative mb-4">
          <input
            id="ticker-input"
            ref={inputRef}
            autoComplete="off"
            role="combobox"
            aria-expanded={suggestOpen}
            aria-controls="ticker-suggestions"
            aria-activedescendant={
              highlight >= 0 && suggestions[highlight]
                ? `ticker-opt-${highlight}`
                : undefined
            }
            value={ticker}
            onChange={(e) => {
              setTicker(e.target.value);
              openSuggestions();
            }}
            onFocus={openSuggestions}
            onBlur={scheduleCloseSuggestions}
            onKeyDown={(e) => {
              if (!suggestOpen && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
                openSuggestions();
                return;
              }
              if (!suggestOpen) return;
              if (e.key === "Escape") {
                e.preventDefault();
                setSuggestOpen(false);
                setHighlight(-1);
                return;
              }
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setHighlight((h) =>
                  suggestions.length ? Math.min(h + 1, suggestions.length - 1) : -1,
                );
                return;
              }
              if (e.key === "ArrowUp") {
                e.preventDefault();
                setHighlight((h) => (h <= 0 ? -1 : h - 1));
                return;
              }
              if (e.key === "Enter" && highlight >= 0 && suggestions[highlight]) {
                e.preventDefault();
                pickSuggestion(suggestions[highlight]);
              }
            }}
            placeholder="e.g. AAPL, TSLA, 2222.SR …"
            className="w-full rounded-lg bg-slate-950 border border-slate-700 px-3 py-2 outline-none focus:border-blue-500"
          />
          {suggestOpen && (suggestions.length > 0 || suggestLoading || ticker.trim()) ? (
            <ul
              id="ticker-suggestions"
              role="listbox"
              className="absolute z-50 mt-1 max-h-52 w-full overflow-auto rounded-lg border border-slate-700 bg-slate-900 py-1 shadow-lg"
            >
              {suggestLoading && suggestions.length === 0 ? (
                <li className="px-3 py-2 text-sm text-slate-500">Loading…</li>
              ) : null}
              {!suggestLoading && suggestions.length === 0 && ticker.trim() ? (
                <li className="px-3 py-2 text-sm text-slate-500">
                  No saved matches — type any valid ticker and generate
                </li>
              ) : null}
              {suggestions.map((row, i) => (
                <li
                  key={`${row.ticker}-${i}`}
                  id={`ticker-opt-${i}`}
                  role="option"
                  aria-selected={i === highlight}
                  className={`cursor-pointer px-3 py-2 text-sm ${
                    i === highlight
                      ? "bg-slate-800 text-white"
                      : "text-slate-200 hover:bg-slate-800/80"
                  }`}
                  onMouseDown={(e) => e.preventDefault()}
                  onMouseEnter={() => setHighlight(i)}
                  onClick={() => pickSuggestion(row)}
                >
                  <span className="font-mono text-blue-300">{row.ticker}</span>
                  <span className="mx-2 text-slate-500">·</span>
                  <span>{row.company}</span>
                  {row.country ? (
                    <span className="ml-2 text-xs text-slate-500">{row.country}</span>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : null}
        </div>

        <label className="flex items-center gap-2 text-sm text-slate-300 mb-6">
          <input
            type="checkbox"
            checked={skipLlm}
            onChange={(e) => setSkipLlm(e.target.checked)}
          />
          Skip LLM (faster)
        </label>

        <button
          onClick={generateAndDownload}
          disabled={disabled}
          className="w-full rounded-lg bg-blue-600 hover:bg-blue-500 disabled:bg-slate-700 disabled:cursor-not-allowed px-4 py-2 font-medium"
        >
          {loading ? "Working..." : "Generate & Download"}
        </button>

        {status ? <p className="mt-4 text-sm text-emerald-400">{status}</p> : null}
        {error ? <p className="mt-2 text-sm text-rose-400">{error}</p> : null}

        {lastReport ? (
          <div className="mt-4 pt-4 border-t border-slate-800">
            <p className="text-xs text-slate-400 mb-2">
              Data trace: every number on the deck mapped to its source provider,
              URL, data period, and fetch timestamp.
            </p>
            <button
              type="button"
              onClick={downloadProvenance}
              className="w-full rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 px-4 py-2 text-sm font-medium text-slate-200"
            >
              Download data sources (.xlsx)
            </button>
          </div>
        ) : null}
        </div>
      </div>
    </div>
  );
}
