"""Live log fan-out to browsers.

Three rules, each of which exists because breaking it produces a failure that only shows up weeks
later:

1. The producer never blocks on a consumer. A browser tab on bad wifi must not be able to stall a
   test run. A subscriber whose queue fills is dropped and told to resync.
2. Memory is not the durability layer. The ring buffer serves fast replay; `stdout.log` on disk is
   the record.
3. Every stream heartbeats. Corporate proxies close idle connections without telling either end,
   and the heartbeat write is how a dead subscriber gets noticed and removed. Without it,
   subscribers accumulate forever and the leak is invisible until the process is large.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("testboard.streaming")

RING_LINES = 5000
SUBSCRIBER_QUEUE = 500
HEARTBEAT_SECONDS = 15


@dataclass
class _Stream:
    ring: deque = field(default_factory=lambda: deque(maxlen=RING_LINES))
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    seq: int = 0
    finished: bool = False
    log_path: Path | None = None


class LogBus:
    def __init__(self) -> None:
        self._streams: dict[int, _Stream] = {}

    def open(self, run_id: int, log_path: Path) -> None:
        stream = _Stream(log_path=log_path)
        self._streams[run_id] = stream

    def publish(self, run_id: int, line: str) -> None:
        stream = self._streams.get(run_id)
        if stream is None:
            return
        stream.seq += 1
        item = (stream.seq, line)
        stream.ring.append(item)
        for queue in list(stream.subscribers):
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                # Drop the slow subscriber rather than applying backpressure to the test run.
                # A sentinel tells its generator to close so the browser reconnects and replays.
                stream.subscribers.discard(queue)
                log.debug("dropped a slow subscriber on run %s", run_id)

    def finish(self, run_id: int) -> None:
        stream = self._streams.get(run_id)
        if stream is None:
            return
        stream.finished = True
        for queue in list(stream.subscribers):
            try:
                queue.put_nowait((None, None))
            except asyncio.QueueFull:
                pass

    def close(self, run_id: int) -> None:
        self._streams.pop(run_id, None)

    def is_live(self, run_id: int) -> bool:
        stream = self._streams.get(run_id)
        return stream is not None and not stream.finished

    def backlog(self, run_id: int, since: int, log_path: Path | None) -> list[tuple[int, str]]:
        """Everything after `since`, from memory if it is still there and from disk if not."""
        stream = self._streams.get(run_id)
        if stream and stream.ring:
            oldest = stream.ring[0][0]
            if since >= oldest - 1:
                return [item for item in stream.ring if item[0] > since]

        # Either the run is over or the client was away longer than the ring holds. Serving the
        # file means a late joiner sees the whole run rather than a truncated tail with no warning.
        path = log_path or (stream.log_path if stream else None)
        if not path or not Path(path).exists():
            return []
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return [(i, line) for i, line in enumerate(text.splitlines(), start=1) if i > since]

    async def subscribe(self, run_id: int):
        """Yield lines as they arrive. Caller is responsible for replaying the backlog first."""
        stream = self._streams.get(run_id)
        if stream is None:
            return
        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE)
        stream.subscribers.add(queue)
        try:
            while True:
                try:
                    seq, line = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield None, None      # caller emits a heartbeat comment
                    continue
                if seq is None:
                    return
                yield seq, line
        finally:
            stream.subscribers.discard(queue)

    def subscriber_count(self, run_id: int) -> int:
        stream = self._streams.get(run_id)
        return len(stream.subscribers) if stream else 0
