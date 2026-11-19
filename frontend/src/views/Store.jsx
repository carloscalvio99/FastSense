import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import {
  categoryIcon,
  formatNumber,
  formatPrice,
  SOURCE_LABEL,
  SOURCE_STYLE,
} from "../format.js";

function ScoreBar({ score }) {
  const width = Math.max(4, Math.min(100, Math.round(Number(score) * 100)));
  return (
    <div className="mt-3">
      <div className="flex items-center justify-between text-[10px] uppercase tracking-wider text-slate-500">
        <span>Afinidad</span>
        <span className="mono text-slate-300">{Number(score).toFixed(2)}</span>
      </div>
      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-white/5">
        <div
          className="h-full rounded-full bg-gradient-to-r from-cyan to-lime"
          style={{ width: `${width}%` }}
        />
      </div>
    </div>
  );
}

function RecommendationCard({ item, onAdd, onLike, busy }) {
  return (
    <article className="group flex flex-col rounded-2xl border border-edge bg-panel/70 p-4 transition hover:border-cyan/30 hover:bg-panel">
      <div className="relative mb-3 flex h-28 items-center justify-center overflow-hidden rounded-xl border border-edge/80 bg-gradient-to-br from-white/[0.06] to-transparent">
        <span className="text-5xl drop-shadow-lg transition group-hover:scale-110">
          {categoryIcon(item.category)}
        </span>
        <span className="absolute left-2 top-2 rounded-md border border-edge bg-ink/70 px-1.5 py-0.5 mono text-[10px] text-slate-400">
          #{item.rank}
        </span>
        <span className="absolute right-2 top-2 rounded-md border border-lime/20 bg-lime/10 px-1.5 py-0.5 text-[10px] font-bold text-lime">
          {item.category}
        </span>
      </div>

      <h3 className="line-clamp-2 min-h-[2.5rem] text-sm font-semibold leading-snug text-slate-100">
        {item.name}
      </h3>
      <p className="mono mt-1 text-[11px] text-slate-500">{item.product_id}</p>
      <p className="mt-2 text-lg font-extrabold text-white">{formatPrice(item.price)}</p>

      <ScoreBar score={item.score} />

      <div className="mt-3 grid grid-cols-[1fr_auto] gap-2">
        <button className="btn-primary py-1.5 text-[13px]" onClick={() => onAdd(item)} disabled={busy}>
          Agregar al carro
        </button>
        <button
          className="btn-ghost px-2.5 py-1.5"
          title="Enviar interacción rating"
          onClick={() => onLike(item)}
          disabled={busy}
        >
          ♥
        </button>
      </div>
    </article>
  );
}

export default function Store() {
  const [users, setUsers] = useState([]);
  const [userId, setUserId] = useState("u_1001");
  const [recommendations, setRecommendations] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [simulate, setSimulate] = useState(false);
  const [cart, setCart] = useState([]);
  const [events, setEvents] = useState([]);
  const [history, setHistory] = useState([]);
  const [flash, setFlash] = useState("");

  const load = useCallback(
    async (target, forceFailure = simulate) => {
      setLoading(true);
      setError("");
      try {
        const data = await api.recommendations(target, {
          limit: 10,
          simulateFailure: forceFailure,
        });
        setRecommendations(data);
        setHistory((previous) => [data, ...previous].slice(0, 6));
      } catch (err) {
        setError(err.message);
        setRecommendations(null);
      } finally {
        setLoading(false);
      }
    },
    [simulate]
  );

  useEffect(() => {
    api
      .users(50)
      .then((data) => setUsers(data.users || []))
      .catch(() => setUsers([]));
    load("u_1001", false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const options = useMemo(() => {
    const list = [...users];
    if (!list.some((user) => user.user_id === "u_1001")) {
      list.unshift({ user_id: "u_1001", segment: "vip", interactions: 0 });
    }
    return list;
  }, [users]);

  const selected = options.find((user) => user.user_id === userId);
  const cartTotal = cart.reduce((total, item) => total + Number(item.price || 0), 0);
  const source = recommendations?.source;
  const showFlash = (message) => {
    setFlash(message);
    setTimeout(() => setFlash(""), 5000);
  };

  const changeUser = (value) => {
    setUserId(value);
    load(value);
  };

  const track = async (product, eventType, rating) => {
    setBusy(true);
    try {
      const payload = {
        user_id: userId,
        product_id: product.product_id,
        event_type: eventType,
        session_id: "s_web_demo",
      };
      if (rating !== undefined) payload.rating = rating;
      const result = await api.track(payload);
      setEvents((previous) =>
        [
          {
            event_id: result.event_id,
            product_id: product.product_id,
            event_type: eventType,
            accepted_at: result.accepted_at,
          },
          ...previous,
        ].slice(0, 8)
      );
      showFlash(`${eventType} aceptado (202) · ${result.event_id.slice(0, 18)}…`);
    } catch (err) {
      showFlash(`Error al enviar ${eventType}: ${err.message}`);
    } finally {
      setBusy(false);
    }
  };

  const addToCart = async (item) => {
    setCart((previous) =>
      previous.some((product) => product.product_id === item.product_id)
        ? previous
        : [...previous, item]
    );
    await track(item, "cart");
  };

  const like = async (item) => {
    await track(item, "rating", 5);
  };

  const checkout = async () => {
    if (!cart.length) return;
    setBusy(true);
    for (const item of cart) {
      // Se envía una compra por producto; la API responde 202 en <10 ms.
      // eslint-disable-next-line no-await-in-loop
      await track(item, "purchase");
    }
    setCart([]);
    setBusy(false);
    showFlash("Compra enviada al pipeline asíncrono (SQS). El worker la persistirá.");
  };

  const toggleSimulation = async () => {
    const next = !simulate;
    setSimulate(next);
    await load(userId, next);
    showFlash(
      next
        ? "X-Simulate-Failure activo: el SVD falla y se sirve top:global."
        : "Simulación desactivada: vuelve la inferencia SVD."
    );
  };

  return (
    <div className="space-y-5">
      <section className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="label">Experiencia personalizada</p>
          <h1 className="mt-1 text-2xl font-extrabold tracking-tight text-white sm:text-3xl">
            Recomendado para ti
          </h1>
          <p className="mt-1 max-w-xl text-sm text-slate-400">
            Top-10 generado por SVD, servido desde caché Redis o degradado a Most Popular
            si el motor excede el presupuesto de 400 ms.
          </p>
        </div>

        <div className="flex flex-wrap items-end gap-3">
          <label className="block">
            <span className="label">Usuario</span>
            <select
              value={userId}
              onChange={(event) => changeUser(event.target.value)}
              className="mt-1 w-56 rounded-xl border border-edge bg-panel px-3 py-2 text-sm text-slate-200 outline-none focus:border-cyan/50"
            >
              {options.map((user) => (
                <option key={user.user_id} value={user.user_id}>
                  {user.user_id} · {user.segment} · {formatNumber(user.interactions)} int.
                </option>
              ))}
            </select>
          </label>
          <button className="btn-ghost" onClick={() => load(userId)} disabled={loading}>
            {loading ? "Cargando…" : "↻ Actualizar"}
          </button>
        </div>
      </section>

      {flash && (
        <div className="rounded-xl border border-cyan/30 bg-cyan/10 px-4 py-2.5 text-sm text-cyan">
          {flash}
        </div>
      )}

      <section className="flex flex-wrap items-center gap-3 rounded-xl border border-edge bg-panel/60 px-4 py-3">
        <span className={`pill ${SOURCE_STYLE[source] || "border-edge text-slate-400"}`}>
          {SOURCE_LABEL[source] || "consultando…"}
        </span>
        <span className="mono text-[11px] text-slate-500">
          latency_ms <span className="text-slate-200">{recommendations?.latency_ms ?? "—"}</span>
        </span>
        <span className="mono hidden text-[11px] text-slate-500 sm:inline">
          request_id <span className="text-slate-400">{recommendations?.request_id ?? "—"}</span>
        </span>
        <span className="mono hidden text-[11px] text-slate-500 md:inline">
          {selected?.segment ? `segmento ${selected.segment}` : ""}
        </span>
        <button
          onClick={toggleSimulation}
          className={`ml-auto ${simulate ? "btn-danger" : "btn-ghost"} py-1.5 text-[12px]`}
        >
          {simulate ? "Restaurar SVD" : "Simular fallo SVD"}
        </button>
      </section>

      {source === "fallback" && (
        <div className="rounded-xl border border-amber/30 bg-amber/10 px-4 py-3 text-sm text-amber">
          <strong>Degradación elegante activa.</strong> El Circuit Breaker está abierto o el
          SVD agotó los 400 ms: se respondió <span className="mono">HTTP 200</span> con el Top-10
          global (<span className="mono">top:global</span>). El cliente nunca recibe un 5xx.
        </div>
      )}

      {error && (
        <div className="rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-300">
          {error}
        </div>
      )}

      <div className="grid gap-5 lg:grid-cols-[1fr_320px]">
        <section>
          {loading && !recommendations ? (
            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
              {Array.from({ length: 6 }).map((_, index) => (
                <div key={index} className="h-72 animate-pulse rounded-2xl border border-edge bg-panel/50" />
              ))}
            </div>
          ) : recommendations?.items?.length ? (
            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
              {recommendations.items.map((item) => (
                <RecommendationCard
                  key={item.product_id}
                  item={item}
                  onAdd={addToCart}
                  onLike={like}
                  busy={busy}
                />
              ))}
            </div>
          ) : (
            <div className="card text-sm text-slate-400">
              Sin recomendaciones disponibles todavía. El modelo se está entrenando; recarga en
              unos segundos.
            </div>
          )}
        </section>

        <aside className="space-y-4">
          <div className="card">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-bold text-white">Carro de compras</h2>
              <span className="pill border-edge text-slate-400">{cart.length} ítems</span>
            </div>
            {cart.length === 0 ? (
              <p className="mt-3 text-sm text-slate-500">
                Agrega un producto recomendado para generar un evento <span className="mono">cart</span>.
              </p>
            ) : (
              <ul className="mt-3 space-y-2">
                {cart.map((item) => (
                  <li key={item.product_id} className="flex items-center gap-2 text-sm">
                    <span className="text-lg">{categoryIcon(item.category)}</span>
                    <span className="min-w-0 flex-1 truncate text-slate-300">{item.name}</span>
                    <span className="mono text-[11px] text-slate-400">{formatPrice(item.price)}</span>
                  </li>
                ))}
              </ul>
            )}
            <div className="mt-4 flex items-center justify-between border-t border-edge pt-3">
              <span className="label">Total</span>
              <span className="text-lg font-extrabold text-white">{formatPrice(cartTotal)}</span>
            </div>
            <button className="btn-primary mt-3 w-full" onClick={checkout} disabled={!cart.length || busy}>
              Pagar y emitir purchase
            </button>
          </div>

          <div className="card">
            <h2 className="text-sm font-bold text-white">Ingesta asíncrona</h2>
            <p className="mt-1 text-[12px] text-slate-500">
              POST /track-interaction → SQS → Worker → MySQL + Parquet/S3
            </p>
            <ul className="mt-3 space-y-2">
              {events.length === 0 && (
                <li className="text-sm text-slate-500">Aún no hay eventos en esta sesión.</li>
              )}
              {events.map((event) => (
                <li key={event.event_id} className="rounded-lg border border-edge bg-white/[0.02] px-2.5 py-2">
                  <div className="flex items-center justify-between gap-2">
                    <span className="pill border-lime/30 bg-lime/10 text-lime">202</span>
                    <span className="mono truncate text-[10px] text-slate-500">{event.event_id}</span>
                  </div>
                  <p className="mono mt-1 text-[11px] text-slate-400">
                    {event.event_type} · {event.product_id}
                  </p>
                </li>
              ))}
            </ul>
          </div>

          <div className="card">
            <h2 className="text-sm font-bold text-white">Últimas respuestas</h2>
            <div className="mt-3 space-y-2">
              {history.map((entry, index) => (
                <div
                  key={`${entry.request_id}-${index}`}
                  className="flex items-center justify-between gap-2 border-b border-edge/60 pb-2 last:border-0 last:pb-0"
                >
                  <span className={`pill ${SOURCE_STYLE[entry.source] || "border-edge"}`}>
                    {entry.source}
                  </span>
                  <span className="mono text-[11px] text-slate-400">{entry.latency_ms} ms</span>
                  <span className="mono max-w-[110px] truncate text-[10px] text-slate-600">
                    {entry.request_id}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}
