"""Fan-out to connected dashboards.

The broadcast loop builds one frame per tick and hands it to every client. Handing over
never blocks: each client owns a one-slot mailbox, and a client that cannot keep up loses
stale frames rather than backing pressure up into the loop that serves everyone else.
"""

from __future__ import annotations

import asyncio
import time

import orjson
from fastapi import WebSocket

from backend.state import FleetState
from shared.contract import meta


class Client:
    """One connected dashboard.

    The queue is the whole backpressure design. Python's WebSocket API has no equivalent
    of the browser's `bufferedAmount`, so we cannot ask the socket how far behind it is.
    Instead we give each client a mailbox of exactly one frame: if a new frame arrives
    while the previous one is still unsent, the unsent one is thrown away. A slow client
    therefore sees a lower frame rate, and nobody else sees anything at all.

    Dropping the older frame is safe precisely because frames are snapshots of *now*: a
    frame that has not been sent yet is already out of date, and the one replacing it
    contains everything it would have said.
    """

    def __init__(self, ws: WebSocket, queue_size: int = 1) -> None:
        self.ws = ws
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=max(1, queue_size))
        self.dropped = 0
        self.sent = 0
        self.wants_snapshot = False
        self.connected_at = time.monotonic()

    def offer(self, frame: bytes) -> None:
        """Give this client a frame. Never blocks, never awaits, never raises.

        Called from the broadcast loop once per client per tick, so it must stay cheap
        and must not be able to hold the loop up.
        """
        try:
            self.queue.put_nowait(frame)
            return
        except asyncio.QueueFull:
            pass

        # Mailbox full: discard what is waiting and leave the newer frame instead.
        try:
            self.queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            pass
        self.dropped += 1

    async def pump(self) -> None:
        """Drain the mailbox into the socket.

        Runs as its own task per client, so a slow socket stalls only this coroutine.
        Cancelled when the client disconnects.
        """
        while True:
            frame = await self.queue.get()
            await self.ws.send_bytes(frame)
            self.sent += 1

    def stats(self) -> dict:
        return {
            "sent": self.sent,
            "dropped": self.dropped,
            "queued": self.queue.qsize(),
            "connected_seconds": round(time.monotonic() - self.connected_at, 1),
        }


class Hub:
    """Owns the broadcast clock and the set of connected dashboards.

    One frame is built per tick and handed to every client, so serialising costs
    O(fleet), not O(fleet x clients). This is the half of the design that decouples
    ingest rate from render rate: robots may publish at any rate they like, and
    dashboards still receive exactly `broadcast_hz` frames per second.
    """

    def __init__(self, state: FleetState, settings) -> None:
        self.state = state
        self.settings = settings
        self.clients: set[Client] = set()

        self.seq = 0
        self.ticks = 0
        self.frames_built = 0
        self.overruns = 0
        self.bytes_sent = 0
        self.last_frame_bytes = 0
        self.last_build_seconds = 0.0

    # -------------------------------------------------------------------- clients ----

    def add(self, client: Client) -> None:
        self.clients.add(client)

    def remove(self, client: Client) -> None:
        self.clients.discard(client)

    def hello(self) -> bytes:
        """The first message a client gets: how to read the stream, plus the site.

        Sending the geometry and status vocabulary here means the dashboard never
        hardcodes them, so changing the contract cannot leave the two sides disagreeing.
        """
        return orjson.dumps({
            "type": "hello",
            "seq": self.seq,
            "broadcast_hz": self.settings.broadcast_hz,
            "meta": meta(),
        })

    # ------------------------------------------------------------------ broadcast ----

    def _encode(
        self, kind: str, robots: list, summary: dict, now_ms: int,
        gone: list[str] | None = None,
    ) -> bytes:
        frame = {
            "type": kind,
            "seq": self.seq,
            "ts": now_ms,
            "robots": robots,
            "summary": summary,
        }
        # Only present when something was actually decommissioned, which is rare. No
        # reason to put an empty list on the wire five times a second.
        if gone:
            frame["gone"] = gone
        return orjson.dumps(frame)

    def snapshot_frame(self) -> bytes:
        """Every robot, for a client that has just connected or asked to resync."""
        return self._encode(
            "snapshot", self.state.snapshot(), self.state.summary(),
            int(time.time() * 1000),
        )

    def tick(self) -> None:
        """Build one frame and hand it to every client."""
        self.ticks += 1
        self.seq += 1
        self.state.sweep_stale()

        if not self.clients:
            # Nobody is listening. Drain the change set anyway, or it would grow without
            # bound while the dashboard is closed and the first client back would get a
            # needlessly enormous delta.
            self.state.drain_dirty()
            self.state.drain_removed()
            return

        # A periodic full snapshot bounds how long a dropped frame can matter: a client
        # that lost a delta is wrong for at most this many ticks, with no action needed
        # from either side.
        periodic = (
            self.settings.snapshot_every_n_ticks > 0
            and self.ticks % self.settings.snapshot_every_n_ticks == 0
        )

        started = time.perf_counter()
        now_ms = int(time.time() * 1000)
        summary = self.state.summary()

        if periodic:
            frame = self._encode("snapshot", self.state.snapshot(), summary, now_ms)
            self.state.dirty.clear()
            # A snapshot is the whole truth, so it purges removed robots on the client by
            # simply not containing them. Drain the set so it does not carry over.
            self.state.drain_removed()
            resync = frame
        else:
            frame = self._encode(
                "delta", self.state.drain_dirty(), summary, now_ms,
                self.state.drain_removed(),
            )
            resync = None  # built lazily, only if a client actually asked for one

        for client in self.clients:
            if client.wants_snapshot and not periodic:
                if resync is None:
                    resync = self._encode(
                        "snapshot", self.state.snapshot(), summary, now_ms
                    )
                client.offer(resync)
            else:
                client.offer(frame)
            client.wants_snapshot = False

        self.last_build_seconds = time.perf_counter() - started
        self.last_frame_bytes = len(frame)
        self.frames_built += 1
        self.bytes_sent += len(frame) * len(self.clients)

    async def run(self) -> None:
        """Broadcast forever on a fixed clock.

        The deadline advances by exactly one interval per tick rather than sleeping for
        an interval, so frame build time does not accumulate into drift. When we do fall
        behind, we resynchronise to now instead of trying to catch up: catching up on
        live telemetry would mean sending a burst of frames that are already stale.
        """
        interval = self.settings.broadcast_interval
        deadline = time.perf_counter()
        while True:
            self.tick()
            deadline += interval
            delay = deadline - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                self.overruns += 1
                deadline = time.perf_counter()

    def stats(self) -> dict:
        return {
            "clients": len(self.clients),
            "broadcast_hz": self.settings.broadcast_hz,
            "seq": self.seq,
            "ticks": self.ticks,
            "frames_built": self.frames_built,
            "overruns": self.overruns,
            "last_frame_bytes": self.last_frame_bytes,
            "last_build_ms": round(self.last_build_seconds * 1000, 2),
            "bytes_sent": self.bytes_sent,
            "dropped_total": sum(c.dropped for c in self.clients),
        }
