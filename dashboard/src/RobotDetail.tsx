/**
 * Everything an operator needs about one robot in order to decide what to do next.
 *
 * Read live from the local Map rather than fetched, so it tracks the robot as it moves
 * with no extra request per selection. The battery sparkline is accumulated client-side
 * for the selected robot only: keeping a short trace for one robot is cheap, keeping it
 * for all of them is the per-robot history endpoint's job.
 */

import { useEffect, useRef } from "react";
import { store, useFleetTick } from "./fleetStore";

interface Props {
  robotId: string | null;
  onClose: () => void;
}

const TRACE_LENGTH = 90;

export default function RobotDetail({ robotId, onClose }: Props) {
  const tick = useFleetTick(4);
  const traceRef = useRef<{ id: string; values: number[] }>({ id: "", values: [] });
  const canvasRef = useRef<HTMLCanvasElement>(null);

  const robot = robotId ? store.robots.get(robotId) : undefined;

  // Accumulate the battery trace for whichever robot is selected.
  if (robot) {
    const trace = traceRef.current;
    if (trace.id !== robot.id) {
      trace.id = robot.id;
      trace.values = [];
    }
    const last = trace.values[trace.values.length - 1];
    if (last === undefined || Math.abs(last - robot.battery) > 0.001) {
      trace.values.push(robot.battery);
      if (trace.values.length > TRACE_LENGTH) trace.values.shift();
    }
  }

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const values = traceRef.current.values;
    if (values.length < 2) return;

    const low = store.meta?.battery_low ?? 30;
    ctx.strokeStyle = "#334155";
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(0, height - (low / 100) * height);
    ctx.lineTo(width, height - (low / 100) * height);
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.strokeStyle = "#38bdf8";
    ctx.lineWidth = 2;
    ctx.beginPath();
    values.forEach((value, index) => {
      const x = (index / (TRACE_LENGTH - 1)) * width;
      const y = height - (value / 100) * height;
      index === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.stroke();
  }, [tick, robotId]);

  if (!robotId) {
    return (
      <section className="panel detail">
        <header className="panel-head">
          <h2>Robot detail</h2>
        </header>
        <p className="empty">Select a robot on the map or in the list.</p>
      </section>
    );
  }

  if (!robot) {
    return (
      <section className="panel detail">
        <header className="panel-head">
          <h2>{robotId}</h2>
          <button className="link" onClick={onClose}>
            clear
          </button>
        </header>
        <p className="empty">
          {robotId} is no longer in the fleet. It may have been removed by a fleet-size
          change.
        </p>
      </section>
    );
  }

  const status = store.statusName(robot.status);
  const attention = store.needsAttention(robot);
  const low = robot.battery < (store.meta?.battery_low ?? 30);

  return (
    <section className="panel detail">
      <header className="panel-head">
        <h2>
          <span className="dot" style={{ background: store.statusColor(robot.status) }} />
          {robot.id}
        </h2>
        <button className="link" onClick={onClose}>
          clear
        </button>
      </header>

      {attention && (
        <p className="alert">
          {robot.stale
            ? "No telemetry received recently. The robot may have lost its connection or stopped mid-task."
            : `Reported ${status}. This robot cannot recover on its own.`}
        </p>
      )}

      <dl className="kv">
        <div>
          <dt>Status</dt>
          <dd>{robot.stale ? `${status} (no signal)` : status}</dd>
        </div>
        <div>
          <dt>Type</dt>
          <dd>{store.typeName(robot.type)}</dd>
        </div>
        <div>
          <dt>Battery</dt>
          <dd className={low ? "low" : ""}>{robot.battery.toFixed(1)}%</dd>
        </div>
        <div>
          <dt>Position</dt>
          <dd>
            {robot.x.toFixed(1)}, {robot.y.toFixed(1)}
          </dd>
        </div>
      </dl>

      <div className="spark">
        <span className="muted">Battery, last {traceRef.current.values.length} samples</span>
        <canvas ref={canvasRef} />
      </div>
    </section>
  );
}
