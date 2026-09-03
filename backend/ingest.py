"""MQTT ingest: robots in, fleet state updated.

This is the hot end of the pipeline. Everything it does per message is parse, validate
and assign -- no disk, no network, no lock. The broadcast loop reads the state it
maintains on its own clock, so a burst of telemetry lands in a dict rather than backing
up behind a slow dashboard.
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiomqtt
import orjson

from backend.state import FleetState

log = logging.getLogger("ingest")


class Ingest:
    def __init__(self, state: FleetState, settings) -> None:
        self.state = state
        self.settings = settings

        self.connected = False
        self.messages = 0
        self.decode_errors = 0
        self.reconnects = 0
        self.decommissioned = 0

        # Last config the simulator reported about itself, surfaced on /api/stats so the
        # operator can see what the fleet is actually doing, not what we asked for.
        self.sim_status: dict = {}

        self._client: aiomqtt.Client | None = None
        self._rate_mark = (time.monotonic(), 0)
        self._rate = 0.0

    # ------------------------------------------------------------------ hot path ----

    def _handle(self, topic: str, payload: bytes, now: float) -> None:
        """Apply one broker message. Called once per robot per interval."""
        try:
            data = orjson.loads(payload)
        except orjson.JSONDecodeError:
            self.decode_errors += 1
            return

        if not isinstance(data, dict):
            self.decode_errors += 1
            return

        if topic == self.settings.sim_status_topic:
            self.sim_status = data
            return

        # An explicit goodbye. This is the only thing that removes a robot: silence never
        # does, because silence is what a robot dying mid-task looks like and that has to
        # stay on the operator's screen.
        if topic.endswith("/lifecycle"):
            robot_id = data.get("robot_id")
            if isinstance(robot_id, str) and data.get("event") == "decommissioned":
                if self.state.remove(robot_id):
                    self.decommissioned += 1
            return

        # A batch message carries many robots; a plain one carries a single robot. Both
        # shapes are accepted so the simulator's batch knob needs no backend change.
        batch = data.get("robots")
        if isinstance(batch, list):
            for robot in batch:
                if isinstance(robot, dict):
                    self.state.apply(robot, now)
                    self.messages += 1
            return

        self.state.apply(data, now)
        self.messages += 1

    # ------------------------------------------------------------------ main loop ----

    async def run(self) -> None:
        """Consume forever, reconnecting on failure.

        Losing the broker is treated as an expected event rather than a crash: we back
        off, reconnect and carry on from the state we already hold. Robots that kept
        publishing while we were away simply resume; robots that did not go stale on
        their own, which is exactly what the operator should see.
        """
        backoff = 1.0
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self.settings.mqtt_host,
                    port=self.settings.mqtt_port,
                    username=self.settings.mqtt_username,
                    password=self.settings.mqtt_password,
                ) as client:
                    self._client = client
                    self.connected = True
                    backoff = 1.0
                    log.info("ingest connected to %s:%d",
                             self.settings.mqtt_host, self.settings.mqtt_port)

                    # QoS 0 for telemetry: a lost sample is replaced by the next one in
                    # well under a second, and acknowledging every sample would cost more
                    # than the sample is worth. The simulator's own status and lifecycle
                    # messages are QoS 1, because missing one would leave the UI lying.
                    await client.subscribe(self.settings.telemetry_topic, qos=0)
                    await client.subscribe(self.settings.batch_topic, qos=0)
                    await client.subscribe(self.settings.sim_status_topic, qos=1)
                    await client.subscribe(self.settings.lifecycle_topic, qos=1)

                    async for message in client.messages:
                        self._handle(str(message.topic), message.payload, time.time())

            except aiomqtt.MqttError as exc:
                self.reconnects += 1
                log.warning("broker connection lost (%s); retrying in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                self._client = None
                self.connected = False

    # -------------------------------------------------------------------- control ----

    async def publish_control(self, payload: dict) -> bool:
        """Forward a live config change to the simulator.

        The admin endpoint authenticates the operator and then calls this, so the broker
        connection stays the only path to the fleet and there is exactly one place where
        access is checked. QoS 1: a dropped control message would silently do nothing.
        """
        client = self._client
        if client is None:
            return False
        await client.publish(
            self.settings.control_topic, orjson.dumps(payload), qos=1
        )
        return True

    # ---------------------------------------------------------------------- stats ----

    def rate(self) -> float:
        """Ingest rate in messages per second, sampled at most once a second."""
        now = time.monotonic()
        mark_time, mark_count = self._rate_mark
        elapsed = now - mark_time
        if elapsed >= 1.0:
            self._rate = (self.messages - mark_count) / elapsed
            self._rate_mark = (now, self.messages)
        return round(self._rate, 1)

    def stats(self) -> dict:
        return {
            "connected": self.connected,
            "messages": self.messages,
            "messages_per_second": self.rate(),
            "decode_errors": self.decode_errors,
            "reconnects": self.reconnects,
            "decommissioned": self.decommissioned,
            "simulator": self.sim_status,
        }
