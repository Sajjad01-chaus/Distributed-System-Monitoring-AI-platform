"use client";

import { FormEvent, useState } from "react";
import { AlertRow, AgentRow, API_URL, ApiError, api } from "@/lib/api";
import { usePoll, useLiveEvents } from "@/lib/hooks";

const REMEDIATION_FOR: Record<string, string> = {
  cpu_threshold_breach: "cpu_threshold_breach",
  disk_threshold_breach: "disk_threshold_breach",
  disk_full_forecast: "disk_threshold_breach",
};

function ago(iso: string | null): string {
  if (!iso) return "never";
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

function Stat({ label, value, tone }: { label: string; value: number | string | undefined; tone?: string }) {
  return (
    <div className="card stat">
      <div className="stat-label">{label}</div>
      <div className={`stat-value ${tone ?? ""}`}>{value ?? "…"}</div>
    </div>
  );
}

function Sparkline({ values }: { values: number[] }) {
  const w = 300, h = 60, max = Math.max(1, ...values);
  const points = values.map((v, i) => `${(i / (values.length - 1)) * w},${h - (v / max) * (h - 4) - 2}`).join(" ");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="spark" role="img" aria-label="Ingest rate, last 60 seconds">
      <polyline points={points} fill="none" stroke="var(--accent)" strokeWidth="2" />
    </svg>
  );
}

function Login({ onToken }: { onToken: (t: string | null) => void }) {
  const [user, setUser] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    try {
      const t = await api.login(user, password);
      if (t.role !== "admin") throw new Error("admin role required for actions");
      onToken(t.access_token);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };
  return (
    <form className="login" onSubmit={submit}>
      <input aria-label="Username" value={user} onChange={(e) => setUser(e.target.value)} />
      <input aria-label="Password" type="password" placeholder="password" value={password}
             onChange={(e) => setPassword(e.target.value)} />
      <button type="submit">Sign in for actions</button>
      {error && <span className="error">{error}</span>}
    </form>
  );
}

export default function Dashboard() {
  const status = usePoll(api.status, 5000);
  const pipeline = usePoll(api.pipeline, 5000);
  const agents = usePoll(api.agents, 10000);
  const alerts = usePoll(api.activeAlerts, 5000);
  const live = useLiveEvents();
  const [token, setToken] = useState<string | null>(null);
  const [actions, setActions] = useState<Record<string, string>>({});

  const note = (key: string, text: string) => setActions((a) => ({ ...a, [key]: text }));

  const remediate = async (agent: string, issue: string) => {
    if (!token) return;
    note(agent, "sending…");
    try {
      const cmd = await api.remediate(agent, issue, token);
      note(agent, `delivered via ${cmd.routed_to}`);
      // The agent reports back asynchronously; poll the command until it completes.
      for (let i = 0; i < 10; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        const c = await api.command(cmd.command_id, token);
        if (c.status === "completed") {
          note(agent, c.result?.success ? "completed ✓" : "completed (failed)");
          return;
        }
      }
      note(agent, "delivered, no result yet");
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setToken(null);
      note(agent, `error: ${e instanceof Error ? e.message : e}`);
    }
  };

  const resolve = async (alert: AlertRow) => {
    if (!token) return;
    try {
      await api.resolveAlert(alert.id, token);
      alerts.refresh();
    } catch (e) {
      note(`alert-${alert.id}`, `error: ${e instanceof Error ? e.message : e}`);
    }
  };

  const s = status.data;
  const p = pipeline.data;
  const rateNow = live.rate[live.rate.length - 1];
  const agentRows: AgentRow[] = (agents.data ?? [])
    .slice()
    .sort((a, b) => (b.last_seen ?? "").localeCompare(a.last_seen ?? ""))
    .slice(0, 25);

  return (
    <main>
      <header>
        <div>
          <h1>Fleet monitor</h1>
          <p className="muted">
            {API_URL} · <span className={live.connected ? "ok" : "bad"}>{live.connected ? "live" : "reconnecting…"}</span>
            {s && <> · answered by {s.this_replica.id}</>}
          </p>
        </div>
        {token ? <button onClick={() => setToken(null)}>Sign out</button> : <Login onToken={setToken} />}
      </header>

      {status.error && <p className="error banner">API unreachable: {status.error}</p>}

      <section className="grid stats">
        <Stat label="Agents reporting" value={s?.connected_agents} />
        <Stat label="Healthy" value={s?.healthy_agents} tone="ok" />
        <Stat label="Offline" value={s?.offline_agents} tone={s?.offline_agents ? "warn" : ""} />
        <Stat label="Active alerts" value={s?.active_alerts} tone={s?.active_alerts ? "bad" : ""} />
      </section>

      <section className="grid two">
        <div className="card">
          <h2>Ingest (live) <span className="muted">{rateNow} samples/s</span></h2>
          <Sparkline values={live.rate} />
        </div>
        <div className="card">
          <h2>Pipeline</h2>
          {p ? (
            <dl className="kv">
              {Object.entries(p.groups).map(([name, g]) => (
                <div key={name}><dt>{name} backlog</dt><dd>{(g.lag ?? 0) + g.pending} · {g.consumers} consumers</dd></div>
              ))}
              <div><dt>dead letters</dt><dd className={p.dead_letters ? "bad" : ""}>{p.dead_letters}</dd></div>
              <div><dt>admission</dt><dd className={p.admission.overloaded ? "bad" : "ok"}>
                {p.admission.overloaded ? "throttling" : "accepting"}</dd></div>
            </dl>
          ) : <p className="muted">…</p>}
        </div>
      </section>

      <section className="grid two">
        <div className="card">
          <h2>Active alerts</h2>
          <table>
            <thead><tr><th>Agent</th><th>Type</th><th>Severity</th><th>Since</th>{token && <th />}</tr></thead>
            <tbody>
              {(alerts.data ?? []).slice(0, 15).map((a) => (
                <tr key={a.id} title={a.description}>
                  <td className="mono">{a.agent_id}</td>
                  <td>{a.alert_type}</td>
                  <td><span className={`pill ${a.severity}`}>{a.severity}</span></td>
                  <td>{ago(a.timestamp)}</td>
                  {token && (
                    <td className="actions">
                      {REMEDIATION_FOR[a.alert_type] && (
                        <button onClick={() => remediate(a.agent_id, REMEDIATION_FOR[a.alert_type])}>Remediate</button>
                      )}
                      <button onClick={() => resolve(a)}>Resolve</button>
                      <span className="muted">{actions[a.agent_id] ?? actions[`alert-${a.id}`]}</span>
                    </td>
                  )}
                </tr>
              ))}
              {alerts.data?.length === 0 && <tr><td colSpan={5} className="muted">No active alerts</td></tr>}
            </tbody>
          </table>
        </div>

        <div className="card">
          <h2>Live events</h2>
          <ul className="feed">
            {live.events.map((e) => (
              <li key={e.id}>
                <span className={`pill ${e.type}`}>{e.type.replace("_", " ")}</span>
                <span className="mono">{e.agent_id}</span> {e.summary}
              </li>
            ))}
            {live.events.length === 0 && <li className="muted">Waiting for events…</li>}
          </ul>
        </div>
      </section>

      <section className="card">
        <h2>Agents <span className="muted">({agents.data?.length ?? "…"} known, most recent first)</span></h2>
        <table>
          <thead><tr><th>Agent</th><th>Host</th><th>Platform</th><th>Status</th><th>Last seen</th></tr></thead>
          <tbody>
            {agentRows.map((a) => (
              <tr key={a.agent_id}>
                <td className="mono">{a.agent_id}</td>
                <td>{a.hostname}</td>
                <td>{a.platform}</td>
                <td><span className={`pill ${a.status}`}>{a.status}</span></td>
                <td>{ago(a.last_seen)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </main>
  );
}
