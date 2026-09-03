"""The simulated fleet's publisher.

One process pretends to be the whole fleet: it advances the physics, then publishes each
robot's telemetry to the broker on its own topic, exactly as a real robot would. Nothing
downstream can tell the difference -- the backend subscribes to `fleet/+/telemetry` and
neither knows nor cares that the publishers share a process.

Run with:  python -m simulator.main
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import aiomqtt
import orjson

from shared.contract import ROBOT_TYPES, STATUSES
from shared.eventloop import use_selector_loop_on_windows
from simulator.config import settings
from simulator.fleet import Fleet

log = logging.getLogger("simulator")


class Simulator:
    def __init__(self) -> None:
        self.fleet = Fleet(settings.fleet_size, seed=settings.seed)
        self.interval = settings.interval_seconds
        self.padding = settings.payload_padding_bytes
        self.batch_size = max(1, settings.publish_batch_size)
        self.started_at = time.monotonic()

        # Robots removed by a fleet-size change, waiting to be announced on the next tick.
        self.decommissioned: list[str] = []

        # Counters, surfaced on the sim status topic and used for the load-test numbers.
        self.published_messages = 0
        self.published_ticks = 0
        self.overruns = 0
        self.last_publish_seconds = 0.0
        self.connected = False

    # ------------------------------------------------------------------ live knobs ----

    def apply_control(self, payload: dict) -> dict:
        """Apply a runtime config change. Returns the config as it now stands.

        Only the knobs the challenge asks to be adjustable are accepted; anything else in
        the message is ignored rather than trusted."""
        changed = {}
        if (n := payload.get("fleet_size")) is not None:
            n = max(0, min(int(n), 20_000))  # hard ceiling so a typo cannot OOM the box
            self.decommissioned.extend(self.fleet.resize(n))
            changed["fleet_size"] = n
        if (ms := payload.get("update_interval_ms")) is not None:
            self.interval = max(20, min(int(ms), 60_000)) / 1000.0
            changed["update_interval_ms"] = int(self.interval * 1000)
        if (pad := payload.get("payload_padding_bytes")) is not None:
            self.padding = max(0, min(int(pad), 64_000))
            changed["payload_padding_bytes"] = self.padding
        if (batch := payload.get("publish_batch_size")) is not None:
            self.batch_size = max(1, min(int(batch), 1000))
            changed["publish_batch_size"] = self.batch_size
        if changed:
            log.info("control applied: %s", changed)
        return self.current_config()

    def current_config(self) -> dict:
        return {
            "fleet_size": self.fleet.n,
            "update_interval_ms": int(round(self.interval * 1000)),
            "payload_padding_bytes": self.padding,
            "publish_batch_size": self.batch_size,
        }

    def stats(self) -> dict:
        return {
            **self.current_config(),
            "uptime_seconds": round(time.monotonic() - self.started_at, 1),
            "published_messages": self.published_messages,
            "published_ticks": self.published_ticks,
            "overruns": self.overruns,
            "last_publish_seconds": round(self.last_publish_seconds, 4),
            "connected": self.connected,
        }

    # ------------------------------------------------------------------- publishing ----

    def _encode(self, index: int, now_ms: int) -> dict:
        """One robot's telemetry, in the shape the data contract defines.

        Deviation from the recorded log, documented in README.md: the log's `t` counts
        seconds from the start of its recording window, which is a property of a
        recording rather than of a robot. A live stream needs an absolute clock -- so
        that ordering survives a simulator restart -- so we publish `ts` in unix
        milliseconds instead. Every other field matches the contract exactly.
        """
        fleet = self.fleet
        msg = {
            "ts": now_ms,
            "robot_id": fleet.ids[index],
            "x": round(float(fleet.x[index]), 1),
            "y": round(float(fleet.y[index]), 1),
            "status": STATUSES[int(fleet.status[index])],
            "battery": round(float(fleet.battery[index]), 1),
            "robot_type": ROBOT_TYPES[int(fleet.rtype[index])],
        }
        if self.padding:
            msg["pad"] = "x" * self.padding
        return msg

    async def _publish_tick(self, client: aiomqtt.Client) -> None:
        """Publish the whole fleet once.

        QoS 0 is deliberate for telemetry: a position sample that arrives late is worth
        less than the next one, and the retry bookkeeping of QoS 1 would cost more than
        the sample is worth. Control and lifecycle messages use QoS 1, where delivery
        does matter.
        """
        now_ms = int(time.time() * 1000)
        prefix = settings.topic_prefix
        started = time.perf_counter()

        # Encode the whole tick before publishing any of it.
        #
        # `await client.publish` yields to the event loop, and the control loop runs there
        # too: a fleet-size change arriving mid-publish resizes the arrays underneath us,
        # and the next iteration indexes past the end of a fleet that just shrank. Taking
        # the snapshot up front makes "one tick is one instant" literally true, which is
        # what it always claimed to be. Batch size is captured for the same reason.
        batch_size = self.batch_size
        payloads = [self._encode(i, now_ms) for i in range(self.fleet.n)]

        # Announce anything the last control message removed, before publishing telemetry
        # that no longer mentions it. QoS 1, because a lost decommission notice leaves a
        # robot on the operator's map that no longer exists.
        if self.decommissioned:
            leaving, self.decommissioned = self.decommissioned, []
            for robot_id in leaving:
                await client.publish(
                    f"{prefix}/{robot_id}/lifecycle",
                    orjson.dumps({"ts": now_ms, "robot_id": robot_id,
                                  "event": "decommissioned"}),
                    qos=1,
                )

        if batch_size == 1:
            for message in payloads:
                await client.publish(
                    f"{prefix}/{message['robot_id']}/telemetry",
                    orjson.dumps(message),
                    qos=0,
                )
                self.published_messages += 1
        else:
            for start in range(0, len(payloads), batch_size):
                await client.publish(
                    settings.batch_topic,
                    orjson.dumps({
                        "ts": now_ms,
                        "robots": payloads[start:start + batch_size],
                    }),
                    qos=0,
                )
                self.published_messages += 1

        self.last_publish_seconds = time.perf_counter() - started
        self.published_ticks += 1

    # ------------------------------------------------------------------- main loops ----

    async def _control_loop(self, client: aiomqtt.Client) -> None:
        """Listen for live knob changes and answer with the config as applied."""
        async for message in client.messages:
            try:
                payload = orjson.loads(message.payload)
            except orjson.JSONDecodeError:
                log.warning("ignoring malformed control message")
                continue
            applied = self.apply_control(payload)
            await client.publish(
                settings.sim_status_topic, orjson.dumps(self.stats()), qos=1, retain=True
            )
            log.info("config now %s", applied)

    async def _tick_loop(self, client: aiomqtt.Client) -> None:
        """Advance and publish on a fixed schedule.

        The deadline is advanced by exactly one interval per tick rather than sleeping for
        an interval, so publish time does not accumulate into drift. If a tick overruns
        its interval we count it and resynchronise instead of falling further behind --
        that counter is the simulator's own honest signal that it is saturated.
        """
        deadline = time.perf_counter()
        last = time.monotonic()
        status_countdown = 0

        while True:
            now = time.monotonic()
            dt = now - last
            last = now
            self.fleet.tick(now - self.started_at, dt)
            await self._publish_tick(client)

            status_countdown -= 1
            if status_countdown <= 0:
                await client.publish(
                    settings.sim_status_topic, orjson.dumps(self.stats()), qos=1, retain=True
                )
                status_countdown = max(1, int(2.0 / self.interval))

            deadline += self.interval
            remaining = deadline - time.perf_counter()
            if remaining > 0:
                await asyncio.sleep(remaining)
            else:
                self.overruns += 1
                if settings.warn_on_overrun and self.overruns % 20 == 1:
                    log.warning(
                        "tick overran by %.0f ms (fleet=%d, interval=%d ms, publish=%.0f ms)",
                        -remaining * 1000, self.fleet.n,
                        int(self.interval * 1000), self.last_publish_seconds * 1000,
                    )
                deadline = time.perf_counter()

    async def run(self) -> None:
        """Connect, publish, and reconnect forever.

        A robot's link to the broker is assumed to be flaky, so losing it is a normal
        event rather than a crash: we back off, reconnect, and carry on from the fleet
        state we already have. The physics keeps no history, so nothing is lost.
        """
        backoff = 1.0
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=settings.mqtt_host,
                    port=settings.mqtt_port,
                    username=settings.mqtt_username,
                    password=settings.mqtt_password,
                    keepalive=settings.mqtt_keepalive,
                ) as client:
                    self.connected = True
                    backoff = 1.0
                    log.info(
                        "connected to %s:%d, publishing %d robots every %d ms",
                        settings.mqtt_host, settings.mqtt_port,
                        self.fleet.n, int(self.interval * 1000),
                    )
                    await client.subscribe(settings.control_topic, qos=1)
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._tick_loop(client))
                        tg.create_task(self._control_loop(client))
            except* aiomqtt.MqttError as eg:
                self.connected = False
                log.warning("broker connection lost (%s); retrying in %.0fs",
                            eg.exceptions[0], backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)


async def amain() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    await Simulator().run()


if __name__ == "__main__":
    use_selector_loop_on_windows()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(amain())
