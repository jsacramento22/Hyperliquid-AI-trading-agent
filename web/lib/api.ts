import type {
  Account,
  CostResponse,
  DecisionsResponse,
  EquityResponse,
  FillsResponse,
  Health,
  LeverageApplyResponse,
  ModelApplyResponse,
  Risk,
  RuntimeState,
  TradesResponse,
} from "./types";

const BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

// Optional basic-auth credentials for when the API is behind nginx + auth
// (e.g., deployed on a public droplet). Local dev typically has neither set
// and the headers stay empty.
const API_USER = process.env.NEXT_PUBLIC_API_USER || "";
const API_PASS = process.env.NEXT_PUBLIC_API_PASS || "";

function authHeader(): Record<string, string> {
  if (!API_USER || !API_PASS) return {};
  // btoa is browser-native; on the server side Next renders client components
  // only after hydration so this runs in the browser context.
  const token = btoa(`${API_USER}:${API_PASS}`);
  return { Authorization: `Basic ${token}` };
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(`${BASE}${path}`, {
    cache: "no-store",
    headers: authHeader(),
  });
  if (!r.ok) {
    throw new Error(`GET ${path} → ${r.status}: ${await r.text()}`);
  }
  return (await r.json()) as T;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json", ...authHeader() },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!r.ok) {
    let detail = await r.text();
    try {
      const j = JSON.parse(detail);
      detail = j.detail ?? detail;
    } catch {
      // detail is plain text
    }
    throw new Error(`POST ${path} → ${r.status}: ${detail}`);
  }
  return (await r.json()) as T;
}

export const api = {
  health: () => get<Health>("/api/health"),
  account: () => get<Account>("/api/account"),
  equity: (hours = 24) => get<EquityResponse>(`/api/equity?hours=${hours}`),
  decisions: (limit = 20) =>
    get<DecisionsResponse>(`/api/decisions?limit=${limit}`),
  fills: (limit = 50) => get<FillsResponse>(`/api/fills?limit=${limit}`),
  trades: (limit = 100, days = 365) =>
    get<TradesResponse>(`/api/trades?limit=${limit}&days=${days}`),
  cost: (hours = 24) => get<CostResponse>(`/api/cost?hours=${hours}`),
  runtime: () => get<RuntimeState>("/api/runtime"),
  pause: (paused: boolean) =>
    post<{ paused: boolean }>("/api/pause", { paused }),
  setRisk: (overrides: Partial<Risk>) =>
    post<{ risk_overrides: Partial<Risk>; effective_risk: Risk }>(
      "/api/risk",
      overrides,
    ),
  setLeverage: (body: { leverage?: number; is_cross?: boolean }) =>
    post<LeverageApplyResponse>("/api/leverage", body),
  setModel: (model: string) =>
    post<ModelApplyResponse>("/api/model", { model }),
};
