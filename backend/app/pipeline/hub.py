"""Per-API-process pieces: dashboard fan-out and ingest admission control."""
from __future__ import annotations

import asyncio
import logging
from typing import Set

import redis.asyncio as aioredis
from fastapi import WebSocket

from app import config
from app.pipeline.streams import group_backlog

log = logging.getLogger(__name__)

SEND_TIMEOUT_S = 1.0


class DashboardHub:
    """Relays the Redis `dashboard` channel to this process's dashboard sockets.

    Every API replica subscribes, so an event produced by any worker reaches every dashboard
    no matter which replica it is connected to. A dashboard that can't take a message within
    SEND_TIMEOUT_S is dropped rather than allowed to stall everyone else's updates."""

    def __init__(self, redis: aioredis.Redis):
        self.redis = redis
        self.sockets: Set[WebSocket] = set()

    def add(self, ws: WebSocket) -> None:
        self.sockets.add(ws)

    def discard(self, ws: WebSocket) -> None:
        self.sockets.discard(ws)

    async def run(self) -> None:
        while True:
            try:
                async with self.redis.pubsub(ignore_subscribe_messages=True) as pubsub:
                    await pubsub.subscribe(config.DASHBOARD_CHANNEL)
                    async for message in pubsub.listen():
                        if message and message.get("type") == "message":
                            await self.broadcast(message["data"])
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("dashboard relay lost Redis, resubscribing in 1s: %s", e)
                await asyncio.sleep(1)

    async def broadcast(self, text: str) -> None:
        if not self.sockets:
            return
        targets = list(self.sockets)
        results = await asyncio.gather(
            *(asyncio.wait_for(ws.send_text(text), SEND_TIMEOUT_S) for ws in targets),
            return_exceptions=True)
        for ws, result in zip(targets, results):
            if isinstance(result, BaseException):
                self.sockets.discard(ws)


class Admission:
    """Edge admission control. Tracks how far the persist group is behind; above
    PERSIST_MAX_LAG new telemetry is refused with an explicit throttle, so overload is
    visible to agents and operators instead of turning into silent loss or lockout."""

    def __init__(self, redis: aioredis.Redis, poll_s: float = 0.5):
        self.redis = redis
        self.poll_s = poll_s
        self.persist_lag = 0
        self.accepted = 0
        self.rejected = 0

    @property
    def overloaded(self) -> bool:
        return self.persist_lag > config.PERSIST_MAX_LAG

    async def refresh(self) -> None:
        self.persist_lag = await group_backlog(self.redis, config.PERSIST_GROUP)

    async def run(self) -> None:
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("admission poll failed: %s", e)
            await asyncio.sleep(self.poll_s)
