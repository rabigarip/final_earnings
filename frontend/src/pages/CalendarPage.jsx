import { useCallback, useEffect, useMemo, useState } from "react";
import {
  addMonths,
  eachDayOfInterval,
  endOfMonth,
  endOfWeek,
  format,
  isSameDay,
  isSameMonth,
  parseISO,
  startOfMonth,
  startOfWeek,
  subMonths,
} from "date-fns";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "";
const WEEK_STARTS_ON = 1; // Monday

function iso(d) {
  return format(d, "yyyy-MM-dd");
}

function formatNumber(v) {
  if (v == null || Number.isNaN(v)) return "—";
  if (Math.abs(v) >= 1e9) return (v / 1e9).toFixed(2) + "B";
  if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (Math.abs(v) >= 1e3) return (v / 1e3).toFixed(1) + "K";
  return String(v);
}

// Source colour map — one dot per provider so a chip can be skimmed
// without opening the detail panel. Colours match the legend below the
// calendar grid.
const SOURCE_DOT = {
  marketscreener: "bg-sky-400",
  yahoo: "bg-violet-400",
  bloomberg: "bg-orange-400",
};

function EventChip({ event, onClick }) {
  const confirmed = !!event.confirmed;
  const source = (event.source || "").toLowerCase();
  const sourceDot = SOURCE_DOT[source] || "bg-slate-400";
  return (
    <button
      type="button"
      onClick={onClick}
      className={`w-full text-left rounded-md px-2 py-1 text-[11px] leading-tight transition ${
        confirmed
          ? "bg-emerald-900/40 hover:bg-emerald-800/60 border border-emerald-700/60"
          : "bg-slate-800 hover:bg-slate-700 border border-slate-700"
      }`}
      title={`${event.ticker} — ${event.company_name || ""} (${confirmed ? "confirmed" : "estimated"} · ${event.source || "unknown"})`}
    >
      <div className="flex items-center gap-1">
        <span
          className={`h-1.5 w-1.5 rounded-full ${
            confirmed ? "bg-emerald-400" : "bg-amber-400"
          }`}
          aria-label={confirmed ? "confirmed" : "estimated"}
        />
        <span
          className={`h-1.5 w-1.5 rounded-full ${sourceDot}`}
          aria-label={`source: ${event.source || "unknown"}`}
        />
        <span className="font-mono text-blue-300 truncate">{event.ticker}</span>
        {event.company_name ? (
          <span className="text-slate-300 truncate ml-1 text-[10px]">
            {event.company_name}
          </span>
        ) : null}
      </div>
    </button>
  );
}

function EventDetail({ event, onClose, onGeneratePreview, generating }) {
  if (!event) return null;
  return (
    <div className="fixed inset-0 z-50 flex">
      <div
        className="flex-1 bg-black/60"
        onClick={onClose}
        role="presentation"
      />
      <aside className="w-[400px] max-w-full bg-slate-900 border-l border-slate-800 p-6 overflow-auto">
        <div className="flex items-start justify-between mb-4">
          <div>
            <div className="text-xs uppercase tracking-wide text-slate-500">
              {event.country || ""} · {event.sector || ""}
            </div>
            <div className="text-xl font-semibold">
              <span className="font-mono text-blue-300">{event.ticker}</span>
            </div>
            <div className="text-sm text-slate-300">{event.company_name}</div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-slate-400 hover:text-white"
            aria-label="Close"
          >
            ✕
          </button>
        </div>
        <dl className="text-sm grid grid-cols-2 gap-y-2">
          <dt className="text-slate-500">Event date</dt>
          <dd>{event.event_date}</dd>
          <dt className="text-slate-500">Status</dt>
          <dd className={event.confirmed ? "text-emerald-400" : "text-amber-400"}>
            {event.confirmed ? "Confirmed" : "Estimated"}
          </dd>
          <dt className="text-slate-500">Period</dt>
          <dd>{event.period_label || "—"}</dd>
          <dt className="text-slate-500">Consensus revenue</dt>
          <dd>{formatNumber(event.consensus_revenue)}</dd>
          <dt className="text-slate-500">Consensus EPS</dt>
          <dd>{formatNumber(event.consensus_eps)}</dd>
          <dt className="text-slate-500">Source</dt>
          <dd className="capitalize">{event.source || "—"}</dd>
          <dt className="text-slate-500">Last checked</dt>
          <dd className="text-xs text-slate-400">{event.last_checked || "—"}</dd>
        </dl>
        <button
          type="button"
          onClick={() => onGeneratePreview(event.ticker)}
          disabled={generating}
          className="mt-6 w-full rounded-lg bg-blue-600 hover:bg-blue-500 disabled:bg-slate-700 disabled:cursor-not-allowed px-4 py-2 text-sm font-medium"
        >
          {generating ? "Generating…" : "Generate preview & download"}
        </button>
      </aside>
    </div>
  );
}

export default function CalendarPage() {
  const [anchor, setAnchor] = useState(() => startOfMonth(new Date()));
  const [events, setEvents] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshStatus, setRefreshStatus] = useState("");
  const [generating, setGenerating] = useState(false);
  const [confirmedOnly, setConfirmedOnly] = useState(false);
  const [countryFilter, setCountryFilter] = useState("");
  // Sector filter is client-side: the API already returns event.sector, so
  // we filter without re-fetching. Comma-separated, case-insensitive.
  const [sectorFilter, setSectorFilter] = useState("");
  // View toggle: "grid" = month-grid (default), "agenda" = sortable list.
  const [viewMode, setViewMode] = useState("grid");

  const days = useMemo(() => {
    const start = startOfWeek(startOfMonth(anchor), { weekStartsOn: WEEK_STARTS_ON });
    const end = endOfWeek(endOfMonth(anchor), { weekStartsOn: WEEK_STARTS_ON });
    return eachDayOfInterval({ start, end });
  }, [anchor]);

  const rangeStart = days[0];
  const rangeEnd = days[days.length - 1];

  const fetchEvents = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const params = new URLSearchParams({
        start: iso(rangeStart),
        end: iso(rangeEnd),
      });
      if (confirmedOnly) params.set("confirmed", "1");
      if (countryFilter.trim()) params.set("countries", countryFilter.trim());
      const res = await fetch(`${API_BASE}/api/calendar?${params.toString()}`);
      if (!res.ok) throw new Error(`Calendar fetch failed (${res.status})`);
      const data = await res.json();
      setEvents(Array.isArray(data.events) ? data.events : []);
    } catch (e) {
      setError(e?.message || String(e));
      setEvents([]);
    } finally {
      setLoading(false);
    }
  }, [rangeStart, rangeEnd, confirmedOnly, countryFilter]);

  useEffect(() => {
    fetchEvents();
  }, [fetchEvents]);

  // Client-side sector filter. Comma-separated; substring match on
  // event.sector (case-insensitive). Whitespace-only filter = no-op.
  const filteredEvents = useMemo(() => {
    const tokens = sectorFilter
      .split(",")
      .map((s) => s.trim().toLowerCase())
      .filter(Boolean);
    if (tokens.length === 0) return events;
    return events.filter((e) => {
      const s = (e.sector || "").toLowerCase();
      return tokens.some((t) => s.includes(t));
    });
  }, [events, sectorFilter]);

  const eventsByDay = useMemo(() => {
    const map = new Map();
    for (const e of filteredEvents) {
      const d = e.event_date;
      if (!map.has(d)) map.set(d, []);
      map.get(d).push(e);
    }
    return map;
  }, [filteredEvents]);

  // Agenda view sorts by date ascending so the upcoming print is at the top.
  const agendaEvents = useMemo(() => {
    return [...filteredEvents].sort((a, b) => {
      const ad = a.event_date || "";
      const bd = b.event_date || "";
      if (ad !== bd) return ad < bd ? -1 : 1;
      return (a.ticker || "").localeCompare(b.ticker || "");
    });
  }, [filteredEvents]);

  const triggerRefresh = async () => {
    setRefreshing(true);
    setRefreshStatus("Queued…");
    try {
      const res = await fetch(`${API_BASE}/api/calendar/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force: false }),
      });
      if (!res.ok) throw new Error(`Refresh failed (${res.status})`);
      const { job_id } = await res.json();
      // Poll every 2s until completion (or 2 min timeout)
      const started = Date.now();
      while (Date.now() - started < 120_000) {
        await new Promise((r) => setTimeout(r, 2000));
        const s = await fetch(`${API_BASE}/api/calendar/refresh/${job_id}`);
        const job = await s.json();
        setRefreshStatus(
          job.status === "running"
            ? "Running…"
            : job.status === "completed"
            ? `Done: updated ${job.summary?.updated ?? 0}/${job.summary?.total ?? 0}`
            : job.status,
        );
        if (job.status === "completed" || job.status === "failed") {
          await fetchEvents();
          break;
        }
      }
    } catch (e) {
      setRefreshStatus(`Error: ${e?.message || e}`);
    } finally {
      setRefreshing(false);
    }
  };

  const generatePreview = async (ticker) => {
    setGenerating(true);
    try {
      // Use the full pipeline (LLM enabled) so calendar-triggered previews
      // produce the same investment thesis / catalysts / risks as the main
      // flow. Earlier this passed `skip_llm: true` and shipped empty narrative.
      const res = await fetch(`${API_BASE}/api/reports`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ticker }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err?.detail?.summary || err?.detail || "Generation failed");
      }
      const created = await res.json();
      const runId = created?.report?.id;
      if (!runId) throw new Error("No run id");
      const dl = await fetch(`${API_BASE}/api/reports/${runId}/download?t=${Date.now()}`);
      if (!dl.ok) throw new Error("Download failed");
      const blob = await dl.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${ticker}_earnings_preview.pptx`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 100);
    } catch (e) {
      alert(`Could not generate: ${e?.message || e}`);
    } finally {
      setGenerating(false);
    }
  };

  const weekdayLabels = useMemo(
    () => ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    [],
  );

  return (
    <div className="max-w-7xl mx-auto px-6 py-8">
      <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
        <div>
          <h1 className="text-2xl font-semibold">Earnings Calendar</h1>
          <p className="text-sm text-slate-400">
            {format(anchor, "MMMM yyyy")} · {events.length} event
            {events.length === 1 ? "" : "s"} in range
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setAnchor((d) => subMonths(d, 1))}
            className="rounded-md bg-slate-800 hover:bg-slate-700 px-3 py-1 text-sm"
          >
            ◄
          </button>
          <button
            type="button"
            onClick={() => setAnchor(startOfMonth(new Date()))}
            className="rounded-md bg-slate-800 hover:bg-slate-700 px-3 py-1 text-sm"
          >
            Today
          </button>
          <button
            type="button"
            onClick={() => setAnchor((d) => addMonths(d, 1))}
            className="rounded-md bg-slate-800 hover:bg-slate-700 px-3 py-1 text-sm"
          >
            ►
          </button>
          <button
            type="button"
            onClick={triggerRefresh}
            disabled={refreshing}
            className="ml-2 rounded-md bg-blue-600 hover:bg-blue-500 disabled:bg-slate-700 px-3 py-1 text-sm"
          >
            {refreshing ? "Refreshing…" : "Refresh data"}
          </button>
        </div>
      </div>

      <div className="flex flex-wrap gap-3 mb-4 text-sm">
        {/* Grid / Agenda toggle. Defaults to grid; agenda is the
            institutional-list style requested for week-of-print review. */}
        <div className="inline-flex rounded-md border border-slate-700 overflow-hidden">
          <button
            type="button"
            onClick={() => setViewMode("grid")}
            className={`px-3 py-1 text-xs ${
              viewMode === "grid"
                ? "bg-slate-700 text-white"
                : "bg-slate-900 text-slate-300 hover:bg-slate-800"
            }`}
          >
            Grid
          </button>
          <button
            type="button"
            onClick={() => setViewMode("agenda")}
            className={`px-3 py-1 text-xs border-l border-slate-700 ${
              viewMode === "agenda"
                ? "bg-slate-700 text-white"
                : "bg-slate-900 text-slate-300 hover:bg-slate-800"
            }`}
          >
            Agenda
          </button>
        </div>
        <label className="flex items-center gap-2 text-slate-300">
          <input
            type="checkbox"
            checked={confirmedOnly}
            onChange={(e) => setConfirmedOnly(e.target.checked)}
          />
          Confirmed only
        </label>
        <input
          type="text"
          placeholder="Countries (e.g. SA,AE)"
          value={countryFilter}
          onChange={(e) => setCountryFilter(e.target.value)}
          className="rounded-md bg-slate-900 border border-slate-700 px-3 py-1 text-sm w-44"
        />
        <input
          type="text"
          placeholder="Sectors (e.g. financials,energy)"
          value={sectorFilter}
          onChange={(e) => setSectorFilter(e.target.value)}
          className="rounded-md bg-slate-900 border border-slate-700 px-3 py-1 text-sm w-56"
        />
        {refreshStatus ? (
          <span className="text-xs text-slate-400 self-center">{refreshStatus}</span>
        ) : null}
        {loading ? (
          <span className="text-xs text-slate-400 self-center">Loading…</span>
        ) : null}
        {error ? (
          <span className="text-xs text-rose-400 self-center">{error}</span>
        ) : null}
      </div>

      {viewMode === "grid" ? (
        <div className="grid grid-cols-7 gap-[1px] bg-slate-800 border border-slate-800 rounded-lg overflow-hidden">
          {weekdayLabels.map((w) => (
            <div
              key={w}
              className="bg-slate-900 text-xs uppercase tracking-wide text-slate-500 px-2 py-2 text-center"
            >
              {w}
            </div>
          ))}
          {days.map((day) => {
            const key = iso(day);
            const dayEvents = eventsByDay.get(key) || [];
            const inMonth = isSameMonth(day, anchor);
            const today = isSameDay(day, new Date());
            return (
              <div
                key={key}
                className={`min-h-[110px] p-1.5 bg-slate-950 ${
                  inMonth ? "" : "opacity-50"
                }`}
              >
                <div
                  className={`text-xs mb-1 ${
                    today
                      ? "inline-block rounded bg-blue-600 text-white px-1.5"
                      : "text-slate-400"
                  }`}
                >
                  {format(day, "d")}
                </div>
                <div className="flex flex-col gap-1">
                  {dayEvents.slice(0, 4).map((e) => (
                    <EventChip
                      key={`${e.ticker}-${e.event_date}`}
                      event={e}
                      onClick={() => setSelected(e)}
                    />
                  ))}
                  {dayEvents.length > 4 ? (
                    <div className="text-[10px] text-slate-500 pl-1">
                      +{dayEvents.length - 4} more
                    </div>
                  ) : null}
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        // ── Agenda view: sortable list of events in the visible date range. ──
        // Used by institutional reviewers who scan "what's printing this week"
        // without thinking in calendar grid coordinates.
        <div className="border border-slate-800 rounded-lg overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-slate-900 text-slate-400 uppercase text-[10px] tracking-wide">
              <tr>
                <th className="text-left px-3 py-2 w-28">Date</th>
                <th className="text-left px-3 py-2 w-24">Ticker</th>
                <th className="text-left px-3 py-2">Company</th>
                <th className="text-left px-3 py-2 w-28">Country</th>
                <th className="text-left px-3 py-2 w-40">Sector</th>
                <th className="text-left px-3 py-2 w-28">Period</th>
                <th className="text-left px-3 py-2 w-28">Status</th>
                <th className="text-left px-3 py-2 w-32">Source</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {agendaEvents.length === 0 ? (
                <tr>
                  <td colSpan={8} className="px-3 py-6 text-center text-slate-500">
                    No events in range matching the current filters.
                  </td>
                </tr>
              ) : (
                agendaEvents.map((e) => {
                  const confirmed = !!e.confirmed;
                  return (
                    <tr
                      key={`${e.ticker}-${e.event_date}-${e.source || ""}`}
                      onClick={() => setSelected(e)}
                      className="cursor-pointer hover:bg-slate-900/60"
                    >
                      <td className="px-3 py-2 text-slate-300 font-mono text-xs">
                        {e.event_date}
                      </td>
                      <td className="px-3 py-2 font-mono text-blue-300">
                        {e.ticker}
                      </td>
                      <td className="px-3 py-2 text-slate-200 truncate">
                        {e.company_name || "—"}
                      </td>
                      <td className="px-3 py-2 text-slate-400">
                        {e.country || "—"}
                      </td>
                      <td className="px-3 py-2 text-slate-400 truncate">
                        {e.sector || "—"}
                      </td>
                      <td className="px-3 py-2 text-slate-300">
                        {e.period_label || "—"}
                      </td>
                      <td className="px-3 py-2">
                        <span
                          className={`inline-flex items-center gap-1 text-xs ${
                            confirmed ? "text-emerald-400" : "text-amber-400"
                          }`}
                        >
                          <span
                            className={`h-1.5 w-1.5 rounded-full ${
                              confirmed ? "bg-emerald-400" : "bg-amber-400"
                            }`}
                          />
                          {confirmed ? "confirmed" : "estimated"}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-slate-400 capitalize">
                        <span className="inline-flex items-center gap-1">
                          <span
                            className={`h-1.5 w-1.5 rounded-full ${
                              SOURCE_DOT[(e.source || "").toLowerCase()] || "bg-slate-400"
                            }`}
                          />
                          {e.source || "—"}
                        </span>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      )}

      <EventDetail
        event={selected}
        onClose={() => setSelected(null)}
        onGeneratePreview={generatePreview}
        generating={generating}
      />

      <div className="mt-6 text-xs text-slate-500 flex flex-wrap gap-x-5 gap-y-2">
        <span className="text-slate-400 uppercase tracking-wide">Status</span>
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-2 rounded-full bg-emerald-400 inline-block" />
          confirmed
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-2 rounded-full bg-amber-400 inline-block" />
          estimated
        </span>
        <span className="text-slate-400 uppercase tracking-wide ml-2">Source</span>
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-2 rounded-full bg-sky-400 inline-block" />
          MarketScreener
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-2 rounded-full bg-violet-400 inline-block" />
          Yahoo
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-2 rounded-full bg-orange-400 inline-block" />
          Bloomberg
        </span>
      </div>
    </div>
  );
}
