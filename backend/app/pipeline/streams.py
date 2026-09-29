"""Telemetry envelope + Redis Stream plumbing shared by the API and the workers."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

import redis.asyncio as aioredis

from app import config

AGENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
MAX_CLOCK_SKEW = timedelta(days=1)


def default_redis() -> aioredis.Redis:
    return aioredis.from_url(config.REDIS_URL, decode_responses=True)


# Tests swap this for a fakeredis client; everything else calls redis_factory().
redis_factory: Callable[[], aioredis.Redis] = default_redis


class InvalidTelemetry(ValueError):
    """Message can never be processed (bad JSON, wrong shape). Goes to the DLQ, never retried."""


@dataclass(frozen=True)
class Envelope:
    stream_id: str
    agent_id: str
    boot_id: str
    seq: int
    ts: datetime
    payload: Dict[str, Any]

    @property
    def key(self) -> tuple:
        return (self.agent_id, self.boot_id, self.seq)


def _stream_ms(stream_id: str) -> int:
    return int(stream_id.split("-", 1)[0])


def _parse_ts(value: Any, received: datetime) -> datetime:
    """Agent clocks are untrusted: naive times are UTC, and anything more than a day away
    from the receive time is replaced by the receive time (keeps Timescale chunks sane)."""
    try:
        ts = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return received
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts if abs(ts - received) <= MAX_CLOCK_SKEW else received


def parse_entry(stream_id: str, fields: Dict[str, str]) -> Envelope:
    try:
        agent_id = fields["agent_id"]
        payload = json.loads(fields["data"])
    except (KeyError, TypeError, json.JSONDecodeError) as e:
        raise InvalidTelemetry(f"unreadable entry: {e}") from e
    if not isinstance(payload, dict):
        raise InvalidTelemetry("payload is not a JSON object")

    received = datetime.fromtimestamp(_stream_ms(stream_id) / 1000, tz=timezone.utc)
    boot_id, seq = payload.get("boot_id"), payload.get("seq")
    if not (isinstance(boot_id, str) and boot_id and isinstance(seq, int) and not isinstance(seq, bool)):
        # Legacy agent without a sequence number: the stream id is still unique per message,
        # so storage stays correct; only cross-connection dedup is lost.
        ms, n = stream_id.split("-", 1)
        boot_id, seq = "srv", int(ms) * 1000 + int(n)
    return Envelope(stream_id, agent_id, boot_id[:64], seq, _parse_ts(payload.get("timestamp"), received), payload)


def validate_agent_message(agent_id: str, raw: str) -> Optional[Dict[str, Any]]:
    """API-side checks before enqueueing. Returns the parsed payload or raises InvalidTelemetry."""
    if len(raw.encode()) > config.MAX_MESSAGE_BYTES:
        raise InvalidTelemetry(f"message exceeds {config.MAX_MESSAGE_BYTES} bytes")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise InvalidTelemetry("invalid JSON") from e
    if not isinstance(payload, dict):
        raise InvalidTelemetry("payload must be a JSON object")
    claimed = payload.get("agent_id", agent_id)
    if claimed != agent_id:
        raise InvalidTelemetry("agent_id in payload does not match the connection")
    return payload


async def enqueue(redis: aioredis.Redis, agent_id: str, raw: str) -> str:
    return await redis.xadd(config.TELEMETRY_STREAM, {"agent_id": agent_id, "data": raw},
                            maxlen=config.STREAM_MAXLEN, approximate=True)


async def publish(redis: aioredis.Redis, event: Dict[str, Any]) -> None:
    await redis.publish(config.DASHBOARD_CHANNEL, json.dumps(event, default=str))


async def group_backlog(redis: aioredis.Redis, group: str) -> int:
    """Entries the group hasn't finished: never delivered (lag) + delivered but unacked (pending).
    Redis reports lag as null only after XDEL of unread entries, which this system never does;
    pending alone is the fallback then."""
    try:
        groups = await redis.xinfo_groups(config.TELEMETRY_STREAM)
    except aioredis.ResponseError:
        return 0   # stream not created yet
    for g in groups:
        if g["name"] == group:
            return (g.get("lag") or 0) + (g.get("pending") or 0)
    return 0
