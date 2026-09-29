"""Control plane across API replicas: agent presence and routed commands.

With several API replicas behind a load balancer, an agent's WebSocket lives on exactly one
of them, and a command request can land on any of them. So:
  presence:{agent}               -> replica id holding the agent's socket (TTL, refreshed by traffic)
  replica:{replica}:commands     pub/sub channel each replica listens on for commands to deliver
  cmdack:{command_id}            list the delivering replica pushes one ack onto (BLPOP-able)
  cmd:{command_id}               hash with the command's lifecycle, for GET /api/v1/commands/{id}
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional

import redis.asyncio as aioredis
from redis.exceptions import WatchError

from app import config

log = logging.getLogger(__name__)

COMMAND_TTL_S = 3600


def presence_key(agent_id: str) -> str:
    return f"presence:{agent_id}"


def command_channel(replica: str) -> str:
    return f"replica:{replica}:commands"


class Presence:
    def __init__(self, redis: aioredis.Redis, replica: str = config.REPLICA_ID):
        self.redis = redis
        self.replica = replica

    async def claim(self, agent_id: str) -> None:
        """This replica now holds the agent (a reconnect may move it from another replica)."""
        await self.redis.set(presence_key(agent_id), self.replica, ex=config.PRESENCE_TTL_S)

    async def refresh(self, agent_id: str) -> None:
        await self.redis.set(presence_key(agent_id), self.replica, ex=config.PRESENCE_TTL_S)

    async def release(self, agent_id: str) -> bool:
        """Drop presence only if it still points here: if the agent already reconnected via
        another replica, that newer claim must survive this socket closing."""
        key = presence_key(agent_id)
        async with self.redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                if await pipe.get(key) != self.replica:
                    await pipe.reset()
                    return False
                pipe.multi()
                pipe.delete(key)
                await pipe.execute()
                return True
            except WatchError:          # changed under us: someone else claimed it
                return False

    async def where(self, agent_id: str) -> Optional[str]:
        return await self.redis.get(presence_key(agent_id))


class CommandBus:
    """Request/response over Redis: route a command to the replica holding the agent and
    wait (briefly) for that replica to confirm it wrote the command to the socket."""

    def __init__(self, redis: aioredis.Redis, replica: str = config.REPLICA_ID):
        self.redis = redis
        self.replica = replica

    async def send(self, agent_id: str, command: Dict[str, Any], ack_timeout_s: float = 3.0) -> Dict[str, Any]:
        command_id = uuid.uuid4().hex
        now = time.time()
        command = {**command, "command_id": command_id, "issued_at": now}
        record = {"command_id": command_id, "agent_id": agent_id, "type": command.get("type", ""),
                  "status": "pending", "issued_at": now, "issued_via": self.replica}

        target = await Presence(self.redis).where(agent_id)
        if target is None:
            return {**record, "status": "not_connected"}
        record["routed_to"] = target
        await self.redis.hset(f"cmd:{command_id}", mapping={k: str(v) for k, v in record.items()})
        await self.redis.expire(f"cmd:{command_id}", COMMAND_TTL_S)

        receivers = await self.redis.publish(command_channel(target),
                                             json.dumps({"agent_id": agent_id, "command": command}))
        if receivers == 0:  # presence is stale: the replica is gone and its TTL hasn't run out yet
            await self._set_status(command_id, "undeliverable")
            return {**record, "status": "undeliverable"}

        ack = await self.redis.blpop([f"cmdack:{command_id}"], timeout=ack_timeout_s)
        if ack is None:
            status = "unacknowledged"
        else:
            status = "delivered" if json.loads(ack[1]).get("delivered") else "undeliverable"
        await self._set_status(command_id, status)
        return {**record, "status": status}

    async def record_result(self, agent_id: str, message: Dict[str, Any]) -> None:
        """Agent reported the outcome of a command it received."""
        command_id = message.get("command_id")
        if not command_id:
            return
        key = f"cmd:{command_id}"
        if await self.redis.hget(key, "agent_id") != agent_id:
            return  # unknown command, or an agent reporting on someone else's: ignore
        await self.redis.hset(key, mapping={"status": "completed", "completed_at": str(time.time()),
                                            "result": json.dumps(message)[:10_000]})

    async def get(self, command_id: str) -> Optional[Dict[str, str]]:
        data = await self.redis.hgetall(f"cmd:{command_id}")
        return data or None

    async def _set_status(self, command_id: str, status: str) -> None:
        await self.redis.hset(f"cmd:{command_id}", "status", status)

    async def listen(self, deliver: Callable[[str, Dict[str, Any]], Awaitable[bool]]) -> None:
        """Run on every replica: deliver commands routed to this replica, then ack."""
        while True:
            try:
                async with self.redis.pubsub(ignore_subscribe_messages=True) as pubsub:
                    await pubsub.subscribe(command_channel(self.replica))
                    async for message in pubsub.listen():
                        if message and message.get("type") == "message":
                            await self._deliver_one(json.loads(message["data"]), deliver)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("command listener lost Redis, resubscribing in 1s: %s", e)
                await asyncio.sleep(1)

    async def _deliver_one(self, envelope: Dict[str, Any], deliver) -> None:
        command = envelope["command"]
        delivered = await deliver(envelope["agent_id"], command)
        ack_key = f"cmdack:{command['command_id']}"
        pipe = self.redis.pipeline()
        pipe.rpush(ack_key, json.dumps({"delivered": delivered, "replica": self.replica}))
        pipe.expire(ack_key, 60)
        await pipe.execute()
