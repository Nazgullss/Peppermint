"""The authoritative fleet state: what every robot is doing right now.

This is the only place the live fleet lives. Ingest writes into it, broadcast reads out
of it, and nothing in between touches a disk or a socket -- `apply` is a validate and a
dict assignment, which is what lets ingest keep up as the fleet grows.
"""

from __future__ import annotations

import time

from shared.contract import (
    ATTENTION_CODES,
    BATTERY_LOW,
    ROBOT_TYPES,
    SITE_HEIGHT,
    SITE_WIDTH,
    STATUS_CODE,
    STATUSES,
    WORKING_CODES,
    PackedRobot,
    pack,
)


class FleetState:
    def __init__(self, stale_after: float = 10.0) -> None:
        self.stale_after = stale_after
        self.robots: dict[str, dict] = {}

        # Robots whose state changed since the last broadcast. The broadcast loop drains
        # this instead of re-sending the whole fleet, so a parked robot costs nothing.
        self.dirty: set[str] = set()

        # Robots explicitly decommissioned since the last broadcast, so the hub can tell
        # clients to drop them instead of waiting for the next full snapshot.
        self.removed: set[str] = set()

        self.ingested = 0
        self.rejected_out_of_order = 0
        self.rejected_malformed = 0
        self.started_at = time.monotonic()

    # ------------------------------------------------------------------ hot path ----

    def apply(self, msg: dict, now: float | None = None) -> bool:
        """Record one telemetry message. Returns True if it changed our state.

        Never awaits and never touches I/O: this runs once per robot per interval, so
        anything expensive here multiplies by the whole fleet.
        """
        now = time.time() if now is None else now

        robot_id = msg.get("robot_id")
        if not isinstance(robot_id, str) or not robot_id:
            self.rejected_malformed += 1
            return False

        status = STATUS_CODE.get(msg.get("status"))
        if status is None:
            self.rejected_malformed += 1
            return False

        try:
            x = float(msg["x"])
            y = float(msg["y"])
            battery = float(msg["battery"])
            ts = int(msg["ts"])
        except (KeyError, TypeError, ValueError):
            self.rejected_malformed += 1
            return False

        # Late delivery: a sample that is older than what we already hold is dropped
        # rather than applied. Without this, one message overtaking another on a
        # reconnect would visibly rewind a robot on the map.
        previous = self.robots.get(robot_id)
        if previous is not None and ts < previous["ts"]:
            self.rejected_out_of_order += 1
            return False

        type_name = msg.get("robot_type")
        type_code = ROBOT_TYPES.index(type_name) if type_name in ROBOT_TYPES else 0

        self.robots[robot_id] = {
            "x": min(max(x, 0.0), SITE_WIDTH),
            "y": min(max(y, 0.0), SITE_HEIGHT),
            "battery": min(max(battery, 0.0), 100.0),
            "status": status,
            "type": type_code,
            "ts": ts,
            "seen": now,
            "stale": False,
        }
        self.dirty.add(robot_id)
        self.ingested += 1
        return True

    # -------------------------------------------------------------------- reading ----

    def _packed(self, robot_id: str) -> PackedRobot:
        r = self.robots[robot_id]
        return pack(robot_id, r["x"], r["y"], r["battery"], r["status"], r["type"], r["stale"])

    def snapshot(self) -> list[PackedRobot]:
        """Every robot. Sent to a client on connect and on resync."""
        return [self._packed(rid) for rid in self.robots]

    def drain_dirty(self) -> list[PackedRobot]:
        """Robots that changed since the last call, and reset the change set."""
        if not self.dirty:
            return []
        packed = [self._packed(rid) for rid in self.dirty if rid in self.robots]
        self.dirty.clear()
        return packed

    def remove(self, robot_id: str) -> bool:
        """Drop a robot that was explicitly decommissioned.

        Only ever called for a lifecycle message. A robot that merely went quiet is never
        removed here -- that is what `stale` is for. Conflating the two would mean a robot
        that died mid-task quietly vanishing from the operator's screen, which is the one
        thing this dashboard exists to prevent.
        """
        if self.robots.pop(robot_id, None) is None:
            return False
        self.dirty.discard(robot_id)
        self.removed.add(robot_id)
        return True

    def drain_removed(self) -> list[str]:
        """Ids decommissioned since the last call, and reset the set."""
        if not self.removed:
            return []
        gone = list(self.removed)
        self.removed.clear()
        return gone

    # ------------------------------------------------------------------- liveness ----

    def sweep_stale(self, now: float | None = None) -> int:
        """Flag robots we have stopped hearing from, and unflag any that came back.

        A robot that dies mid-task stops publishing without ever sending a final status,
        so nothing in the telemetry stream announces it -- the only evidence is silence.
        Marking counts as a change, so the flag reaches the dashboard on the next frame
        like any other update.
        """
        now = time.time() if now is None else now
        cutoff = now - self.stale_after
        changed = 0
        for robot_id, r in self.robots.items():
            stale = r["seen"] < cutoff
            if stale != r["stale"]:
                r["stale"] = stale
                self.dirty.add(robot_id)
                changed += 1
        return changed

    # -------------------------------------------------------------------- summary ----

    def summary(self) -> dict:
        """Fleet-wide counts, recomputed from scratch on every broadcast.

        This is O(fleet size) per frame, which at 2000 robots and 5 Hz costs well under a
        millisecond. Maintaining incremental counters would be O(1) but could drift out
        of sync with the robots dict on any edge case, and we would never notice. The
        cheap correct version wins until measurement says otherwise.
        """
        by_status = dict.fromkeys(STATUSES, 0)
        total = len(self.robots)
        working = attention = stale = low_battery = 0
        battery_sum = 0.0

        for r in self.robots.values():
            by_status[STATUSES[r["status"]]] += 1
            battery_sum += r["battery"]
            if r["battery"] < BATTERY_LOW:
                low_battery += 1
            # Silence outranks whatever the robot last claimed to be doing.
            if r["stale"]:
                stale += 1
                attention += 1
            elif r["status"] in ATTENTION_CODES:
                attention += 1
            elif r["status"] in WORKING_CODES:
                working += 1

        return {
            "total": total,
            "by_status": by_status,
            "working": working,
            "attention": attention,
            "stale": stale,
            "low_battery": low_battery,
            "avg_battery": round(battery_sum / total, 1) if total else 0.0,
            "working_pct": round(100.0 * working / total, 1) if total else 0.0,
        }

    def get(self, robot_id: str, now: float | None = None) -> dict | None:
        """One robot in full, for the detail panel.

        Named fields rather than the packed tuple: this is read once when an operator
        clicks a robot, not thousands of times per frame, so clarity beats bytes.

        `now` is injected rather than read from the clock inside, so that tests can drive
        `age_seconds` with a fake clock the same way they drive staleness.
        """
        r = self.robots.get(robot_id)
        if r is None:
            return None
        return {
            "robot_id": robot_id,
            "x": r["x"],
            "y": r["y"],
            "battery": r["battery"],
            "status": STATUSES[r["status"]],
            "robot_type": ROBOT_TYPES[r["type"]],
            "stale": r["stale"],
            "ts": r["ts"],
            "age_seconds": round((time.time() if now is None else now) - r["seen"], 1),
            "needs_attention": r["stale"] or r["status"] in ATTENTION_CODES,
        }

    def stats(self) -> dict:
        return {
            "fleet_size": len(self.robots),
            "ingested": self.ingested,
            "rejected_out_of_order": self.rejected_out_of_order,
            "rejected_malformed": self.rejected_malformed,
            "dirty_pending": len(self.dirty),
            "uptime_seconds": round(time.monotonic() - self.started_at, 1),
        }
