"""Delivery guarantees of the stream consumers, tested directly against fakeredis + SQLite."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app import config
from app.database import SessionLocal
from app.models import Agent, Alert, SystemMetrics
from app.pipeline.consumer import StreamConsumer
from app.pipeline.detect import DetectConsumer
from app.pipeline.persist import PersistConsumer
from app.pipeline.streams import enqueue, parse_entry



def sample(seq=1, boot="boot-a", agent="agent-1", mem=40.0, **extra):
    ts = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc) + timedelta(seconds=10 * seq)
    return {"agent_id": agent, "boot_id": boot, "seq": seq, "timestamp": ts.isoformat(),
            "cpu_usage": 10.0, "memory_usage": mem, "disk_usage": 20.0, "network_latency": 5.0,
            "platform": {"system": "Linux", "node": "host-1"}, **extra}


async def send(redis, payload, agent="agent-1"):
    return await enqueue(redis, agent, json.dumps(payload))


def count_rows(model=SystemMetrics) -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(model))


async def pending(redis, group) -> int:
    return (await redis.xpending(config.TELEMETRY_STREAM, group))["pending"]


async def collect(pubsub, polls=10):
    """All messages currently published (get_message returns None while it handles
    subscribe confirmations, so a single None doesn't mean the channel is empty)."""
    out = []
    for _ in range(polls):
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.05)
        if msg is not None:
            out.append(json.loads(msg["data"]))
    return out


async def drain(consumer, rounds=3):
    await consumer.ensure_group()
    for _ in range(rounds):
        await consumer.run_once(block_ms=1)


@pytest.mark.asyncio
async def test_persist_writes_rows_and_registers_agent(redis_client):
    for seq in (1, 2, 3):
        await send(redis_client, sample(seq=seq))
    await drain(PersistConsumer(redis_client, consumer_name="p1"))

    assert count_rows() == 3
    assert await pending(redis_client, config.PERSIST_GROUP) == 0
    with SessionLocal() as db:
        agent = db.scalar(select(Agent).where(Agent.agent_id == "agent-1"))
    assert agent.hostname == "host-1" and agent.status == "healthy"


@pytest.mark.asyncio
async def test_duplicates_and_redeliveries_are_stored_once(redis_client):
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(config.DASHBOARD_CHANNEL)

    await send(redis_client, sample(seq=7))
    await send(redis_client, sample(seq=7))          # duplicate inside the same batch
    consumer = PersistConsumer(redis_client, consumer_name="p1")
    await drain(consumer)
    await send(redis_client, sample(seq=7))          # replay arriving later
    await drain(consumer)

    events = await collect(pubsub)
    assert count_rows() == 1
    assert [e["type"] for e in events] == ["metrics_update"], "duplicates must not re-announce"


@pytest.mark.asyncio
async def test_crashed_consumer_entries_are_reclaimed(redis_client, fast_claims):
    await send(redis_client, sample(seq=1))
    crashed = PersistConsumer(redis_client, consumer_name="crashed")
    await crashed.ensure_group()
    # Reads the entry, then "dies" before handling/acking it.
    await redis_client.xreadgroup(config.PERSIST_GROUP, "crashed", {config.TELEMETRY_STREAM: ">"}, count=10)
    assert await pending(redis_client, config.PERSIST_GROUP) == 1 and count_rows() == 0

    survivor = PersistConsumer(redis_client, consumer_name="survivor")
    await survivor.run_once(block_ms=1)

    assert count_rows() == 1
    assert await pending(redis_client, config.PERSIST_GROUP) == 0


@pytest.mark.asyncio
async def test_unparseable_entry_goes_to_dlq_and_neighbours_survive(redis_client):
    await redis_client.xadd(config.TELEMETRY_STREAM, {"agent_id": "agent-1", "data": "{not json"})
    await send(redis_client, sample(seq=1))
    await drain(PersistConsumer(redis_client, consumer_name="p1"))

    assert count_rows() == 1
    dlq = await redis_client.xrange(config.DLQ_STREAM)
    assert len(dlq) == 1 and "unreadable" in dlq[0][1]["reason"]
    assert await pending(redis_client, config.PERSIST_GROUP) == 0


class Flaky(StreamConsumer):
    group = "flaky"

    def __init__(self, redis, error):
        super().__init__(redis, consumer_name="f1")
        self.error, self.calls = error, 0

    async def handle(self, envelopes):
        self.calls += 1
        if any(e.seq == 2 for e in envelopes):
            raise self.error


@pytest.mark.asyncio
async def test_poison_message_is_isolated_not_the_whole_batch(redis_client):
    for seq in (1, 2, 3):
        await send(redis_client, sample(seq=seq))
    consumer = Flaky(redis_client, ValueError("bad data"))
    await drain(consumer)

    dlq = await redis_client.xrange(config.DLQ_STREAM)
    assert len(dlq) == 1 and json.loads(dlq[0][1]["data"])["seq"] == 2
    assert await pending(redis_client, "flaky") == 0          # 1 and 3 acked, 2 dead-lettered


@pytest.mark.asyncio
async def test_transient_failure_keeps_batch_pending_for_retry(redis_client):
    await send(redis_client, sample(seq=2))
    consumer = Flaky(redis_client, OperationalError("SELECT 1", {}, Exception("db down")))
    await consumer.ensure_group()
    with pytest.raises(OperationalError):
        await consumer.run_once(block_ms=1)

    assert await redis_client.xlen(config.DLQ_STREAM) == 0     # not dead-lettered
    assert await pending(redis_client, "flaky") == 1           # still owed, will be retried


@pytest.mark.asyncio
async def test_entry_failing_too_often_is_dead_lettered(redis_client, fast_claims, monkeypatch):
    monkeypatch.setattr(config, "MAX_DELIVERIES", 2)
    await send(redis_client, sample(seq=2))
    consumer = Flaky(redis_client, OperationalError("SELECT 1", {}, Exception("still down")))
    await consumer.ensure_group()
    for _ in range(5):
        try:
            await consumer.run_once(block_ms=1)
        except OperationalError:
            pass

    assert consumer.calls == 2, "handled exactly MAX_DELIVERIES times, then given up on"
    dlq = await redis_client.xrange(config.DLQ_STREAM)
    assert len(dlq) == 1 and "deliveries" in dlq[0][1]["reason"]
    assert await pending(redis_client, "flaky") == 0


@pytest.mark.asyncio
async def test_legacy_message_without_seq_is_still_stored(redis_client):
    legacy = sample()
    del legacy["boot_id"], legacy["seq"]
    await send(redis_client, legacy)
    await send(redis_client, legacy)       # identical body, but distinct messages
    await drain(PersistConsumer(redis_client, consumer_name="p1"))

    with SessionLocal() as db:
        boots = db.scalars(select(SystemMetrics.boot_id)).all()
    assert boots == ["srv", "srv"]


def test_untrusted_agent_clock_is_clamped():
    far_future = sample(timestamp="2099-01-01T00:00:00")
    env = parse_entry("1790000000000-0", {"agent_id": "agent-1", "data": json.dumps(far_future)})
    assert env.ts == datetime.fromtimestamp(1790000000, tz=timezone.utc)
    naive = parse_entry("1790000000000-0", {"agent_id": "a", "data": json.dumps(
        sample(timestamp=datetime.fromtimestamp(1790000000).astimezone(timezone.utc)
               .replace(tzinfo=None).isoformat()))})
    assert naive.ts.tzinfo is not None


@pytest.mark.asyncio
async def test_alert_lifecycle_opens_on_transition_and_resolves_with_hysteresis(redis_client, monkeypatch):
    monkeypatch.setattr(config, "DETECT_CLEAR_AFTER", 2)
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(config.DASHBOARD_CHANNEL)
    detect = DetectConsumer(redis_client, consumer_name="d1")

    def alerts():
        with SessionLocal() as db:
            return db.scalars(select(Alert).where(Alert.alert_type == "memory_threshold_breach")).all()

    for seq in (1, 2, 3):
        await send(redis_client, sample(seq=seq, mem=95.0))
    await drain(detect)
    [alert] = alerts()
    assert alert.status == "active" and alert.occurrences == 1
    assert [e["type"] for e in await collect(pubsub)] == ["anomaly_detected"]

    await send(redis_client, sample(seq=3, mem=95.0))     # duplicate
    await send(redis_client, sample(seq=4, mem=95.0))     # still firing
    await drain(detect)
    assert [a.occurrences for a in alerts()] == [1], "steady firing is not a new alert"
    assert await collect(pubsub) == [], "and causes no dashboard traffic"

    # Recovering: the condition stops holding at seq 7; resolves after 2 clear evaluations.
    for seq in (5, 6, 7):
        await send(redis_client, sample(seq=seq, mem=50.0))
        await drain(detect)
    assert alerts()[0].status == "active", "one clear evaluation is not enough (hysteresis)"
    await send(redis_client, sample(seq=8, mem=50.0))
    await drain(detect)
    assert alerts()[0].status == "resolved"
    assert [e["type"] for e in await collect(pubsub)] == ["alert_resolved"]

    # A relapse opens a fresh alert rather than reviving the resolved one.
    for seq in (9, 10, 11):
        await send(redis_client, sample(seq=seq, mem=96.0))
    await drain(detect)
    assert sorted(a.status for a in alerts()) == ["active", "resolved"]


@pytest.mark.asyncio
async def test_detect_state_is_shared_so_any_worker_can_take_any_agent(redis_client):
    """Consumer groups spread one agent's messages over workers; the window lives in Redis,
    so a leak split across two workers is still seen as one continuous trend."""
    workers = [DetectConsumer(redis_client, consumer_name=f"d{i}") for i in range(2)]
    for w in workers:
        await w.ensure_group()
    for seq in range(1, 13):
        await send(redis_client, sample(seq=seq, mem=40 + 2.0 * seq))   # +12 %/min
        await workers[seq % 2].run_once(block_ms=1)

    with SessionLocal() as db:
        kinds = set(db.scalars(select(Alert.alert_type)).all())
    assert "memory_leak_pattern" in kinds


@pytest.mark.asyncio
async def test_retire_leaves_group_only_when_nothing_is_owed(redis_client):
    async def consumers():
        return {c["name"] for c in await redis_client.xinfo_consumers(config.TELEMETRY_STREAM,
                                                                      config.PERSIST_GROUP)}
    await send(redis_client, sample(seq=1))
    owing = PersistConsumer(redis_client, consumer_name="owing")
    await owing.ensure_group()
    await redis_client.xreadgroup(config.PERSIST_GROUP, "owing", {config.TELEMETRY_STREAM: ">"}, count=1)
    assert not await owing.retire() and "owing" in await consumers()

    clean = PersistConsumer(redis_client, consumer_name="clean")
    await clean.run_once(block_ms=1)
    assert await clean.retire() and "clean" not in await consumers()


@pytest.mark.asyncio
async def test_janitor_reaps_only_idle_consumers_that_owe_nothing(redis_client):
    await send(redis_client, sample(seq=1))
    owing = PersistConsumer(redis_client, consumer_name="owing")
    await owing.ensure_group()
    await redis_client.xreadgroup(config.PERSIST_GROUP, "owing", {config.TELEMETRY_STREAM: ">"}, count=1)
    await redis_client.xreadgroup(config.PERSIST_GROUP, "empty", {config.TELEMETRY_STREAM: ">"}, count=1)

    janitor = PersistConsumer(redis_client, consumer_name="janitor")
    assert await janitor.reap_idle_consumers(max_idle_ms=0) == 1
    names = {c["name"] for c in await redis_client.xinfo_consumers(config.TELEMETRY_STREAM, config.PERSIST_GROUP)}
    assert "owing" in names and "empty" not in names
