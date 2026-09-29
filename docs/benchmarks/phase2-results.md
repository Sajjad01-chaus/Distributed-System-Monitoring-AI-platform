# Phase 2 results: ingestion pipeline and per-agent detection

**Headline:** on the same laptop, workload and seed as the [Phase 1 baseline](phase1-baseline.md),
the pipeline delivers **every message at every load level**. It sustained **770 msg/s** (~100× the
old ~8 msg/s ceiling, and the ceiling itself wasn't reached) with p50 end-to-end latency of 10–17 ms.
The API stays responsive (`/health` p99 ≈ 5 ms), duplicates are stored exactly once, a killed
worker loses nothing, and detection finds 92–100% of injected faults while flagging **0 of 160**
healthy hosts.

Design rationale: [../architecture.md](../architecture.md). Methodology and caveats: [methodology.md](methodology.md).

## 1. Like-for-like against the baseline

Same command as Phase 1 (25 → 400 agents, 10 s interval, 60 s stages, seed 42, default scenario mix
including duplicate and out-of-order senders), 3 runs, 1 persist + 1 detect worker.
Reports: [run 1](phase2-ramp-run1.json) · [run 2](phase2-ramp-run2.json) · [run 3](phase2-ramp-run3.json).

| Metric | 25 agents | 50 agents | 100 agents | 200 agents | 400 agents |
|---|---|---|---|---|---|
| Agents connected | 25 | 50 | 100 | 200 | 400 |
| Target msg/s | 2.5 | 5.0 | 10.0 | 20.0 | 40.0 |
| Delivered msg/s | 2.5 | 5.1 | 10.1 | 20.2 [19.8–20.2] | 40.6 [40.6–41.5] |
| Failed connects | 0 | 0 | 0 | 0 | 0 |
| E2E p50 (ms) | 12 [11–13] | 12 [11–13] | 12 [10–12] | 10 [10–11] | 10 [10–11] |
| E2E p99 (ms) | 90 [48–103] | 48 [21–51] | 41 [31–42] | 34 [20–45] | 27 [22–46] |
| `/health` p99 (ms) | 4 [4–4] | 4 [4–4] | 4 [4–5] | 5 [4–6] | 5 [5–6] |
| Healthy hosts falsely flagged | 0% | 0% | 0% | 0% | 0% |

Median [min–max] over 3 runs; all 15 stages passed the stall and generator checks.

**Side by side at 400 agents** (baseline medians from [phase1-baseline.md](phase1-baseline.md)):

| | Phase 1 | Phase 2 |
|---|---|---|
| Agents connected | 163 / 400 | **400 / 400** |
| Delivered msg/s (target 40) | 7.4 | **40.6** |
| Failed connection attempts | 1,654 | **0** |
| E2E p50 / p99 | 817 ms / 3,115 ms | **10 ms / 27 ms** |
| `/health` p99 | 5,088 ms | **5 ms** |
| Healthy hosts falsely flagged | 22% | **0%** |
| Duplicates stored (14 sent) | 14 | **0** |

In every one of the 15 stages, rows persisted = unique messages sent. The default mix includes
hosts that deliberately re-send messages, and none of those duplicates reached the database.

## 2. Capacity: how far past the old ceiling

200 agents with shorter intervals, healthy hosts only. Reports: `phase2-capacity-w{1,3}-i{1,0.5,0.25}.json`.

| Target | Workers per group | Delivered msg/s | E2E p50 | E2E p99 | `/health` p99 |
|---|---|---|---|---|---|
| 200 msg/s | 1 | 193.9 | 13 ms | 27 ms | 47 ms |
| 400 msg/s | 1 | 386.2 | 16 ms | 39 ms | 42 ms |
| 800 msg/s | 1 | 770.8 | 53 ms | 116 ms | 31 ms |
| 200 msg/s | 3 | 193.9 | 9 ms | 59 ms | 27 ms |
| 400 msg/s | 3 | 386.2 | 11 ms | 31 ms | 39 ms |
| 800 msg/s | 3 | 770.8 | **17 ms** | **47 ms** | 46 ms |

Every message was delivered and persisted in all six runs, with no backlog left after the drain.
Delivered is ~4% under target because the simulator jitters each agent's interval by ±10%.

- **The throughput ceiling was not reached.** At 800 msg/s the single-process API, the workers, the
  databases and the load generator all share one 12-vCPU laptop VM, so the next limit is as likely
  to be the generator as the backend. Finding the real ceiling needs a separate load machine.
- **Adding workers helps where it should.** At 800 msg/s one persist worker keeps up but queues
  (p50 53 ms); three workers bring p50 down to 17 ms. That is horizontal scaling of the consumer
  group, with no code change.

## 3. Crash recovery: kill a worker holding unacked messages

[`scripts/chaos-kill-persist-worker.sh`](../../scripts/chaos-kill-persist-worker.sh): 100 agents at
100 msg/s and 2 persist workers. A 10 s table lock makes both workers block mid-INSERT, each holding a batch it
has read but not acknowledged. One worker is then `docker kill`ed. Report: [phase2-crash.json](phase2-crash.json).

| Check | Result |
|---|---|
| Unique messages sent / persisted | 9,296 / **9,296**, no loss |
| Survivor log | `persist: re-claimed 1 orphaned entries`, 24 s after the kill |
| Rows by storage delay | **1 row at 30 s** (the orphan: `CLAIM_IDLE_MS`), ~550 rows at 6–10 s (backlog from the 10 s lock, drained afterwards), rest < 5 s |

The first version of this test killed a worker at a random moment and lost nothing, but it didn't
exercise recovery at all: with ~1 ms batches the worker happened to hold nothing when it died.
The table lock makes the dangerous case (dying with in-flight messages) certain.

## 4. Detection quality

200 agents, 10 s interval, **5 minutes**, default mix, seed 42 (never used while tuning; the
threshold regression test uses seeds 1000+). Report: [phase2-detection.json](phase2-detection.json).

| Injected fault | Hosts | Detected | Recall | Phase 1 recall |
|---|---|---|---|---|
| Memory leak | 12 | 12 | **100%** | 1% |
| Disk filling | 8 | 8 | **100%** (forecast before full) | not reached |
| Network degradation | 8 | 8 | **100%** | not reached |
| CPU spikes | 12 | 11 | **92%** (11/11 where a spike occurred) | 32% |
| **Healthy hosts flagged** | 160 | 0 | **0% false positives** | 21–40% |

"Not reached": in Phase 1's 60 s stages these faults never crossed the old engine's absolute
thresholds, so recall wasn't measurable.

How the false-positive rate got to zero, from 15% on the first Phase 2 run: every false alarm
was traced to a detector drawing a conclusion from too little evidence. That meant forecasting
disk-full a day ahead from 2 minutes of data, calling a 12-sample wiggle a "leak", and treating
a 1 → 11 ms latency change as "9× degradation". Each detector now requires minimum evidence:
fit quality, a real total rise, extrapolation of at most 10× the observed span, a robust z-score
against the host's own noise, and an absolute floor. See `backend/app/detection/detectors.py`
and `backend/tests/test_detection_quality.py`.

**Caveats.** The thresholds match the simulator's time scale (10 s samples, faults developing
over minutes). On real fleets they are per-environment configuration, and these numbers show
the method works, not what to expect everywhere. The one "missed" CPU-spike host never spiked:
its spikes are randomly timed, and in this run its CPU peaked at 29.8%, so there was nothing to detect.
