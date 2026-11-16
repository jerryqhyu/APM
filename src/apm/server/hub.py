"""Fan-out of live events to every connected WebSocket."""

import asyncio
import contextlib
from typing import Any


class Hub:
    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[dict[str, Any]]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=10_000)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._queues.discard(queue)

    def publish(self, event: dict[str, Any]) -> None:
        """Safe to call from any thread."""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._deliver, event)

    def _deliver(self, event: dict[str, Any]) -> None:
        for queue in self._queues:
            # A client too slow to keep up misses events rather than stalling the others; it
            # refetches on the next one it does get.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)
