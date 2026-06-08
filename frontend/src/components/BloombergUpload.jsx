import { useRef, useState } from "react";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "";

/**
 * Drag-and-drop Bloomberg consensus uploader.
 *
 * Accepts a CSV / XLSX in the schema documented at
 * docs/stage2/bloomberg_upload.md. On upload:
 *   1. POSTs the file to /api/bloomberg/upload.
 *   2. Server parses, persists to data/bloomberg/consensus.csv,
 *      refreshes affected tickers, re-renders their decks.
 *   3. Shows the list of tickers updated with direct deck-download links.
 */
export default function BloombergUpload({ onUploaded }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState(null);
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef(null);

  const onFiles = async (files) => {
    if (!files || !files[0]) return;
    setError("");
    setResult(null);
    setBusy(true);
    try {
      const form = new FormData();
      form.append("file", files[0]);
      const res = await fetch(`${API_BASE}/api/bloomberg/upload`, {
        method: "POST",
        body: form,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data?.detail || `HTTP ${res.status}`);
      }
      setResult(data);
      if (onUploaded) onUploaded(data);
    } catch (e) {
      setError(String(e?.message || e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="w-full max-w-5xl mx-auto px-4 pt-2 pb-4">
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          onFiles(e.dataTransfer.files);
        }}
        className={`rounded-lg border-2 border-dashed p-5 transition-colors ${
          dragOver
            ? "border-amber-500 bg-amber-950/20"
            : "border-slate-700 bg-slate-900/40"
        }`}
      >
        <div className="flex items-center justify-between gap-4">
          <div className="min-w-0">
            <div className="text-sm font-semibold text-slate-100">
              Upload Bloomberg consensus
            </div>
            <div className="text-xs text-slate-400 mt-0.5">
              Drop a .csv or .xlsx (BEST schema:{" "}
              <code className="bg-slate-800 px-1 rounded">
                ticker, period_type, period_label, metric, mean, low, high, num_estimates
              </code>
              ). Each ticker's deck regenerates immediately.
            </div>
          </div>
          <button
            type="button"
            onClick={() => inputRef.current?.click()}
            disabled={busy}
            className="shrink-0 rounded-md bg-amber-700 hover:bg-amber-600 disabled:bg-slate-700 disabled:cursor-not-allowed px-3 py-1.5 text-sm font-medium text-slate-100"
          >
            {busy ? "Uploading…" : "Choose file"}
          </button>
          <input
            ref={inputRef}
            type="file"
            accept=".csv,.xlsx,.xls"
            className="hidden"
            onChange={(e) => onFiles(e.target.files)}
          />
        </div>

        {error && (
          <div className="mt-3 text-xs text-rose-400">Error: {error}</div>
        )}

        {result && (
          <div className="mt-3 text-xs text-slate-300">
            <div className="text-emerald-400 font-medium mb-1">
              ✓ {result.rows_persisted} rows persisted ·{" "}
              {result.decks_rerendered.length} deck(s) regenerated
            </div>
            <div className="flex flex-wrap gap-2">
              {result.decks_rerendered.map((t) => (
                <a
                  key={t}
                  href={`${API_BASE}/api/jabal/${t}/deck.pptx`}
                  className="font-mono text-amber-400 hover:underline bg-slate-800 px-2 py-0.5 rounded"
                >
                  {t} .pptx
                </a>
              ))}
            </div>
          </div>
        )}
      </div>
    </section>
  );
}
