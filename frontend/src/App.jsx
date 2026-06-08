import { BrowserRouter, Link, NavLink, Route, Routes } from "react-router-dom";
import CalendarPage from "./pages/CalendarPage";
import DashboardPage from "./pages/DashboardPage";
import GenerateReportPage from "./pages/GenerateReportPage";

function NavBar() {
  const base = "px-3 py-1.5 rounded-md text-sm";
  const idle = "text-slate-300 hover:bg-slate-800";
  const active = "bg-slate-800 text-white";
  return (
    <header className="border-b border-slate-800 bg-slate-950/90 backdrop-blur sticky top-0 z-40">
      <div className="max-w-7xl mx-auto px-6 py-3 flex items-center justify-between">
        <Link to="/" className="text-sm font-semibold tracking-wide">
          Earnings Research
        </Link>
        <nav className="flex items-center gap-1">
          <NavLink
            to="/"
            end
            className={({ isActive }) => `${base} ${isActive ? active : idle}`}
          >
            Generate
          </NavLink>
          <NavLink
            to="/dashboard"
            className={({ isActive }) => `${base} ${isActive ? active : idle}`}
          >
            Dashboard
          </NavLink>
          <NavLink
            to="/calendar"
            className={({ isActive }) => `${base} ${isActive ? active : idle}`}
          >
            Calendar
          </NavLink>
        </nav>
      </div>
    </header>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <div className="min-h-screen bg-slate-950 text-slate-100">
        <NavBar />
        <main>
          <Routes>
            <Route path="/" element={<GenerateReportPage />} />
            <Route path="/dashboard" element={<DashboardPage />} />
            <Route path="/calendar" element={<CalendarPage />} />
            <Route path="*" element={<GenerateReportPage />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  );
}

