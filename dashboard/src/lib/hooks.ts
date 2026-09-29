"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { WS_URL } from "./api";

/** Poll `fetcher` every `intervalMs`; keeps the last good value if a poll fails. */
export function usePoll<T>(fetcher: () => Promise<T>, intervalMs: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const refresh = useCallback(async () => {
    try {
      setData(await fetcherRef.current());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, intervalMs);
    return () => clearInterval(id);
  }, [refresh, intervalMs]);

  return { data, error, refresh };
}

export type LiveEvent = { id: number; at: number; type: string; agent_id?: string; summary: string };

const RATE_WINDOW_S = 60;

function summarize(msg: Record<string, unknown>): string {
  switch (msg.type) {
    case "anomaly_detected":
      return ((msg.anomalies as { description?: string }[]) ?? []).map((a) => a.description).join("; ");
    case "alert_resolved":
      return `${msg.alert_type} resolved`;
    case "agent_offline":
      return "agent stopped reporting";
    case "agent_online":
      return "agent is reporting again";
    case "remediation_result":
      return `remediation ${msg.success ? "succeeded" : "failed"}${msg.dry_run ? " (dry run)" : ""}`;
    default:
      return String(msg.type);
  }
}

/**
 * Subscribes to /ws/dashboard. High-volume `metrics_update` messages are only counted
 * (per-second ingest rate); everything else goes into a capped event feed.
 * Reconnects with exponential backoff, like the agents do.
 */
export function useLiveEvents(maxEvents = 50) {
  const [connected, setConnected] = useState(false);
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [rate, setRate] = useState<number[]>(() => Array(RATE_WINDOW_S).fill(0));
  const counter = useRef(0);
  const nextId = useRef(0);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout>;
    let backoff = 1000;
    let closed = false;

    const connect = () => {
      ws = new WebSocket(`${WS_URL}/ws/dashboard`);
      ws.onopen = () => {
        setConnected(true);
        backoff = 1000;
      };
      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.type === "metrics_update") {
          counter.current += 1;
          return;
        }
        if (msg.type === "pong") return;
        const event = { id: nextId.current++, at: Date.now(), type: msg.type, agent_id: msg.agent_id, summary: summarize(msg) };
        setEvents((prev) => [event, ...prev].slice(0, maxEvents));
      };
      ws.onclose = () => {
        setConnected(false);
        if (closed) return;
        retry = setTimeout(connect, backoff * (0.5 + Math.random()));
        backoff = Math.min(backoff * 2, 30_000);
      };
    };
    connect();

    const tick = setInterval(() => {
      const n = counter.current;
      counter.current = 0;
      setRate((prev) => [...prev.slice(1), n]);
    }, 1000);

    return () => {
      closed = true;
      clearTimeout(retry);
      clearInterval(tick);
      ws?.close();
    };
  }, [maxEvents]);

  return { connected, events, rate };
}
