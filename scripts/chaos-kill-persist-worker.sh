#!/usr/bin/env bash
# Crash-recovery check: kill a persist worker while it is guaranteed to hold unacked
# messages, and verify nothing is lost. A 10 s table lock makes both workers block
# mid-INSERT (each holding a batch it has read but not XACKed); one is then killed. The
# survivor must re-claim the orphaned batch via XAUTOCLAIM after CLAIM_IDLE_MS (30 s).
#
#   ./scripts/chaos-kill-persist-worker.sh      # needs the compose stack and .env
# Pass: persisted_rows == sent_unique in docs/benchmarks/phase2-crash.json, and the
# survivor logs "re-claimed N orphaned entries".
set -euo pipefail
export MSYS_NO_PATHCONV=1   # Git Bash on Windows: don't rewrite /reports paths
docker compose up -d --wait --scale worker-persist=2 backend worker-persist worker-detect
(
  sleep 35
  docker compose exec -T postgres psql -U "${POSTGRES_USER:-monitor_user}" -d "${POSTGRES_DB:-system_monitor}" \
    -c "BEGIN; LOCK TABLE system_metrics IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep(10); COMMIT;" >/dev/null &
  sleep 4
  echo "in flight before kill: $(docker compose exec -T redis redis-cli XPENDING telemetry persist | head -1)"
  victim=$(docker compose ps -q worker-persist | head -1)
  docker kill "$victim" >/dev/null && echo "killed $(docker inspect -f '{{.Name}}' "$victim")"
  wait
) &
docker compose --profile loadtest run --rm --no-deps -T fleet --agents 100 --interval 1 --duration 90 \
  --ramp-up 5 --drain 60 --seed 42 --mix normal=1 --run-prefix crash --report /reports/phase2-crash.json
wait
docker compose logs worker-persist | grep -E "re-claimed" || { echo "FAIL: no re-claim logged"; exit 1; }
