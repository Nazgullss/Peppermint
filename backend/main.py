"""The backend service: broker in, dashboards out.

Two background loops do the work. `ingest.run` fills the fleet state from the broker as
fast as robots publish; `hub.run` empties it towards the browsers on its own fixed clock.
Neither knows the other exists, and that is the whole design: ingest rate and render rate
are separate numbers, so turning one up does not move the other.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import AsyncIterator
from pathlib import Path

import orjson
from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.config import settings
from backend.hub import Client, Hub
from backend.ingest import Ingest
from backend.state import FleetState
from backend.trends import TrendBuffer
from shared.contract import ATTENTION_STATUSES, meta
from shared.eventloop import use_selector_loop_on_windows

use_selector_loop_on_windows()

log = logging.getLogger("backend")

# One state, one ingest, one hub, for the life of the process. The fleet lives in memory
# and has exactly one owner; see ARCHITECTURE.md for what scaling past one process costs.
state = FleetState(stale_after=settings.stale_after_seconds)
ingest = Ingest(state, settings)
hub = Hub(state, settings)
trends = TrendBuffer(retention_minutes=settings.history_retention_minutes)


async def sample_trends() -> None:
    """Record one fleet-wide data point a second, whether or not anyone is watching.

    Deliberately its own task rather than a line inside the broadcast tick: the hub skips
    building frames when no dashboard is connected, and history that only accumulates
    while someone is looking at it is not history.
    """
    while True:
        trends.sample(state.summary())
        await asyncio.sleep(trends.interval)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the background loops that make this a service rather than a website."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    tasks = [
        asyncio.create_task(ingest.run(), name="ingest"),
        asyncio.create_task(hub.run(), name="broadcast"),
        asyncio.create_task(sample_trends(), name="trends"),
    ]
    log.info("backend up: broadcasting at %.1f Hz", settings.broadcast_hz)
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="Fleet Management Backend", lifespan=lifespan)

if settings.cors_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_list,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """The live feed the dashboard runs on.

    A client gets `hello` (site geometry and status vocabulary), then an immediate
    snapshot so the map is populated before the first delta arrives, then deltas on the
    hub's clock.

    Note the ordering below: both opening frames are written directly, and the pump task
    and hub registration come afterwards. Two coroutines writing to one WebSocket would
    interleave their frames, so there is never more than one writer -- first this
    function, then the pump, never both.
    """
    await ws.accept()
    client = Client(ws, queue_size=settings.client_queue_size)

    await ws.send_bytes(hub.hello())
    await ws.send_bytes(hub.snapshot_frame())

    pump = asyncio.create_task(client.pump())
    hub.add(client)
    log.info("dashboard connected (%d total)", len(hub.clients))

    try:
        while True:
            raw = await ws.receive_text()
            try:
                message = orjson.loads(raw)
            except orjson.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue

            # A client that spots a gap in `seq` asks for a fresh snapshot rather than
            # guessing. The hub serves it on the next tick.
            if message.get("type") == "resync":
                client.wants_snapshot = True

    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("dashboard connection failed")
    finally:
        pump.cancel()
        hub.remove(client)
        log.info("dashboard disconnected (%d left, %d frames dropped)",
                 len(hub.clients), client.dropped)


# ======================================================================================
# Read endpoints
# ======================================================================================

@app.get("/healthz")
def healthz() -> dict:
    """Liveness for the load balancer. Deliberately does not depend on the broker: the
    service is up and serving whatever state it has even while the broker is down."""
    return {"ok": True}


@app.get("/api/meta")
def get_meta() -> dict:
    """Site geometry and status vocabulary. The dashboard fetches this once so that
    nothing about the contract is duplicated in frontend code."""
    return meta()


@app.get("/api/stats")
def get_stats() -> dict:
    """Everything we measure about ourselves, in one place.

    This is the endpoint the load tests read: ingest rate, frame build cost, frame size
    and dropped-frame counts are what tell us where the system starts to bend, and they
    are cheap enough to serve continuously.
    """
    return {
        "ingest": ingest.stats(),
        "broadcast": hub.stats(),
        "state": state.stats(),
        "trends": trends.stats(),
        "summary": state.summary(),
        "clients": [c.stats() for c in hub.clients],
    }


@app.get("/api/history/summary")
def get_summary_history(minutes: float = 15.0) -> dict:
    """Fleet-wide history for the trend chart, in uPlot's columnar layout.

    `minutes` selects the window. The chart asks for a wider window when the operator
    zooms out, so the browser never holds more points than it is currently drawing.
    """
    since_ms = int((time.time() - max(0.1, minutes) * 60) * 1000)
    return {"window_minutes": minutes, **trends.series(since_ms)}


@app.get("/api/robots")
def list_robots(
    q: str = "",
    status: str = "",
    attention: bool = False,
    limit: int = 500,
) -> dict:
    """The robot list, filtered server-side.

    Filtering here rather than in the browser means a search across 5000 robots costs one
    pass over a dict instead of shipping 5000 rows the operator will not look at.
    """
    wanted = {s.strip() for s in status.split(",") if s.strip()}
    needle = q.strip().lower()

    rows = []
    for robot_id in state.robots:
        row = state.get(robot_id)
        if row is None:
            continue
        if needle and needle not in robot_id.lower():
            continue
        if wanted and row["status"] not in wanted:
            continue
        if attention and not row["needs_attention"]:
            continue
        rows.append(row)

    # Most urgent first: anything needing attention, then the flattest battery.
    rows.sort(key=lambda r: (not r["needs_attention"], r["battery"]))
    return {
        "total": len(rows),
        "returned": min(len(rows), limit),
        "robots": rows[:limit],
        "attention_statuses": sorted(ATTENTION_STATUSES),
    }


@app.get("/api/robots/{robot_id}")
def get_robot(robot_id: str) -> dict:
    row = state.get(robot_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown robot {robot_id!r}")
    return row


# ======================================================================================
# Admin: the live knobs
# ======================================================================================

def require_admin(authorization: str | None = Header(default=None)) -> None:
    """Guard the live controls with a bearer token.

    An empty token disables the endpoints outright rather than falling back to a default
    password, so an operator who forgets to configure one gets a closed door instead of a
    guessable one. The comparison is constant-time so that a wrong token cannot be
    narrowed down by timing it.
    """
    if not settings.admin_token:
        raise HTTPException(
            status_code=503,
            detail="admin controls are disabled; set BACKEND_ADMIN_TOKEN to enable them",
        )
    expected = f"Bearer {settings.admin_token}"
    if not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid or missing admin token")


class ControlRequest(BaseModel):
    """The three knobs the challenge asks to be adjustable, plus the batching one.

    All optional: a request sets only what it names, so the operator can change the fleet
    size without having to restate the interval.
    """
    fleet_size: int | None = Field(default=None, ge=0, le=20_000)
    update_interval_ms: int | None = Field(default=None, ge=20, le=60_000)
    payload_padding_bytes: int | None = Field(default=None, ge=0, le=64_000)
    publish_batch_size: int | None = Field(default=None, ge=1, le=1_000)


@app.get("/api/admin/config")
def get_config() -> dict:
    """What the simulator last told us about itself. Unauthenticated on purpose: it is
    the same information the dashboard already shows, and reading it changes nothing."""
    return {
        "simulator": ingest.sim_status,
        "backend": {
            "broadcast_hz": settings.broadcast_hz,
            "snapshot_every_n_ticks": settings.snapshot_every_n_ticks,
            "stale_after_seconds": settings.stale_after_seconds,
            "client_queue_size": settings.client_queue_size,
        },
        "admin_enabled": bool(settings.admin_token),
    }


@app.post("/api/admin/config", dependencies=[Depends(require_admin)])
async def set_config(request: ControlRequest) -> dict:
    """Change the fleet at runtime, with no redeploy.

    We do not reach into the simulator: we publish to the broker's control topic and the
    simulator applies it. That keeps the broker the only path to the fleet, so this
    endpoint is the single place where access has to be checked.
    """
    payload = request.model_dump(exclude_none=True)
    if not payload:
        raise HTTPException(status_code=400, detail="no settings supplied")
    if not await ingest.publish_control(payload):
        raise HTTPException(status_code=503, detail="not connected to the broker")
    return {"requested": payload, "note": "simulator applies this on its next tick"}


# ======================================================================================
# The dashboard's own static build, served from this origin when it exists.
# Mounted last, because it claims "/" and would otherwise shadow the API routes above.
# ======================================================================================

DASHBOARD_DIST = Path(__file__).resolve().parent.parent / "dashboard" / "dist"
if DASHBOARD_DIST.is_dir():
    app.mount("/", StaticFiles(directory=DASHBOARD_DIST, html=True), name="dashboard")
    log.info("serving dashboard from %s", DASHBOARD_DIST)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000)
