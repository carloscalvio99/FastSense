/**
 * Cliente HTTP de la demo.
 *
 * - Toda petición pública envía X-API-Key + Bearer JWT (contrato de seguridad).
 * - Los endpoints /internal/* se autentican con X-Internal-Token y están
 *   excluidos del middleware de JWT (uso exclusivo de demo/MLOps local).
 * - X-Demo-Bypass desactiva el rate limit de Nginx solo en esta demo controlada.
 */
export const CONFIG = {
  baseUrl: (import.meta.env.VITE_API_BASE_URL || "http://localhost:8080").replace(/\/$/, ""),
  apiKey: import.meta.env.VITE_API_KEY || "",
  jwt: import.meta.env.VITE_JWT_TOKEN || "",
  internalToken: import.meta.env.VITE_INTERNAL_TOKEN || "",
};

async function request(path, { method = "GET", body, internal = false, simulateFailure = false, timeoutMs = 8000 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const headers = {
    "Content-Type": "application/json",
    "X-API-Key": CONFIG.apiKey,
    Authorization: `Bearer ${CONFIG.jwt}`,
    "X-Demo-Bypass": "fastsense-demo-bypass",
  };
  if (internal) headers["X-Internal-Token"] = CONFIG.internalToken;
  if (simulateFailure) headers["X-Simulate-Failure"] = "true";

  try {
    const response = await fetch(`${CONFIG.baseUrl}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = { detail: text };
    }
    if (!response.ok) {
      const detail = typeof data?.detail === "string" ? data.detail : JSON.stringify(data?.detail ?? data);
      throw new Error(detail || `HTTP ${response.status}`);
    }
    return data;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("Tiempo de espera agotado (8 s)");
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export const api = {
  health: () => request("/health"),
  ready: () => request("/ready"),
  users: (limit = 50) => request(`/users?limit=${limit}`),
  recommendations: (userId, { limit = 10, simulateFailure = false } = {}) =>
    request(`/recommendations/${encodeURIComponent(userId)}?limit=${limit}`, { simulateFailure }),
  track: (payload) => request("/track-interaction", { method: "POST", body: payload }),
  catalog: (limit = 24) => request(`/catalog?limit=${limit}`),
  summary: () => request("/metrics/summary"),
  simulateFailure: (enabled = true, latencyMs = 0) =>
    request(`/internal/simulate-failure?enabled=${enabled}&latency_ms=${latencyMs}`, {
      method: "POST",
      internal: true,
    }),
  resetBreaker: () => request("/internal/reset-circuit-breaker", { method: "POST", internal: true }),
  reloadModel: () => request("/internal/reload-model", { method: "POST", internal: true, timeoutMs: 30000 }),
};
