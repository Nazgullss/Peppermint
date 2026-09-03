"""The simulated fleet.

Every robot's state lives in a parallel numpy array and one tick advances the entire
fleet with a fixed number of vectorised operations, independent of fleet size. This is
the reason the simulator is not the bottleneck at a few thousand robots: a per-robot
Python loop costs far more at N=2000, and the cost that remains is MQTT publishing
rather than the physics.

Movement, battery and status transitions are all anchored to numbers measured from the
reference log (see shared/contract.py) so the output is plausible rather than merely
random.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from shared.contract import (
    ACTIVE,
    BATTERY_FULL,
    BATTERY_LOW,
    BATTERY_RATE,
    BLOCKED,
    CHARGING,
    DOCKS,
    IDLE,
    OBSTACLE_MARGIN,
    OBSTACLES,
    OFFLINE,
    ON_MISSION,
    ROBOT_TYPES,
    SITE_HEIGHT,
    SITE_WIDTH,
    SPEED_BY_STATUS,
    STATUSES,
)

_SEED_ROSTER = Path(__file__).resolve().parent.parent / "shared" / "robots.seed.json"

# --------------------------------------------------------------------------------------
# Status machine
#
# Row i is the distribution over next statuses for a robot currently in status i, sampled
# when its dwell timer expires. Rows are indexed by the status codes in shared.contract,
# so the order here must match STATUSES.
#
# The row weights, together with the dwell times below, are tuned so the steady state
# lands in the same neighbourhood as the reference log (idle-heavy, with a working
# minority and a small tail of degraded robots) rather than reproducing it exactly.
#
#            idle  actv  missn charg block error maint offln
_TRANSITIONS = np.array([
    [0.35, 0.22, 0.33, 0.00, 0.00, 0.01, 0.06, 0.03],  # idle       -> often stays parked
    [0.34, 0.24, 0.27, 0.00, 0.09, 0.03, 0.00, 0.03],  # active
    [0.30, 0.14, 0.36, 0.00, 0.12, 0.05, 0.00, 0.03],  # on_mission -> often continues
    [0.05, 0.00, 0.00, 0.95, 0.00, 0.00, 0.00, 0.00],  # charging   -> battery rule decides
    [0.15, 0.30, 0.40, 0.00, 0.10, 0.05, 0.00, 0.00],  # blocked    -> tries to resume
    [0.20, 0.00, 0.00, 0.00, 0.00, 0.30, 0.45, 0.05],  # error      -> usually maintenance
    [0.60, 0.10, 0.00, 0.00, 0.00, 0.05, 0.25, 0.00],  # maintenance
    [0.55, 0.05, 0.00, 0.00, 0.00, 0.10, 0.00, 0.30],  # offline
], dtype=np.float64)
_CUMULATIVE = np.cumsum(_TRANSITIONS, axis=1)

# How long a robot stays in a status before rolling again, in seconds (low, high).
# A status's share of the fleet is roughly its arrival rate times its mean dwell, so these
# matter as much as the weights above.
_DWELL = np.array([
    (10.0, 45.0),   # idle
    (10.0, 40.0),   # active
    (20.0, 70.0),   # on_mission
    (10.0, 20.0),   # charging
    (5.0, 25.0),    # blocked
    (20.0, 90.0),   # error
    (30.0, 120.0),  # maintenance
    (15.0, 60.0),   # offline
], dtype=np.float32)

_SPEED = np.array([SPEED_BY_STATUS[i] for i in range(len(STATUSES))], dtype=np.float32)
_BATTERY_RATE = np.array([BATTERY_RATE[i] for i in range(len(STATUSES))], dtype=np.float32)
_DOCKS = np.array(DOCKS, dtype=np.float32)

# Statuses in which a robot is stationary and unable to look after itself, so the
# low-battery rule must not drag it off to a dock.
_IMMOBILE = np.array([False, False, False, True, False, True, True, True])

_WAYPOINT_REACHED = 4.0  # units; closer than this counts as arrival
_DOCK_REACHED = 8.0


class Fleet:
    """Mutable fleet state. Not thread-safe: one owner, ticked from a single task."""

    def __init__(self, size: int, seed: int | None = None) -> None:
        self.rng = np.random.default_rng(seed)
        self._seed_positions = self._load_seed_roster()
        self.n = 0
        self.ids: list[str] = []
        # Allocated lazily by _grow so that __init__ and resize share one code path.
        self.x = np.zeros(0, dtype=np.float32)
        self.y = np.zeros(0, dtype=np.float32)
        self.tx = np.zeros(0, dtype=np.float32)
        self.ty = np.zeros(0, dtype=np.float32)
        self.battery = np.zeros(0, dtype=np.float32)
        self.status = np.zeros(0, dtype=np.int8)
        self.until = np.zeros(0, dtype=np.float32)
        self.rtype = np.zeros(0, dtype=np.int8)
        self.speed_factor = np.zeros(0, dtype=np.float32)
        self.resize(size)

    # ---------------------------------------------------------------- construction ----

    @staticmethod
    def _load_seed_roster() -> list[tuple[float, float, int]]:
        """Start the first robots from the roster shipped with the challenge, so a
        fleet of 8 lines up with the reference data. Beyond that we invent robots."""
        try:
            roster = json.loads(_SEED_ROSTER.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        return [
            (float(r["start"]["x"]), float(r["start"]["y"]),
             ROBOT_TYPES.index(r["robot_type"]) if r.get("robot_type") in ROBOT_TYPES else 0)
            for r in roster
        ]

    def resize(self, size: int) -> list[str]:
        """Grow or shrink the fleet in place. Existing robots keep their identity and
        state; this is what makes the live fleet-size knob non-disruptive.

        Returns the ids of any robots that were removed, so the caller can announce them.
        A consumer cannot otherwise tell a decommissioned robot from one that died
        mid-task -- both simply stop publishing -- and conflating the two would mean
        either ghosts accumulating on the map or real failures quietly disappearing.
        """
        size = max(0, int(size))
        if size > self.n:
            self._grow(size - self.n)
            return []
        if size < self.n:
            removed = self.ids[size:]
            self._shrink(size)
            return removed
        return []

    def _grow(self, count: int) -> None:
        start = self.n
        new_ids = [f"r{i + 1}" for i in range(start, start + count)]

        px = np.empty(count, dtype=np.float32)
        py = np.empty(count, dtype=np.float32)
        rt = np.empty(count, dtype=np.int8)
        for i in range(count):
            idx = start + i
            if idx < len(self._seed_positions):
                sx, sy, st = self._seed_positions[idx]
                px[i], py[i], rt[i] = sx, sy, st
            else:
                px[i], py[i] = 0.0, 0.0  # replaced below
                rt[i] = idx % len(ROBOT_TYPES)
        invented = np.arange(count) >= max(0, len(self._seed_positions) - start)
        if invented.any():
            fx, fy = self._random_free_points(int(invented.sum()))
            px[invented], py[invented] = fx, fy

        tx, ty = self._random_free_points(count)
        self.ids.extend(new_ids)
        self.x = np.concatenate([self.x, px])
        self.y = np.concatenate([self.y, py])
        self.tx = np.concatenate([self.tx, tx])
        self.ty = np.concatenate([self.ty, ty])
        self.battery = np.concatenate([
            self.battery, self.rng.uniform(25.0, 97.0, count).astype(np.float32)
        ])
        self.status = np.concatenate([self.status, np.full(count, IDLE, dtype=np.int8)])
        self.until = np.concatenate([self.until, np.zeros(count, dtype=np.float32)])
        self.rtype = np.concatenate([self.rtype, rt])
        self.speed_factor = np.concatenate([
            self.speed_factor, self.rng.uniform(0.85, 1.15, count).astype(np.float32)
        ])
        self.n += count

    def _shrink(self, size: int) -> None:
        self.ids = self.ids[:size]
        for name in ("x", "y", "tx", "ty", "battery", "status", "until", "rtype", "speed_factor"):
            setattr(self, name, getattr(self, name)[:size].copy())
        self.n = size

    # -------------------------------------------------------------------- geometry ----

    @staticmethod
    def _inside_obstacle(x: np.ndarray, y: np.ndarray, margin: float = OBSTACLE_MARGIN) -> np.ndarray:
        """Vectorised obstacle test over the whole fleet. Six rectangles means six
        masked comparisons regardless of how many robots there are."""
        hit = np.zeros(x.shape, dtype=bool)
        for x0, y0, x1, y1 in OBSTACLES:
            hit |= (x >= x0 - margin) & (x <= x1 + margin) & (y >= y0 - margin) & (y <= y1 + margin)
        return hit

    def _random_free_points(self, count: int) -> tuple[np.ndarray, np.ndarray]:
        """Uniform points on the site that are not inside a rack. Rejection sampling:
        roughly 84% of the site is free, so a handful of rounds clears any fleet size."""
        x = np.empty(count, dtype=np.float32)
        y = np.empty(count, dtype=np.float32)
        todo = np.arange(count)
        for _ in range(12):
            if todo.size == 0:
                break
            cx = self.rng.uniform(8.0, SITE_WIDTH - 8.0, todo.size).astype(np.float32)
            cy = self.rng.uniform(8.0, SITE_HEIGHT - 8.0, todo.size).astype(np.float32)
            ok = ~self._inside_obstacle(cx, cy, OBSTACLE_MARGIN + 2.0)
            x[todo[ok]] = cx[ok]
            y[todo[ok]] = cy[ok]
            todo = todo[~ok]
        if todo.size:  # pathological fallback: park them in the open bottom aisle
            x[todo] = self.rng.uniform(20.0, SITE_WIDTH - 20.0, todo.size)
            y[todo] = 500.0
        return x, y

    # ------------------------------------------------------------------------ tick ----

    def tick(self, now: float, dt: float) -> None:
        """Advance the whole fleet by `dt` seconds. `now` is seconds since simulator
        start and drives the status dwell timers."""
        if self.n == 0:
            return
        self._step_status(now)
        self._step_battery(dt)
        self._step_motion(dt)

    def _step_status(self, now: float) -> None:
        # Two groups are not free to pick their own next status, because the battery rules
        # own them: a flat robot, until someone recovers it, and a robot on a charger,
        # until it is full. Without the second exclusion the machine would occasionally
        # pull a robot off the dock at 6% and send it straight back out to die.
        due = (self.until <= now) & (self.battery > 0.5) & (self.status != CHARGING)
        if due.any():
            idx = np.flatnonzero(due)
            current = self.status[idx].astype(np.int64)
            roll = self.rng.random(idx.size)
            # First column whose cumulative probability exceeds the roll.
            nxt = (_CUMULATIVE[current] > roll[:, None]).argmax(axis=1).astype(np.int8)
            self.status[idx] = nxt
            low, high = _DWELL[nxt, 0], _DWELL[nxt, 1]
            self.until[idx] = now + self.rng.uniform(low, high).astype(np.float32)
            # A robot that just picked up work heads somewhere new.
            moving = (nxt == ACTIVE) | (nxt == ON_MISSION)
            if moving.any():
                mi = idx[moving]
                self.tx[mi], self.ty[mi] = self._random_free_points(mi.size)

        self._apply_battery_rules(now)

    def _apply_battery_rules(self, now: float) -> None:
        """Battery overrides the status machine: a robot low on charge abandons what it
        is doing and drives to the nearest dock, and a charged one rejoins the pool.
        This is what keeps batteries behaving like batteries over a long run rather than
        drifting monotonically to zero."""
        # Flat battery wins over everything: the robot stops where it is and drops
        # offline. It cannot drive itself to a dock, so it stays there until recovered.
        # A robot already on a charger is climbing back out of the flat band under its own
        # steam, so it is deliberately not counted as stranded -- otherwise the zeroing
        # below would wipe out its first fraction of a percent on every tick and pin it at
        # zero forever.
        stranded = (self.battery <= 0.5) & (self.status != CHARGING)
        if stranded.any():
            self.battery[stranded] = 0.0
            newly_dead = stranded & (self.status != OFFLINE)
            if newly_dead.any():
                idx = np.flatnonzero(newly_dead)
                self.status[idx] = OFFLINE
                self.until[idx] = now + self.rng.uniform(40.0, 100.0, idx.size).astype(np.float32)

            # Recovery: after that delay a technician reaches the robot and puts it on a
            # portable charger in place. Modelled rather than teleported, because a robot
            # jumping across the site would be exactly the implausibility we are avoiding.
            recovered = stranded & (self.status == OFFLINE) & (self.until <= now)
            if recovered.any():
                ri = np.flatnonzero(recovered)
                self.status[ri] = CHARGING
                self.until[ri] = now + 15.0

        low = (self.battery < BATTERY_LOW) & ~_IMMOBILE[self.status] & (self.battery > 0.5)
        if low.any():
            idx = np.flatnonzero(low)
            # Nearest dock by squared distance; a handful of docks, so a small broadcast.
            d2 = ((self.x[idx, None] - _DOCKS[None, :, 0]) ** 2
                  + (self.y[idx, None] - _DOCKS[None, :, 1]) ** 2)
            nearest = d2.argmin(axis=1)
            self.tx[idx] = _DOCKS[nearest, 0]
            self.ty[idx] = _DOCKS[nearest, 1]
            arrived = d2[np.arange(idx.size), nearest] <= _DOCK_REACHED ** 2
            self.status[idx[arrived]] = CHARGING
            self.status[idx[~arrived]] = ACTIVE
            self.until[idx] = now + 10.0

        # Charged up: undock and go back to work.
        done = (self.status == CHARGING) & (self.battery >= BATTERY_FULL)
        if done.any():
            idx = np.flatnonzero(done)
            self.status[idx] = IDLE
            self.until[idx] = now + self.rng.uniform(2.0, 8.0, idx.size).astype(np.float32)
            self.tx[idx], self.ty[idx] = self._random_free_points(idx.size)

    def _step_battery(self, dt: float) -> None:
        self.battery += _BATTERY_RATE[self.status] * dt
        np.clip(self.battery, 0.0, 100.0, out=self.battery)

    def _step_motion(self, dt: float) -> None:
        speed = _SPEED[self.status] * self.speed_factor
        moving = speed > 0.0
        if not moving.any():
            return
        idx = np.flatnonzero(moving)

        dx = self.tx[idx] - self.x[idx]
        dy = self.ty[idx] - self.y[idx]
        dist = np.hypot(dx, dy)

        # Arrived: pick a fresh waypoint and skip this step's movement.
        reached = dist < _WAYPOINT_REACHED
        if reached.any():
            ri = idx[reached]
            # Robots heading to a dock are handled by the battery rule, not re-targeted.
            retarget = ri[self.battery[ri] >= BATTERY_LOW]
            if retarget.size:
                self.tx[retarget], self.ty[retarget] = self._random_free_points(retarget.size)

        go = ~reached
        if not go.any():
            return
        gi = idx[go]
        step = np.minimum(speed[gi] * dt, dist[go])
        ux = dx[go] / dist[go]
        uy = dy[go] / dist[go]
        nx = self.x[gi] + ux * step
        ny = self.y[gi] + uy * step

        # Obstacle response: take the full step if it is clear, otherwise slide along
        # whichever single axis is clear, otherwise stop and report blocked. Sliding is
        # what stops robots from bunching against rack corners and looking frozen.
        clear = ~self._inside_obstacle(nx, ny)
        slide_x = ~clear & ~self._inside_obstacle(nx, self.y[gi])
        slide_y = ~clear & ~slide_x & ~self._inside_obstacle(self.x[gi], ny)
        stuck = ~clear & ~slide_x & ~slide_y

        self.x[gi] = np.where(clear | slide_x, nx, self.x[gi])
        self.y[gi] = np.where(clear | slide_y, ny, self.y[gi])

        if stuck.any():
            si = gi[stuck]
            self.status[si] = BLOCKED
            # Give them a new destination so they do not immediately re-wedge.
            self.tx[si], self.ty[si] = self._random_free_points(si.size)

        np.clip(self.x, 5.0, SITE_WIDTH - 5.0, out=self.x)
        np.clip(self.y, 5.0, SITE_HEIGHT - 5.0, out=self.y)
