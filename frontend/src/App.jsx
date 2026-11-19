import { useCallback, useEffect, useState } from "react";
import Store from "./views/Store.jsx";
import Dashboard from "./views/Dashboard.jsx";
import { CONFIG, api } from "./api.js";

function BoltMark() {
  return (
    <span className="grid h-9 w-9 place-items-center rounded-xl bg-lime text-ink shadow-glow">
      <svg viewBox="0 0 24 24" className="h-5 w-5" fill="currentColor" aria-hidden="true">
        <path d="M13.5 2 4 14h6l-1.5 8L20 9h-6.5l0-7Z" />
      </svg>
    </span>
  );
}

export default function App() {
  const [tab, setTab] = useState("store");
  const [status, setStatus] = useState({ ready: false, version: "", model: "" });

  const check = useCallback(async () => {
    try {
      const ready = await api.ready();
      setStatus({
        ready: ready.status === "ready",
        version: ready.uptime_s !== undefined ? "v3.0.0" : "",
        model: ready.model_version || "",
      });
    } catch {
      setStatus({ ready: false, version: "", model: "" });
    }
  }, []);

  useEffect(() => {
    check();
    const timer = setInterval(check, 15000);
    return () => clearInterval(timer);
  }, [check]);

  return (
    <div className="min-h-screen">
      <div className="h-px w-full bg-gradient-to-r from-transparent via-cyan/60 to-transparent" />

      <header className="sticky top-0 z-30 border-b border-edge/80 bg-ink/80 backdrop-blur-xl">
        <div className="mx-auto flex max-w-7xl items-center gap-4 px-5 py-3">
          <div className="flex items-center gap-3">
            <BoltMark />
            <div className="leading-tight">
              <p className="text-sm font-extrabold tracking-tight text-white">
                ShopFast <span className="text-slate-500">×</span>{" "}
                <span className="text-lime">FastSense</span>
              </p>
              <p className="mono text-[10px] text-slate-500">recomendaciones en tiempo real</p>
            </div>
          </div>

          <nav className="ml-6 flex items-center gap-1 rounded-xl border border-edge bg-white/[0.02] p-1">
            <button
              onClick={() => setTab("store")}
              className={`rounded-lg px-3 py-1.5 text-sm font-semibold transition ${
                tab === "store" ? "bg-white/10 text-white" : "text-slate-400 hover:text-slate-200"
              }`}
            >
              🛍️ Tienda
            </button>
            <button
              onClick={() => setTab("mlops")}
              className={`rounded-lg px-3 py-1.5 text-sm font-semibold transition ${
                tab === "mlops" ? "bg-white/10 text-white" : "text-slate-400 hover:text-slate-200"
              }`}
            >
              📈 MLOps
            </button>
          </nav>

          <div className="ml-auto flex items-center gap-3">
            <span className="hidden mono text-[11px] text-slate-500 lg:inline">{CONFIG.baseUrl}</span>
            <span
              className={`pill ${
                status.ready
                  ? "border-lime/40 bg-lime/10 text-lime"
                  : "border-amber/40 bg-amber/10 text-amber"
              }`}
            >
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  status.ready ? "bg-lime" : "bg-amber animate-pulse"
                }`}
              />
              {status.ready ? "API ready" : "API warming up"}
            </span>
            <a
              href={`${CONFIG.baseUrl}/docs`}
              target="_blank"
              rel="noreferrer"
              className="btn-ghost hidden sm:inline-flex"
            >
              OpenAPI
            </a>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-5 py-6">
        {tab === "store" ? <Store /> : <Dashboard />}
      </main>

      <footer className="mx-auto max-w-7xl px-5 pb-8 pt-2">
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-edge/70 pt-4 mono text-[11px] text-slate-500">
          <span>FastSense v3.0.0 · Nginx → FastAPI → Redis/MySQL · SQS → Worker → S3</span>
          <span>modelo {status.model || "cargando…"} · demo local segura</span>
        </div>
      </footer>
    </div>
  );
}
