import { useEffect, useState } from "react";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "";

/**
 * Curated Jabal Research panel — the FAST LANE. Hits /api/jabal/panel
 * which serves a directory of pre-rendered decks (refreshed nightly
 * by the Render cron). Each card links straight to a static PPTX,
 * so the consumer's click is <100ms and never touches a provider.
 *
 * Off-panel tickers still go through the legacy ticker-search box
 * below this component.
 */
export default function PanelDecks() {
  const [decks, setDecks] = useState([]);
  const [renderedAt, setRenderedAt] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE}/api/jabal/panel`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data) => {
        if (cancelled) return;
        setDecks(Array.isArray(data?.decks) ? data.decks : []);
        setRenderedAt(data?.rendered_at || null);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(String(e?.message || e));
      })
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, []);

  if (loading || (decks.length === 0 && !error)) {
    return null; // silent — fall back to the search box without flashing
  }

  return (
    <section className="w-full max-w-5xl mx-auto px-4 pt-8 pb-2">
      <div className="flex items-baseline justify-between mb-4">
        <div>
          <h2 className="text-xl font-semibold text-slate-100">
            Curated panel
          </h2>
          <p className="text-xs text-slate-400 mt-0.5">
            {decks.length} pre-rendered decks · refreshed{" "}
            {renderedAt ? new Date(renderedAt).toLocaleString() : "—"}
          </p>
        </div>
        <a
          href="/jabal"
          className="text-xs text-slate-400 hover:text-slate-200"
        >
          View as table →
        </a>
      </div>

      {error && (
        <div className="text-xs text-amber-400 mb-3">
          Panel unavailable: {error}
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {decks.map((d) => (
          <DeckCard key={d.ticker} deck={d} />
        ))}
      </div>

      <div className="text-xs text-slate-500 mt-6 mb-4">
        For any other ticker, use the search box below — runs an on-demand
        preview through the full provider stack (~30–90s).
      </div>
    </section>
  );
}

function DeckCard({ deck }) {
  const conf = deck.by_confidence || {};
  const high = conf.High ?? 0;
  const med = conf.Medium ?? 0;
  const low = conf.Low ?? 0;
  const sources = Array.isArray(deck.sources) ? deck.sources : [];
  return (
    <a
      href={`${API_BASE}/api/jabal/${deck.ticker}/deck.pptx`}
      className="group block rounded-lg border border-slate-800 bg-slate-900 px-4 py-3 hover:border-amber-700 hover:bg-slate-900/80 transition-colors"
    >
      <div className="flex items-start justify-between">
        <div className="min-w-0">
          <div className="font-mono text-sm text-amber-400 truncate">
            {deck.ticker}
          </div>
          <div className="text-sm font-medium text-slate-100 truncate">
            {deck.company_name || deck.ticker}
          </div>
          <div className="text-xs text-slate-500 truncate mt-0.5">
            {deck.sector || "—"}
          </div>
        </div>
        <div className="shrink-0 text-xs text-slate-500 group-hover:text-amber-400">
          .pptx →
        </div>
      </div>

      <div className="flex items-center gap-1 mt-3">
        {high > 0 && (
          <span className="text-[10px] font-semibold text-emerald-400 bg-emerald-950/40 px-1.5 py-0.5 rounded">
            {high} H
          </span>
        )}
        {med > 0 && (
          <span className="text-[10px] font-semibold text-amber-400 bg-amber-950/40 px-1.5 py-0.5 rounded">
            {med} M
          </span>
        )}
        {low > 0 && (
          <span className="text-[10px] font-semibold text-slate-400 bg-slate-800 px-1.5 py-0.5 rounded">
            {low} L
          </span>
        )}
        <span className="text-[10px] text-slate-500 ml-auto truncate">
          {sources.join(" · ")}
        </span>
      </div>
    </a>
  );
}
