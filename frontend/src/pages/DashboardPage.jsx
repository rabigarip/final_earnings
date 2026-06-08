import { useState, useEffect } from "react";
import { Link } from "react-router-dom";

// Calendar-driven dashboard. Reads from:
//   GET /api/v2/upcoming   (data/calendar/upcoming.json)
//   GET /api/v2/universe   (data/tickers.json, top-N by USD mcap)
//   GET /api/v2/decks      (outputs/*.pptx mtime-sorted)
//
// No state management gymnastics — three independent fetches, three
// independent loading flags, all in one page. The analyst sees:
//   * Upcoming earnings in the next 14 days (Generate per row)
//   * Recently generated decks (one-click download)
//   * Top 50 of the 500-ticker universe (browse-to-generate)

const FAMILY_OPTIONS = [
  { value: "", label: "All families" },
  { value: "bank", label: "Banks" },
  { value: "energy", label: "Energy" },
  { value: "materials", label: "Materials" },
  { value: "tech", label: "Tech" },
  { value: "healthcare", label: "Healthcare" },
  { value: "consumer_staples", label: "Consumer Staples" },
  { value: "retail", label: "Retail" },
  { value: "industrial", label: "Industrials" },
  { value: "telco", label: "Telco" },
  { value: "utility", label: "Utilities" },
  { value: "reit", label: "REITs" },
  { value: "insurance", label: "Insurance" },
];

function classBadge(family) {
  const colors = {
    bank: "bg-blue-900 text-blue-100",
    energy: "bg-amber-900 text-amber-100",
    materials: "bg-orange-900 text-orange-100",
    tech: "bg-purple-900 text-purple-100",
    healthcare: "bg-emerald-900 text-emerald-100",
    consumer_staples: "bg-pink-900 text-pink-100",
    retail: "bg-rose-900 text-rose-100",
    industrial: "bg-slate-700 text-slate-100",
    telco: "bg-cyan-900 text-cyan-100",
    utility: "bg-yellow-900 text-yellow-100",
    reit: "bg-green-900 text-green-100",
    insurance: "bg-indigo-900 text-indigo-100",
    financial_services: "bg-indigo-900 text-indigo-100",
    consumer_discretionary: "bg-fuchsia-900 text-fuchsia-100",
    other: "bg-slate-800 text-slate-300",
  };
  return colors[family] || colors.other;
}

function urgencyClass(daysUntil) {
  if (daysUntil <= 1) return "text-red-300 font-semibold";
  if (daysUntil <= 3) return "text-amber-300";
  if (daysUntil <= 7) return "text-slate-100";
  return "text-slate-400";
}

function fmtMoney(usd) {
  if (!usd) return "—";
  if (usd >= 1e12) return `$${(usd / 1e12).toFixed(2)}T`;
  if (usd >= 1e9) return `$${(usd / 1e9).toFixed(1)}B`;
  if (usd >= 1e6) return `$${(usd / 1e6).toFixed(0)}M`;
  return `$${usd.toFixed(0)}`;
}

function UpcomingTable({ tickers, loading }) {
  if (loading) return <div className="text-slate-400 text-sm">Loading…</div>;
  if (!tickers.length) {
    return (
      <div className="text-slate-400 text-sm py-4">
        No earnings in the next 14 days for the active universe.
      </div>
    );
  }
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-xs uppercase text-slate-500 border-b border-slate-800">
          <th className="text-left py-2">When</th>
          <th className="text-left">Ticker</th>
          <th className="text-left">Company</th>
          <th className="text-left">Quarter</th>
          <th className="text-left">Family</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {tickers.map((t) => (
          <tr key={t.ticker} className="border-b border-slate-900 hover:bg-slate-900/50">
            <td className={`py-2 ${urgencyClass(t.days_until)}`}>
              {t.days_until === 0 ? "Today" : t.days_until === 1 ? "Tomorrow"
                : `${t.days_until}d`}
              <span className="text-xs text-slate-500 ml-2">{t.earnings_date}</span>
            </td>
            <td className="font-mono text-slate-100">{t.ticker}</td>
            <td className="text-slate-300">{t.company_name}</td>
            <td className="text-slate-400">{t.next_quarter_label || "—"}</td>
            <td>
              <span className={`px-2 py-0.5 rounded text-xs ${classBadge(t.template_family)}`}>
                {t.template_family}
              </span>
            </td>
            <td className="text-right">
              <Link
                to={`/?ticker=${encodeURIComponent(t.ticker)}`}
                className="px-3 py-1 rounded bg-emerald-700 hover:bg-emerald-600 text-white text-xs"
              >
                Generate
              </Link>
              {t.recent_deck_exists && (
                <span className="ml-2 text-xs text-emerald-400" title="A deck for this ticker is already on disk">●</span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function RecentDecks({ decks, loading }) {
  if (loading) return <div className="text-slate-400 text-sm">Loading…</div>;
  if (!decks.length) return <div className="text-slate-400 text-sm">No decks generated yet.</div>;
  return (
    <ul className="text-sm space-y-1">
      {decks.map((d) => (
        <li key={d.filename} className="flex items-center justify-between hover:bg-slate-900/50 px-2 py-1 rounded">
          <div className="flex items-center gap-3 min-w-0">
            <span className="font-mono text-slate-100 text-xs whitespace-nowrap">{d.ticker}</span>
            <span className="text-slate-400 truncate text-xs">{d.filename}</span>
          </div>
          <div className="flex items-center gap-2 text-xs text-slate-500 whitespace-nowrap">
            <span>{d.size_kb} KB</span>
            <a
              href={`/outputs/${d.filename}`}
              className="text-emerald-400 hover:text-emerald-300"
            >Deck</a>
            {d.has_provenance && (
              <a
                href={`/outputs/${d.filename.replace(".pptx", ".provenance.xlsx")}`}
                className="text-blue-400 hover:text-blue-300"
              >.xlsx</a>
            )}
          </div>
        </li>
      ))}
    </ul>
  );
}

function UniverseTable({ tickers, loading }) {
  if (loading) return <div className="text-slate-400 text-sm">Loading…</div>;
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-xs uppercase text-slate-500 border-b border-slate-800">
          <th className="text-left py-2">Ticker</th>
          <th className="text-left">Company</th>
          <th className="text-left">Sector</th>
          <th className="text-right">Mkt Cap</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {tickers.map((t) => (
          <tr key={t.ticker} className="border-b border-slate-900 hover:bg-slate-900/50">
            <td className="py-2 font-mono text-slate-100 text-xs">
              {t.ticker}
              {t.is_depositary_receipt && (
                <span className="ml-1 text-xs text-amber-400" title={`DR; underlying ${t.underlying_ticker}`}>↗</span>
              )}
            </td>
            <td className="text-slate-300 text-xs truncate max-w-xs">{t.company_name}</td>
            <td className="text-slate-400 text-xs">
              <span className={`px-1.5 py-0.5 rounded text-[10px] ${classBadge(t.template_family)}`}>
                {t.template_family}
              </span>
            </td>
            <td className="text-right text-slate-300 text-xs">{fmtMoney(t.market_cap_usd)}</td>
            <td className="text-right">
              <Link to={`/?ticker=${encodeURIComponent(t.ticker)}`} className="text-xs text-emerald-400 hover:text-emerald-300">→</Link>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function CoverageMatrix({ items, loading }) {
  if (loading) return <div className="text-slate-400 text-sm">Loading…</div>;
  if (!items.length) return <div className="text-slate-400 text-sm">No disclosed pipelines configured.</div>;
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-xs uppercase text-slate-500 border-b border-slate-800">
          <th className="text-left py-2">Ticker</th>
          <th className="text-left">Latest</th>
          <th className="text-right">Quarters</th>
          <th className="text-right">Coverage</th>
          <th className="text-right">Age (days)</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {items.map((t) => {
          const stale = t.days_since_period_end != null && t.days_since_period_end > 90;
          return (
            <tr key={t.ticker} className="border-b border-slate-900">
              <td className="py-2 font-mono text-slate-100 text-xs">{t.ticker}</td>
              <td className="text-slate-300 text-xs">{t.most_recent_period || "—"}</td>
              <td className="text-right text-slate-400 text-xs">{t.n_quarters}</td>
              <td className={`text-right text-xs ${t.coverage_pct >= 90 ? "text-emerald-300" : t.coverage_pct >= 50 ? "text-amber-300" : "text-rose-300"}`}>
                {t.coverage_pct}%
              </td>
              <td className={`text-right text-xs ${stale ? "text-rose-300" : "text-slate-400"}`}>
                {t.days_since_period_end != null ? t.days_since_period_end : "—"}
              </td>
              <td className="text-right">
                {t.file_exists
                  ? <span className="text-xs text-emerald-400">✓</span>
                  : <span className="text-xs text-slate-600">—</span>}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function VoiceEvalCard({ snapshot, loading }) {
  if (loading) return <div className="text-slate-400 text-sm">Loading…</div>;
  const agg = snapshot?.aggregate || {};
  const results = snapshot?.results || {};
  if (!agg.n_decks) return <div className="text-slate-400 text-sm">No voice eval data yet.</div>;
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-4 text-xs">
        <span className="text-slate-400">Snapshot:</span>
        <span className="text-slate-200">{snapshot.snapshot_date}</span>
        <span className="text-slate-400">|</span>
        <span className="text-slate-400">Avg composite:</span>
        <span className="text-slate-100 font-mono">{(agg.avg_composite ?? 0).toFixed(3)}</span>
      </div>
      <div className="flex gap-3 text-xs">
        <span className="px-2 py-0.5 rounded bg-emerald-900 text-emerald-100">{agg.n_matches} matches</span>
        <span className="px-2 py-0.5 rounded bg-amber-900 text-amber-100">{agg.n_close} close</span>
        <span className="px-2 py-0.5 rounded bg-rose-900 text-rose-100">{agg.n_diverges} diverges</span>
      </div>
      <ul className="text-xs space-y-1">
        {Object.entries(results).map(([ticker, r]) => {
          const grade = r.grade;
          const color = grade === "matches" ? "text-emerald-300"
                       : grade === "close" ? "text-amber-300" : "text-rose-300";
          return (
            <li key={ticker} className="flex items-center justify-between px-2 py-1 hover:bg-slate-900/50 rounded">
              <span className="font-mono text-slate-200">{ticker}</span>
              <span className={`${color} font-mono`}>{r.composite_score.toFixed(3)} · {grade}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export default function DashboardPage() {
  const [upcoming, setUpcoming] = useState({ tickers: [], generated_at: null });
  const [upcomingLoading, setUpcomingLoading] = useState(true);
  const [decks, setDecks] = useState([]);
  const [decksLoading, setDecksLoading] = useState(true);
  const [universe, setUniverse] = useState([]);
  const [universeLoading, setUniverseLoading] = useState(true);
  const [coverage, setCoverage] = useState([]);
  const [coverageLoading, setCoverageLoading] = useState(true);
  const [voiceEval, setVoiceEval] = useState(null);
  const [voiceLoading, setVoiceLoading] = useState(true);
  const [family, setFamily] = useState("");

  useEffect(() => {
    setUpcomingLoading(true);
    fetch(`/api/v2/upcoming?horizon_days=14${family ? `&family=${family}` : ""}`)
      .then((r) => r.json())
      .then((d) => { setUpcoming(d); setUpcomingLoading(false); })
      .catch(() => setUpcomingLoading(false));
  }, [family]);

  useEffect(() => {
    fetch("/api/v2/decks?limit=15")
      .then((r) => r.json())
      .then((d) => { setDecks(d.decks || []); setDecksLoading(false); })
      .catch(() => setDecksLoading(false));
  }, []);

  useEffect(() => {
    setUniverseLoading(true);
    fetch(`/api/v2/universe?limit=50${family ? `&family=${family}` : ""}`)
      .then((r) => r.json())
      .then((d) => { setUniverse(d.tickers || []); setUniverseLoading(false); })
      .catch(() => setUniverseLoading(false));
  }, [family]);

  useEffect(() => {
    fetch("/api/v2/disclosed_status")
      .then((r) => r.json())
      .then((d) => { setCoverage(d.tickers || []); setCoverageLoading(false); })
      .catch(() => setCoverageLoading(false));
  }, []);

  useEffect(() => {
    fetch("/api/v2/voice_eval_latest")
      .then((r) => r.json())
      .then((d) => { setVoiceEval(d); setVoiceLoading(false); })
      .catch(() => setVoiceLoading(false));
  }, []);

  return (
    <div className="max-w-7xl mx-auto px-6 py-8 space-y-8">
      <div className="flex items-end justify-between flex-wrap gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">Earnings Dashboard</h1>
          <p className="text-sm text-slate-400 mt-1">
            Calendar-driven view over the 500-ticker universe. Generated{" "}
            {upcoming.generated_at ? new Date(upcoming.generated_at).toLocaleString() : "—"}.
          </p>
        </div>
        <div>
          <label className="text-xs text-slate-500 mr-2">Filter:</label>
          <select
            value={family}
            onChange={(e) => setFamily(e.target.value)}
            className="bg-slate-900 border border-slate-700 rounded px-2 py-1 text-sm text-slate-100"
          >
            {FAMILY_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        </div>
      </div>

      <section>
        <h2 className="text-sm uppercase text-slate-400 font-semibold mb-3">Upcoming earnings · next 14 days</h2>
        <div className="bg-slate-900/30 border border-slate-800 rounded p-4">
          <UpcomingTable tickers={upcoming.tickers || []} loading={upcomingLoading} />
        </div>
      </section>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <section>
          <h2 className="text-sm uppercase text-slate-400 font-semibold mb-3">Recent decks</h2>
          <div className="bg-slate-900/30 border border-slate-800 rounded p-4">
            <RecentDecks decks={decks} loading={decksLoading} />
          </div>
        </section>
        <section>
          <h2 className="text-sm uppercase text-slate-400 font-semibold mb-3">Universe · top 50 by market cap</h2>
          <div className="bg-slate-900/30 border border-slate-800 rounded p-4 max-h-[600px] overflow-y-auto">
            <UniverseTable tickers={universe} loading={universeLoading} />
          </div>
        </section>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <section>
          <h2 className="text-sm uppercase text-slate-400 font-semibold mb-3">
            Disclosed coverage <span className="text-xs text-slate-500 normal-case">(per-ticker IR pipelines)</span>
          </h2>
          <div className="bg-slate-900/30 border border-slate-800 rounded p-4">
            <CoverageMatrix items={coverage} loading={coverageLoading} />
          </div>
        </section>
        <section>
          <h2 className="text-sm uppercase text-slate-400 font-semibold mb-3">
            Voice eval <span className="text-xs text-slate-500 normal-case">(vs Apple/JPM/Tesla references)</span>
          </h2>
          <div className="bg-slate-900/30 border border-slate-800 rounded p-4">
            <VoiceEvalCard snapshot={voiceEval} loading={voiceLoading} />
          </div>
        </section>
      </div>
    </div>
  );
}
