"""The data contract shared by the simulator, the backend and (via /api/meta) the dashboard.

Everything that both sides of the wire must agree on lives here and nowhere else:
the site geometry, the status vocabulary, the operator-facing meaning of each status,
and the compact tuple encoding used for broadcast frames.
"""

from __future__ import annotations

from typing import Final

# --------------------------------------------------------------------------------------
# Site geometry
#
# Taken from layout.png. Origin (0, 0) is the top-left corner, x grows right, y grows
# down, and one pixel is one unit -- so these numbers are both pixels and world units.
# The obstacle rectangles were extracted from the image rather than eyeballed.
# --------------------------------------------------------------------------------------

SITE_WIDTH: Final = 900.0
SITE_HEIGHT: Final = 560.0

# (x0, y0, x1, y1), inclusive pixel bounds of each shelving block.
OBSTACLES: Final[tuple[tuple[float, float, float, float], ...]] = (
    (500.0, 60.0, 560.0, 460.0),   # long central rack
    (150.0, 80.0, 350.0, 140.0),   # left block, top
    (150.0, 220.0, 350.0, 280.0),  # left block, middle
    (150.0, 360.0, 350.0, 420.0),  # left block, bottom
    (650.0, 150.0, 850.0, 200.0),  # right block, top
    (650.0, 340.0, 850.0, 390.0),  # right block, bottom
)

# Robots keep this much clearance from a rack so they never render half-inside one.
OBSTACLE_MARGIN: Final = 4.0

# Charging docks, in the open aisles along the top and bottom edges. Spread so that no
# point on the floor is more than ~250 units from one: a robot that starts looking for a
# charger at BATTERY_LOW must be able to reach one before it goes flat, and with only a
# bottom row the far corners could not.
DOCKS: Final[tuple[tuple[float, float], ...]] = (
    (60.0, 40.0),
    (60.0, 510.0),
    (430.0, 40.0),
    (430.0, 510.0),
    (600.0, 510.0),
    (875.0, 40.0),
    (875.0, 510.0),
)

# --------------------------------------------------------------------------------------
# Status vocabulary
#
# The eight statuses are fixed by the challenge's data contract. The integer codes are
# ours: they are what actually travels on the wire, because sending 3 instead of
# "on_mission" across 2000 robots at 5 Hz is a meaningful bandwidth saving.
# --------------------------------------------------------------------------------------

STATUSES: Final[tuple[str, ...]] = (
    "idle",
    "active",
    "on_mission",
    "charging",
    "blocked",
    "error",
    "maintenance",
    "offline",
)

STATUS_CODE: Final[dict[str, int]] = {name: i for i, name in enumerate(STATUSES)}

IDLE: Final = STATUS_CODE["idle"]
ACTIVE: Final = STATUS_CODE["active"]
ON_MISSION: Final = STATUS_CODE["on_mission"]
CHARGING: Final = STATUS_CODE["charging"]
BLOCKED: Final = STATUS_CODE["blocked"]
ERROR: Final = STATUS_CODE["error"]
MAINTENANCE: Final = STATUS_CODE["maintenance"]
OFFLINE: Final = STATUS_CODE["offline"]

# --------------------------------------------------------------------------------------
# Operator semantics
#
# The challenge deliberately leaves "working" and "needs attention" undefined. This is our
# call, and the dashboard, the KPIs and the trend chart are all derived from it:
#
#   WORKING   -- the robot is doing useful work, or deliberately preparing to (charging).
#                Nobody needs to look at it.
#   ATTENTION -- the robot is not doing useful work AND cannot fix itself. A human should
#                look. `blocked` is here rather than in NEUTRAL because a robot that
#                cannot find a way around an obstruction is a floor problem, not a robot
#                problem, and it is the earliest signal an operator can act on.
#   NEUTRAL   -- healthy but unproductive (idle), or already being handled (maintenance).
#
# `active` vs `on_mission`: we read `active` as powered up and moving under its own
# schedule (repositioning, returning to a staging point) and `on_mission` as executing an
# assigned task. Both count as working; the split is kept because the source data makes
# it and an operator may want to know how much of the fleet is on real work.
# --------------------------------------------------------------------------------------

WORKING_STATUSES: Final[frozenset[str]] = frozenset({"active", "on_mission", "charging"})
ATTENTION_STATUSES: Final[frozenset[str]] = frozenset({"blocked", "error", "offline"})
NEUTRAL_STATUSES: Final[frozenset[str]] = frozenset({"idle", "maintenance"})

WORKING_CODES: Final[frozenset[int]] = frozenset(STATUS_CODE[s] for s in WORKING_STATUSES)
ATTENTION_CODES: Final[frozenset[int]] = frozenset(STATUS_CODE[s] for s in ATTENTION_STATUSES)

# Colours live in the contract so the map, the list and the chart cannot drift apart.
STATUS_COLORS: Final[dict[str, str]] = {
    "idle": "#94a3b8",
    "active": "#38bdf8",
    "on_mission": "#22c55e",
    "charging": "#a78bfa",
    "blocked": "#f59e0b",
    "error": "#ef4444",
    "maintenance": "#f472b6",
    "offline": "#64748b",
}

# --------------------------------------------------------------------------------------
# Behaviour constants
#
# Derived from the 15-minute reference log in events.jsonl by differencing consecutive
# samples per robot. Keeping the simulator anchored to these numbers is what makes its
# output plausible rather than merely random.
# --------------------------------------------------------------------------------------

# Units per second, by status. Measured means were on_mission 2.07 and active 1.84.
SPEED_BY_STATUS: Final[dict[int, float]] = {
    IDLE: 0.0,
    ACTIVE: 1.85,
    ON_MISSION: 2.10,
    CHARGING: 0.0,
    BLOCKED: 0.0,
    ERROR: 0.0,
    MAINTENANCE: 0.0,
    OFFLINE: 0.0,
}
MAX_SPEED: Final = 4.0  # observed peak was 3.89 units/s

# Battery percentage points per second, by status. Negative drains, positive charges.
BATTERY_RATE: Final[dict[int, float]] = {
    IDLE: -0.015,
    ACTIVE: -0.101,
    ON_MISSION: -0.106,
    CHARGING: +0.411,
    BLOCKED: -0.020,
    ERROR: -0.010,
    MAINTENANCE: -0.010,
    OFFLINE: -0.010,
}

# A robot must reserve enough charge to actually reach a dock. Worst case is ~250 units
# of travel at 1.85 units/s, which costs ~14 points at the moving drain rate, so 30 leaves
# a real margin. At 20 the fleet slowly died in transit -- robots went flat on the way to
# the charger, which is a genuine failure mode but not the steady state we want.
BATTERY_LOW: Final = 30.0    # below this a robot abandons its task and seeks a dock
BATTERY_FULL: Final = 92.0   # above this it undocks and returns to the pool

ROBOT_TYPES: Final[tuple[str, ...]] = ("picker", "hauler")

# --------------------------------------------------------------------------------------
# Wire encoding
#
# Broadcast frames carry robots as fixed-length tuples rather than objects. Against the
# same fleet this is roughly 60% smaller than the equivalent JSON objects, because the
# key names are not repeated once per robot per frame.
#
#   [robot_id, x, y, battery, status_code, type_code, stale]
#
# `stale` is 1 when the backend has stopped hearing from the robot. It is a separate flag
# rather than an overloaded status because the two mean different things to an operator:
# a robot that reports "offline" told us it was going down, while a stale robot stopped
# talking without saying anything -- which is what a robot dying mid-task looks like.
# --------------------------------------------------------------------------------------

PACKED_FIELDS: Final = ("robot_id", "x", "y", "battery", "status", "robot_type", "stale")

PackedRobot = tuple[str, float, float, float, int, int, int]


def pack(
    robot_id: str, x: float, y: float, battery: float,
    status: int, type_code: int, stale: bool = False,
) -> PackedRobot:
    """Encode one robot for the wire. Positions are rounded to 0.1 units and battery to
    0.1 percent: below that the numbers are noise, and the rounding is worth several
    bytes per robot per frame."""
    return (robot_id, round(x, 1), round(y, 1), round(battery, 1), status, type_code, int(stale))


def is_inside_obstacle(x: float, y: float, margin: float = 0.0) -> bool:
    """Scalar obstacle test. The simulator uses a vectorised version of this over the
    whole fleet at once; this one exists for tests and for validating dock placement."""
    for x0, y0, x1, y1 in OBSTACLES:
        if x0 - margin <= x <= x1 + margin and y0 - margin <= y <= y1 + margin:
            return True
    return False


def meta() -> dict:
    """The self-describing payload the dashboard fetches once on load, so that the site
    geometry and status semantics are never hardcoded twice."""
    return {
        "site": {"width": SITE_WIDTH, "height": SITE_HEIGHT},
        "obstacles": [
            {"x0": x0, "y0": y0, "x1": x1, "y1": y1} for x0, y0, x1, y1 in OBSTACLES
        ],
        "docks": [{"x": x, "y": y} for x, y in DOCKS],
        "statuses": list(STATUSES),
        "status_colors": STATUS_COLORS,
        "working_statuses": sorted(WORKING_STATUSES),
        "attention_statuses": sorted(ATTENTION_STATUSES),
        "neutral_statuses": sorted(NEUTRAL_STATUSES),
        "robot_types": list(ROBOT_TYPES),
        "packed_fields": list(PACKED_FIELDS),
        "battery_low": BATTERY_LOW,
    }
