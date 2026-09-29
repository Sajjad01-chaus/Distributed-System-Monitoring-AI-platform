# Phase 1 baseline: the single-process backend under a synthetic fleet

**Headline:** the current backend delivers at most **~8 messages/s** on this hardware. At the real
agent's default 30 s reporting interval that is roughly **240 hosts** before latency explodes and
agents start being locked out. The limit is not the database or the network: it is an ML model
re-trained on every message, synchronously, on the event loop.

## Setup

| | |
|---|---|
| Machine | Windows 11 laptop, Docker Desktop VM with 12 vCPU / 7.9 GB |
| Stack | `docker compose` from this repo: TimescaleDB (pg14), Redis, backend (1 uvicorn process) |
| Load generator | `simulator/fleet.py` in a container on the same Docker network (shares the VM's CPU) |
| Workload | 25 → 400 agents, one message every 10 s each (2.5 → 40 msg/s), 60 s per stage after a 10 s ramp-up, 30 s drain; default scenario mix; seed 42 |
| Repetitions | 3 independent runs, backend restarted before each; raw reports: [run 1](phase1-baseline-run1.json), [run 2](phase1-baseline-run2.json), [run 3](phase1-baseline-run3.json) |

```bash
docker compose up -d --wait
docker compose --profile loadtest run --rm --no-deps fleet \
  --agents 25,50,100,200,400 --interval 10 --duration 60 --ramp-up 10 --drain 30 \
  --seed 42 --report /reports/phase1-baseline-run1.json
python simulator/summarize.py docs/benchmarks/phase1-baseline-run*.json
```

## Results

| Metric | 25 agents | 50 agents | 100 agents | 200 agents | 400 agents |
|---|---|---|---|---|---|
| Agents connected | 25 | 50 | 88 [88–89] | 114 [114–115] | 163 [122–170] |
| Target msg/s | 2.5 | 5.0 | 10.0 | 20.0 | 40.0 |
| Delivered msg/s | 2.4 | 4.8 [4.8–4.9] | 7.9 [7.9–8.1] | 7.4 [7.1–7.6] | 7.4 [7.1–7.4] |
| Failed connects | 0 | 0 | 71 [67–72] | 610 [602–614] | 1654 [1642–1695] |
| E2E p50 (ms) | 121 [121–121] | 152 [145–159] | 4,399 [4,076–5,677] | 954 [710–2,590] | 817 [748–827] |
| E2E p99 (ms) | 1,344 [423–2,105] | 1,454 [753–1,696] | 10,446 [9,318–15,486] | 3,122 [2,011–4,836] | 3,115 [2,576–3,428] |
| `/health` p99 (ms) | 201 [158–247] | 651 [628–672] | 2,357 [2,215–7,512] | 2,727 [2,004–8,737] | 5,088 [4,787–6,619] |
| Healthy hosts falsely flagged | 33% | 40% | 39% [33%–41%] | 28% [23%–31%] | 22% [21%–29%] |

Median [min–max] over 3 runs. All 15 stages passed the stall and generator-saturation checks.
E2E = agent send → dashboard receive (WebSocket ingest + DB write + AI + broadcast).

## What the numbers say

1. **Throughput ceiling ≈ 8 msg/s.** Delivered throughput tracks demand up to 50 agents, then
   flattens at 7–8 msg/s no matter how many agents try. A standalone micro-benchmark of the AI
   engine measured **~133 ms per message** after warm-up, i.e. ≈ 7.5 msg/s: the ceiling is the AI
   engine. It refits an `IsolationForest` on every message, and it runs inside the WebSocket
   handler, so nothing else on the server can run meanwhile.

2. **The whole API stalls, not just ingestion.** `/health` does no I/O at all, yet its p99 reaches
   2–5 s under load, and is already 200 ms with only 25 agents. Any request, including the
   dashboard's REST calls, waits behind the AI work.

3. **Overload shows up as agents being locked out, not as errors.** Beyond ~90 agents the server
   can't complete WebSocket handshakes in time: at 400 agents only ~40% ever connect, with ~1,650
   failed attempts in 70 s. This is accidental admission control. It's why p50 latency at 200–400
   agents (~0.8 s) is *lower* than at 100 (~4.4 s): at 100, nearly everyone gets in and a backlog
   builds; at 400, the handshake timeouts shed the excess. Nothing tells an operator this is happening.

4. **Duplicates are stored.** Across all 15 stages, persisted rows = frames sent: every duplicate the
   fleet re-sent became an extra row. There is no idempotency key.

5. **Detection quality is poor, for structural reasons.**
   - **Memory leaks: 1 of 81 leaking hosts detected (1%).** The trend detector looks at the last
     15 samples, but that history is one global window shared by *all* agents, so a single host's
     leak is diluted by everyone else's data.
   - **CPU spikes: 24 of 76 hosts detected (32%)**, mostly by the plain > 80% threshold.
   - **21–40% of healthy hosts get flagged** within about a minute. The model's `contamination=0.1`
     forces ~10% of samples to be called outliers by definition, so every host eventually "has an anomaly".
   - Disk-fill and network-degradation faults ramp slowly and do not reach the detection
     thresholds within a 60 s stage, so their recall is **not evaluated** here. A dedicated
     long-run detection benchmark is planned.

## What Phase 2 changes, and how we'll check

| Finding | Phase 2 change | Success looks like |
|---|---|---|
| AI on the request path | Ingest only acknowledges and enqueues to Redis Streams; AI runs in separate worker processes | `/health` p99 flat (< 50 ms) at every stage |
| Ceiling ~8 msg/s | Horizontally scalable consumer-group workers; incremental models instead of per-message refits | Delivered msg/s tracks target through 400 agents |
| Silent lockout | Explicit backpressure and rate limits with clear status codes, and metrics for rejected load | Failed connects ≈ 0; shed load is visible |
| Duplicates stored | Idempotency on `(agent_id, seq)` | Persisted rows = unique messages |
| Global AI history | Per-agent state and models | Memory-leak recall ≫ 1%, lower false-positive rate |

The same command, seed and summarizer will be run against Phase 2 so the comparison is like-for-like.
