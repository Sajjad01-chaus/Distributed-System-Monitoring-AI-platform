"""Reliable Redis Streams consumer shared by the persist and detect workers.

Delivery guarantees:
- at-least-once: an entry is XACKed only after it was handled or dead-lettered;
- crash recovery: entries left pending by a dead consumer for CLAIM_IDLE_MS are re-claimed
  (XAUTOCLAIM) by a live one, so scaling workers down or a crash loses nothing;
- poison isolation: an entry that keeps failing is moved to the DLQ after MAX_DELIVERIES
  instead of blocking the group, and a failing batch is retried entry by entry so one bad
  message doesn't sink its neighbours;
- transient failures (DB/Redis down) are *not* dead-lettered: the batch stays pending and
  is retried after a backoff.
Idempotent handlers turn at-least-once delivery into effectively-once results.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Dict, List, Sequence, Tuple

import redis.asyncio as aioredis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.exc import DisconnectionError, InterfaceError, OperationalError

from app import config
from app.pipeline.streams import Envelope, InvalidTelemetry, parse_entry

log = logging.getLogger(__name__)

TRANSIENT_ERRORS = (OperationalError, InterfaceError, DisconnectionError, RedisConnectionError, RedisTimeoutError)
Entry = Tuple[str, Dict[str, str]]


class StreamConsumer:
    group: str = ""

    def __init__(self, redis: aioredis.Redis, consumer_name: str = config.CONSUMER_NAME):
        self.redis = redis
        self.consumer = consumer_name
        self.stream = config.TELEMETRY_STREAM
        self.stopping = False
        self.backoff_s = 0.0

    # --- subclass hook ------------------------------------------------------------
    async def handle(self, envelopes: Sequence[Envelope]) -> None:
        """Process a batch. Must be idempotent. Raise TRANSIENT_ERRORS to retry later."""
        raise NotImplementedError

    # --- lifecycle ----------------------------------------------------------------
    async def ensure_group(self) -> None:
        try:
            # id "0": a brand-new group starts from the beginning of the stream, so nothing
            # enqueued before the first worker came up is skipped.
            await self.redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def reap_idle_consumers(self, max_idle_ms: int = 3_600_000) -> int:
        """Remove consumers left behind by crashed/replaced workers: idle for an hour and owing
        nothing. Consumers with pending entries are kept so XAUTOCLAIM can still recover them."""
        removed = 0
        for c in await self.redis.xinfo_consumers(self.stream, self.group):
            if c["name"] != self.consumer and c["pending"] == 0 and c["idle"] >= max_idle_ms:
                await self.redis.xgroup_delconsumer(self.stream, self.group, c["name"])
                removed += 1
        if removed:
            log.info("%s: reaped %d idle consumers", self.group, removed)
        return removed

    async def run(self) -> None:
        await self.ensure_group()
        await self.reap_idle_consumers()
        log.info("%s consumer %s started", self.group, self.consumer)
        while not self.stopping:
            try:
                await self.run_once()
            except ResponseError as e:
                if "NOGROUP" not in str(e):
                    raise
                # Stream/group vanished (Redis flushed or restarted without persistence): recreate.
                log.warning("%s: consumer group missing, recreating", self.group)
                await self.ensure_group()
                await asyncio.sleep(0.5)   # never spin if the group keeps disappearing
            except TRANSIENT_ERRORS as e:
                self.backoff_s = min(max(self.backoff_s * 2, 0.5), 30.0)
                log.warning("%s: transient error, retrying in %.1fs: %s", self.group, self.backoff_s, e)
                await asyncio.sleep(self.backoff_s)
        log.info("%s consumer %s stopped", self.group, self.consumer)

    async def retire(self) -> bool:
        """On graceful shutdown, leave the group if we own no pending entries, so the group's
        consumer list reflects live workers. With pending entries we stay registered: they must
        remain visible to XAUTOCLAIM so a surviving worker picks them up."""
        mine = await self.redis.xpending_range(self.stream, self.group, min="-", max="+", count=1,
                                               consumername=self.consumer)
        if mine:
            return False
        await self.redis.xgroup_delconsumer(self.stream, self.group, self.consumer)
        return True

    async def run_once(self, block_ms: int | None = None) -> int:
        """One poll: re-claim orphaned entries first, otherwise read new ones. Returns count handled."""
        entries = await self._claim_stale()
        if not entries:
            resp = await self.redis.xreadgroup(self.group, self.consumer, {self.stream: ">"},
                                               count=config.BATCH_SIZE,
                                               block=config.BLOCK_MS if block_ms is None else block_ms)
            entries = resp[0][1] if resp else []
        if entries:
            await self._process(entries)
            self.backoff_s = 0.0
        return len(entries)

    # --- internals ----------------------------------------------------------------
    async def _claim_stale(self) -> List[Entry]:
        _, claimed, deleted = await self.redis.xautoclaim(
            self.stream, self.group, self.consumer, min_idle_time=config.CLAIM_IDLE_MS,
            start_id="0-0", count=config.BATCH_SIZE)
        if deleted:  # trimmed away before anyone processed them (detect group under overload)
            await self.redis.xack(self.stream, self.group, *deleted)
        claimed = [e for e in claimed if e[1] is not None]
        if not claimed:
            return []
        log.info("%s: re-claimed %d orphaned entries", self.group, len(claimed))
        pending = await self.redis.xpending_range(self.stream, self.group, min=claimed[0][0],
                                                  max=claimed[-1][0], count=len(claimed))
        deliveries = {p["message_id"]: p["times_delivered"] for p in pending}
        keep = []
        for entry in claimed:
            if deliveries.get(entry[0], 0) > config.MAX_DELIVERIES:
                await self._dead_letter(entry, f"exceeded {config.MAX_DELIVERIES} deliveries")
            else:
                keep.append(entry)
        return keep

    async def _process(self, entries: List[Entry]) -> None:
        envelopes: List[Envelope] = []
        for entry in entries:
            try:
                envelopes.append(parse_entry(*entry))
            except InvalidTelemetry as e:
                await self._dead_letter(entry, str(e))
        if not envelopes:
            return
        try:
            await self.handle(envelopes)
        except TRANSIENT_ERRORS:
            raise  # leave the whole batch pending; it is retried after backoff / re-claim
        except Exception:
            log.exception("%s: batch of %d failed, isolating per entry", self.group, len(envelopes))
            for env in envelopes:
                try:
                    await self.handle([env])
                except TRANSIENT_ERRORS:
                    raise
                except Exception as e:
                    await self._dead_letter((env.stream_id, {"agent_id": env.agent_id,
                                                             "data": json.dumps(env.payload)}), repr(e))
                    continue
                await self.redis.xack(self.stream, self.group, env.stream_id)
            return
        await self.redis.xack(self.stream, self.group, *(e.stream_id for e in envelopes))

    async def _dead_letter(self, entry: Entry, reason: str) -> None:
        stream_id, fields = entry
        log.warning("%s: dead-lettering %s: %s", self.group, stream_id, reason)
        await self.redis.xadd(config.DLQ_STREAM, {
            "source_id": stream_id, "group": self.group, "reason": reason[:500],
            "agent_id": (fields or {}).get("agent_id", ""), "data": (fields or {}).get("data", ""),
            "failed_at": str(time.time()),
        }, maxlen=100_000, approximate=True)
        await self.redis.xack(self.stream, self.group, stream_id)
