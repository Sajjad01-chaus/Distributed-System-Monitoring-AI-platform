"""Worker entrypoint: `python -m app.worker persist|detect`.

Run as many of each as needed; members of a consumer group share the stream's entries.
SIGTERM finishes the in-flight batch before exiting; anything unacked is re-claimed by a
surviving worker after CLAIM_IDLE_MS.
"""
import asyncio
import logging
import signal
import sys

from app.pipeline import streams
from app.pipeline.detect import DetectConsumer
from app.pipeline.persist import PersistConsumer

CONSUMERS = {"persist": PersistConsumer, "detect": DetectConsumer}


async def main(kind: str) -> None:
    redis = streams.redis_factory()
    consumer = CONSUMERS[kind](redis)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda: setattr(consumer, "stopping", True))
        except NotImplementedError:  # Windows event loop
            pass
    try:
        await consumer.run()
    finally:
        await redis.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if len(sys.argv) != 2 or sys.argv[1] not in CONSUMERS:
        sys.exit(f"usage: python -m app.worker {{{'|'.join(CONSUMERS)}}}")
    asyncio.run(main(sys.argv[1]))
