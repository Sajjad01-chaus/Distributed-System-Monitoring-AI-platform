// Typed client for the monitoring API. The base URL is baked in at build time
// (NEXT_PUBLIC_API_URL), e.g. http://localhost:8000 locally or the Render URL in production.
export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");
export const WS_URL = API_URL.replace(/^http/, "ws");

export type SystemStatus = {
  total_agents: number;
  connected_agents: number;
  healthy_agents: number;
  offline_agents: number;
  active_alerts: number;
  anomalies_24h: number;
  system_health: string;
  this_replica: { id: string };
};

export type Pipeline = {
  stream_length: number;
  groups: Record<string, { lag: number | null; pending: number; consumers: number }>;
  dead_letters: number;
  admission: { replica: string; overloaded: boolean; persist_lag: number; max_lag: number };
};

export type AgentRow = { agent_id: string; hostname: string; platform: string; status: string; last_seen: string | null };

export type AlertRow = {
  id: number;
  agent_id: string;
  alert_type: string;
  severity: string;
  description: string;
  status: string;
  occurrences: number;
  timestamp: string | null;
  last_seen: string | null;
};

export type Command = { command_id: string; status: string; routed_to?: string; result?: { success?: boolean } };

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}, token?: string | null): Promise<T> {
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body) headers.set("Content-Type", "application/json");
  const res = await fetch(`${API_URL}${path}`, { ...init, headers, cache: "no-store" });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : body.detail?.status ?? JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  status: () => request<SystemStatus>("/api/v1/system/status"),
  pipeline: () => request<Pipeline>("/api/v1/system/pipeline"),
  agents: () => request<{ agents: AgentRow[] }>("/api/v1/agents/").then((r) => r.agents),
  activeAlerts: () => request<{ alerts: AlertRow[] }>("/api/v1/alerts/?status=active&limit=50").then((r) => r.alerts),
  login: (username: string, password: string) =>
    request<{ access_token: string; role: string; expires_in: number }>("/api/v1/auth/token", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  remediate: (agentId: string, issueType: string, token: string) =>
    request<Command>(`/api/v1/agents/${encodeURIComponent(agentId)}/remediate?issue_type=${issueType}`,
      { method: "POST" }, token),
  command: (commandId: string, token: string) => request<Command>(`/api/v1/commands/${commandId}`, {}, token),
  resolveAlert: (alertId: number, token: string) =>
    request<{ message: string }>(`/api/v1/alerts/${alertId}/resolve`, { method: "POST" }, token),
};
