"""Runtime settings, all from the environment so the same image runs as API or worker."""
import os
import socket


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

TELEMETRY_STREAM = os.getenv("TELEMETRY_STREAM", "telemetry")
DLQ_STREAM = os.getenv("DLQ_STREAM", "telemetry:dlq")
DASHBOARD_CHANNEL = os.getenv("DASHBOARD_CHANNEL", "dashboard")
PERSIST_GROUP = "persist"
DETECT_GROUP = "detect"

# Hard memory cap on the stream. Admission control (PERSIST_MAX_LAG) rejects new telemetry
# long before this, so trimming can only ever drop entries the persist group already has;
# the detect group is allowed to fall behind and lose work under overload.
STREAM_MAXLEN = _int("STREAM_MAXLEN", 500_000)
PERSIST_MAX_LAG = _int("PERSIST_MAX_LAG", 50_000)

MAX_MESSAGE_BYTES = _int("MAX_MESSAGE_BYTES", 256 * 1024)
BATCH_SIZE = _int("WORKER_BATCH_SIZE", 500)
BLOCK_MS = _int("WORKER_BLOCK_MS", 1000)
# Entries unacked this long are presumed orphaned by a dead consumer and are re-claimed.
CLAIM_IDLE_MS = _int("CLAIM_IDLE_MS", 30_000)
MAX_DELIVERIES = _int("MAX_DELIVERIES", 5)
DEDUP_TTL_S = _int("DEDUP_TTL_S", 3600)

CONSUMER_NAME = os.getenv("CONSUMER_NAME", f"{socket.gethostname()}-{os.getpid()}")
