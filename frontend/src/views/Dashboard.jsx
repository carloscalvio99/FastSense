import { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { formatNumber, timeAgo } from "../format.js";

function Stat({ label, value, hint, accent = "text-white", suffix }) {
  return (
    <div className="card">
      <p className="label">{label}</p>
      <p className={`mt-2 text-2xl font-extrabold tracking-tight ${accent}`}>
        {value}
        {suffix && <span className="ml-1 text-sm font-semibold text-slate-500">{suffix}</span>}
      </p>
      {hint && <p className="mt-1 text-[11px] text-slate-500">{hint}</p>}
    </div>
  );
}

function Bar({ label, value, total, color }) {
  const percent = total > 0 ? Math.round((value / total) * 100) : 0;
  return (
    <div>
      <div className="flex items-center justify-between text-[11px]">
        <span className="text-slate-400">{label}</span>
        <span className="mono text-slate-300">
          {formatNumber(value)} · {percent}%
        </span>
      </div>
      <div className="mt-1 h-2 overflow-hidden rounded-full bg-white/5">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${Math.max(percent, value > 0 ? 2 : 0)}%` }} />
      </div>
    </div>
  );
}

export default function Dashboard() {
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [testUser, setTestUser] = useState("u_1001");
  const [lastRefresh, setLastRefresh] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const data = await api.summary();
      setSummary(data);
      setError("");
      setLastRefresh(new Date());
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 4000);
    return () => clearInterval(timer);
  }, [refresh]);

  const run = async (label, action) => {
    setBusy(true);
    setMessage("");
    try {
      const result = await action();
      setMessage(`${label}: ${typeof result === "string" ? result : "OK"}`);
      await refresh();
    } catch (err) {
      setMessage(`${label}: ${err.message}`);
    } finally {
      setBusy(false);
      setTimeout(() => setMessage(""), 8000);
    }
  };

  const sendEvents = async () => {
    setBusy(true);
    setMessage("");
    try {
      const types = ["click", "click", "click", "cart", "purchase", "rating"];
      const requests = Array.from({ length: 20 }).map((_, index) => {
        const eventType = types[index % types.length];
        const payload = {
          user_id: testUser,
          product_id: `p_${1001 + Math.floor(Math.random() * 5000)}`,
          event_type: eventType,
          session_id: "s_mlops_dashboard",
        };
        if (eventType === "rating") payload.rating = 4.5;
        return api.track(payload);
      });
      const results = await Promise.all(requests);
      setMessage(
        `20 eventos aceptados (HTTP 202). Primer event_id: ${results[0].event_id}. El worker los persistirá en MySQL y Parquet/S3.`
      );
      await refresh();
    } catch (err) {
      setMessage(`Error al enviar eventos: ${err.message}`);
    } finally {
      setBusy(false);
    }
  };

  const model = summary?.model || {};
  const signals = summary?.golden_signals || {};
  const latency = signals.latency || {};
  const traffic = signals.traffic || {};
  const errors = signals.errors || {};
  const saturation = signals.saturation || {};
  const sources = summary?.sources || {};
  const breaker = summary?.circuit_breaker || {};
  const tracking = summary?.tracking || {};
  const events = summary?.events || {};
  const totalSources = (sources.cache || 0) + (sources.svd || 0) + (sources.fallback || 0);
  const rmseOk = model.rmse !== null && model.rmse !== undefined && Number(model.rmse) <= 0.85;

  return (
    <div className="space-y-5">
      <section className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="label">Observabilidad MLOps</p>
          <h1 className="mt-1 text-2xl font-extrabold tracking-tight text-white sm:text-3xl">
            Golden Signals & modelo vivo
          </h1>
          <p className="mt-1 max-w-2xl text-sm text-slate-400">
            Métricas compartidas por las réplicas vía Redis, quality gate del SVD y estado del
            pipeline SQS → MySQL → S3.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span className="mono text-[11px] text-slate-500">
            refresco 4 s · {lastRefresh ? lastRefresh.toLocaleTimeString("es-CL") : "—"}
          </span>
          <button className="btn-ghost" onClick={refresh} disabled={busy}>
            ↻ Refrescar
          </button>
        </div>
      </section>

      {error && (
        <div className="rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-300">
          {error}
        </div>
      )}
      {message && (
        <div className="rounded-xl border border-cyan/30 bg-cyan/10 px-4 py-3 text-sm text-cyan">{message}</div>
      )}

      <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat
          label="Modelo · RMSE holdout"
          value={model.rmse !== undefined && model.rmse !== null ? Number(model.rmse).toFixed(4) : "—"}
          hint={`Quality gate ≤ ${model.gate_rmse_max ?? 0.85} · ${rmseOk ? "PASS" : "PENDIENTE"}`}
          accent={rmseOk ? "text-lime" : "text-amber"}
        />
        <Stat
          label="Latencia p95"
          value={formatNumber(latency.p95_ms ?? 0, 0)}
          suffix="ms"
          hint={`p50 ${formatNumber(latency.p50_ms ?? 0, 0)} ms · p99 ${formatNumber(latency.p99_ms ?? 0, 0)} ms`}
          accent="text-cyan"
        />
        <Stat
          label="Cache hit rate"
          value={`${(Number(summary?.cache_hit_rate || 0) * 100).toFixed(1)}%`}
          hint={`${formatNumber(sources.cache || 0)} hits de ${formatNumber(totalSources)} requests`}
          accent="text-lime"
        />
        <Stat
          label="Errores 5xx"
          value={`${(Number(errors.rate || 0) * 100).toFixed(2)}%`}
          hint={`${formatNumber(errors.count || 0)} errores · ${formatNumber(traffic.requests_total || 0)} requests`}
          accent="text-white"
        />
      </section>

      <div className="grid gap-5 xl:grid-cols-3">
        <section className="card xl:col-span-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-sm font-bold text-white">Modelo en producción</h2>
            <span
              className={`pill ${
                model.model_loaded
                  ? "border-lime/40 bg-lime/10 text-lime"
                  : "border-amber/40 bg-amber/10 text-amber"
              }`}
            >
              {model.model_loaded ? "model_loaded = true" : "cargando modelo"}
            </span>
          </div>

          <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <div>
              <p className="label">Versión</p>
              <p className="mono mt-1 text-sm text-cyan">{model.version || "—"}</p>
            </div>
            <div>
              <p className="label">Precision@10</p>
              <p className="mt-1 text-sm font-bold text-white">
                {model.precision_at_10 !== null && model.precision_at_10 !== undefined
                  ? Number(model.precision_at_10).toFixed(3)
                  : "—"}
              </p>
            </div>
            <div>
              <p className="label">Usuarios / ítems</p>
              <p className="mt-1 text-sm font-bold text-white">
                {formatNumber(model.n_users || 0)} / {formatNumber(model.n_items || 0)}
              </p>
            </div>
            <div>
              <p className="label">Entrenado</p>
              <p className="mt-1 text-sm text-slate-300">{timeAgo(model.trained_at)}</p>
            </div>
          </div>

          <div className="mt-4 rounded-xl border border-edge bg-ink/40 p-3">
            <p className="label">Artefacto versionado (S3 + joblib zlib-3)</p>
            <p className="mono mt-1 break-all text-[11px] text-slate-400">{model.s3_uri || "—"}</p>
          </div>

          <div className="mt-4 grid gap-4 sm:grid-cols-3">
            <div className="rounded-xl border border-edge bg-white/[0.02] p-3">
              <p className="label">Circuit Breaker</p>
              <p
                className={`mt-1 text-lg font-extrabold ${
                  breaker.state === "open"
                    ? "text-red-400"
                    : breaker.state === "half_open"
                    ? "text-amber"
                    : "text-lime"
                }`}
              >
                {(breaker.state || "closed").toUpperCase()}
              </p>
              <p className="mono mt-1 text-[10px] text-slate-500">
                fails {breaker.fail_counter ?? 0}/{breaker.fail_max ?? 3} · reset {breaker.reset_timeout_s ?? 30}s
              </p>
            </div>
            <div className="rounded-xl border border-edge bg-white/[0.02] p-3">
              <p className="label">SVD timeout</p>
              <p className="mt-1 text-lg font-extrabold text-white">{breaker.timeout_ms ?? 400} ms</p>
              <p className="mono mt-1 text-[10px] text-slate-500">
                simulación {summary?.simulation?.enabled ? "ON" : "OFF"}
              </p>
            </div>
            <div className="rounded-xl border border-edge bg-white/[0.02] p-3">
              <p className="label">Uptime</p>
              <p className="mt-1 text-lg font-extrabold text-white">
                {formatNumber(summary?.uptime_s || 0, 0)} s
              </p>
              <p className="mono mt-1 text-[10px] text-slate-500">proceso API actual</p>
            </div>
          </div>
        </section>

        <section className="card space-y-4">
          <h2 className="text-sm font-bold text-white">Distribución de `source`</h2>
          <Bar label="cache" value={sources.cache || 0} total={totalSources} color="bg-cyan" />
          <Bar label="svd" value={sources.svd || 0} total={totalSources} color="bg-lime" />
          <Bar label="fallback" value={sources.fallback || 0} total={totalSources} color="bg-amber" />
          <div className="border-t border-edge pt-3">
            <div className="flex items-center justify-between text-[12px]">
              <span className="text-slate-400">Throughput</span>
              <span className="mono text-slate-200">{formatNumber(traffic.throughput_rps || 0, 3)} req/s</span>
            </div>
            <div className="mt-2 flex items-center justify-between text-[12px]">
              <span className="text-slate-400">Redis</span>
              <span className={`mono ${saturation.cache_connected ? "text-lime" : "text-red-400"}`}>
                {saturation.cache_connected ? "conectado" : "sin conexión"}
              </span>
            </div>
            <div className="mt-2 flex items-center justify-between text-[12px]">
              <span className="text-slate-400">Cola SQS</span>
              <span className="mono text-slate-200">{formatNumber(saturation.queue_depth ?? -1)}</span>
            </div>
            <div className="mt-2 flex items-center justify-between text-[12px]">
              <span className="text-slate-400">DLQ</span>
              <span className={`mono ${(saturation.dlq_depth ?? 0) > 0 ? "text-amber" : "text-slate-200"}`}>
                {formatNumber(saturation.dlq_depth ?? -1)}
              </span>
            </div>
          </div>
        </section>
      </div>

      <div className="grid gap-5 xl:grid-cols-3">
        <section className="card">
          <h2 className="text-sm font-bold text-white">Control de caos (demo)</h2>
          <p className="mt-1 text-[12px] text-slate-500">
            Fuerza el Circuit Breaker y verifica la degradación elegante sin reiniciar nada.
          </p>
          <div className="mt-3 grid gap-2">
            <button
              className="btn-danger"
              disabled={busy}
              onClick={() => run("Fallo SVD forzado", () => api.simulateFailure(true, 0))}
            >
              Abrir Circuit Breaker
            </button>
            <button
              className="btn-ghost"
              disabled={busy}
              onClick={() => run("Circuit Breaker restaurado", () => api.resetBreaker())}
            >
              Cerrar / resetear breaker
            </button>
            <button
              className="btn-primary"
              disabled={busy}
              onClick={() => run("Hot reload", () => api.reloadModel())}
            >
              Recargar modelo (hot reload)
            </button>
          </div>
        </section>

        <section className="card">
          <h2 className="text-sm font-bold text-white">Prueba de ingesta asíncrona</h2>
          <p className="mt-1 text-[12px] text-slate-500">
            Envía 20 eventos reales y observa el pipeline SQS → Worker → MySQL/S3.
          </p>
          <label className="mt-3 block">
            <span className="label">user_id</span>
            <input
              value={testUser}
              onChange={(event) => setTestUser(event.target.value)}
              className="mono mt-1 w-full rounded-xl border border-edge bg-ink px-3 py-2 text-sm text-slate-200 outline-none focus:border-cyan/50"
            />
          </label>
          <button className="btn-primary mt-3 w-full" onClick={sendEvents} disabled={busy}>
            Enviar 20 interacciones
          </button>
          <div className="mt-3 grid grid-cols-3 gap-2 text-center">
            <div className="rounded-lg border border-edge bg-white/[0.02] p-2">
              <p className="label">Aceptados</p>
              <p className="mono mt-1 text-sm text-lime">{formatNumber(tracking.accepted || 0)}</p>
            </div>
            <div className="rounded-lg border border-edge bg-white/[0.02] p-2">
              <p className="label">Fallidos</p>
              <p className="mono mt-1 text-sm text-red-300">{formatNumber(tracking.failed || 0)}</p>
            </div>
            <div className="rounded-lg border border-edge bg-white/[0.02] p-2">
              <p className="label">Persistidos</p>
              <p className="mono mt-1 text-sm text-cyan">{formatNumber(events.ingested ?? 0)}</p>
            </div>
          </div>
        </section>

        <section className="card">
          <h2 className="text-sm font-bold text-white">Pipeline end-to-end</h2>
          <ol className="mt-3 space-y-2">
            {[
              ["Nginx :8080", "least_conn · keepalive 32 · 100 r/m"],
              ["API FastAPI :8080", "2 workers · UID 1000 · logs JSON"],
              ["Redis", "rec:{user_id} TTL 3600 · top:global"],
              ["MySQL 8.0", "USUARIO · PRODUCTO · INTERACCION"],
              ["SQS + DLQ", "maxReceiveCount=3 · long polling"],
              ["Worker", "SHA-256 · INSERT IGNORE · Parquet"],
              ["S3 Data Lake", "versionado · models/model.pkl"],
              ["ml-trainer", "SVD 50f/20e · gate RMSE ≤ 0.85"],
            ].map(([title, detail], index) => (
              <li key={title} className="flex gap-3">
                <span className="mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full border border-edge bg-white/[0.03] mono text-[10px] text-cyan">
                  {index + 1}
                </span>
                <div>
                  <p className="text-[13px] font-semibold text-slate-200">{title}</p>
                  <p className="mono text-[10px] text-slate-500">{detail}</p>
                </div>
              </li>
            ))}
          </ol>
        </section>
      </div>

      {loading && !summary && (
        <p className="text-center text-sm text-slate-500">Consultando métricas…</p>
      )}
    </div>
  );
}
