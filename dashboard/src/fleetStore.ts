/**
 * The live fleet, held outside React.
 *
 * Robot state lives in a plain Map that the canvas reads directly on its own animation
 * frame. Putting 2000 robots into React state and updating them five times a second
 * would mean reconciling 10,000 elements per second, which is the easiest way to make
 * this dashboard unusable at exactly the fleet sizes it is meant to handle. React is
 * used only for the things that are genuinely small: the KPI row, the filtered list and
 * the detail panel -- and even those are throttled below the frame rate.
 */

import { useEffect, useState } from "react";

// Wire format, defined by shared/contract.py. Tuples rather than objects, because the
// key names would otherwise be repeated once per robot per frame.
// [robot_id, x, y, battery, status_code, type_code, stale]
export type PackedRobot = [string, number, number, number, number, number, number];

export interface Robot {
  id: string;
  /** Latest authoritative position from the backend -- where the robot actually is. */
  x: number;
  y: number;
  /** Where it was being drawn when that update landed, i.e. the interpolation origin. */
  px: number;
  py: number;
  /** Where it is drawn right now; the map advances this every animation frame. */
  rx: number;
  ry: number;
  /** performance.now() when the current move started. */
  t0: number;
  battery: number;
  status: number;
  type: number;
  stale: boolean;
}

export interface Summary {
  total: number;
  by_status: Record<string, number>;
  working: number;
  attention: number;
  stale: number;
  low_battery: number;
  avg_battery: number;
  working_pct: number;
}

export interface Meta {
  site: { width: number; height: number };
  obstacles: { x0: number; y0: number; x1: number; y1: number }[];
  docks: { x: number; y: number }[];
  statuses: string[];
  status_colors: Record<string, string>;
  working_statuses: string[];
  attention_statuses: string[];
  neutral_statuses: string[];
  robot_types: string[];
  battery_low: number;
}

interface Frame {
  type: "hello" | "snapshot" | "delta";
  seq: number;
  ts?: number;
  robots?: PackedRobot[];
  /** Robots explicitly decommissioned since the previous frame. */
  gone?: string[];
  summary?: Summary;
  meta?: Meta;
  broadcast_hz?: number;
}

export class FleetStore {
  /** The live fleet. Read directly by the canvas; never copied into React state. */
  readonly robots = new Map<string, Robot>();

  meta: Meta | null = null;
  summary: Summary | null = null;
  broadcastHz = 5;

  /**
   * Smoothed gap between position updates, in milliseconds -- the window the map
   * interpolates over. Seeded at one second and corrected from what actually arrives, so
   * turning the simulator's interval up or down needs no change here.
   */
  moveInterval = 1000;

  connected = false;
  seq = 0;
  frames = 0;
  gaps = 0;
  resyncs = 0;
  reconnects = 0;
  lastFrameAt = 0;
  /** Bumped on every applied frame so React views can tell that something changed. */
  version = 0;

  private listeners = new Set<() => void>();
  private ws: WebSocket | null = null;
  private retryDelay = 500;
  private retryTimer: number | null = null;
  private stopped = false;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  private emit(): void {
    for (const listener of this.listeners) listener();
  }

  // ---------------------------------------------------------------- frame handling --

  /**
   * Apply one frame from the backend.
   *
   * A snapshot replaces the fleet wholesale; a delta patches only the robots it carries.
   * Returns true when the caller should ask for a resync, which happens when `seq` skips:
   * a frame was dropped between the hub and here, and it may have carried an update that
   * will never be resent.
   */
  applyFrame(frame: Frame): boolean {
    if (frame.type === "hello") {
      this.meta = frame.meta ?? null;
      this.broadcastHz = frame.broadcast_hz ?? 5;
      this.seq = frame.seq;
      this.version++;
      this.emit();
      return false;
    }

    let needsResync = false;
    if (frame.type === "delta" && this.seq > 0 && frame.seq > this.seq + 1) {
      this.gaps++;
      needsResync = true;
    }
    this.seq = frame.seq;

    if (frame.type === "snapshot") {
      // A snapshot is the whole truth, so anything missing from it is gone. Without this
      // purge, shrinking the fleet from 900 to 250 would leave 650 ghosts on the map:
      // deltas only carry robots that changed, never robots that ceased to exist.
      const seen = new Set<string>();
      for (const packed of frame.robots ?? []) {
        this.put(packed);
        seen.add(packed[0]);
      }
      for (const id of [...this.robots.keys()]) {
        if (!seen.has(id)) this.robots.delete(id);
      }
    } else {
      // A decommissioned robot is dropped immediately rather than left to be purged by
      // the next periodic snapshot: the operator who just shrank the fleet is looking at
      // the map right now, and ten seconds of ghosts reads as a bug.
      for (const id of frame.gone ?? []) this.robots.delete(id);
      for (const packed of frame.robots ?? []) this.put(packed);
    }

    if (frame.summary) this.summary = frame.summary;
    this.frames++;
    this.lastFrameAt = Date.now();
    this.version++;
    this.emit();
    return needsResync;
  }

  private put(packed: PackedRobot): void {
    const now = performance.now();
    const existing = this.robots.get(packed[0]);
    if (existing) {
      // Mutated in place: at 2000 robots and 5 Hz, allocating a fresh object per robot
      // per frame hands the garbage collector 10,000 short-lived objects a second, and
      // the resulting collections show up as visible stutter on the map.
      if (existing.x !== packed[1] || existing.y !== packed[2]) {
        // A new destination. Interpolate from wherever the robot is currently *drawn*,
        // not from its last authoritative position: if the previous move had not
        // finished, restarting from the authoritative point would visibly snap it
        // forward before it resumed.
        const gap = now - existing.t0;
        if (gap > 50 && gap < 3000) {
          // One shared estimate of how often positions arrive. Every robot is updated by
          // the same simulator tick, so a global smoothed value is both more stable than
          // a per-robot one and correct for a robot that has just started moving after
          // sitting still.
          this.moveInterval = this.moveInterval * 0.8 + gap * 0.2;
        }
        existing.px = existing.rx;
        existing.py = existing.ry;
        existing.x = packed[1];
        existing.y = packed[2];
        existing.t0 = now;
      }
      existing.battery = packed[3];
      existing.status = packed[4];
      existing.type = packed[5];
      existing.stale = packed[6] === 1;
      return;
    }
    this.robots.set(packed[0], {
      id: packed[0],
      x: packed[1],
      y: packed[2],
      px: packed[1],
      py: packed[2],
      rx: packed[1],
      ry: packed[2],
      t0: now,
      battery: packed[3],
      status: packed[4],
      type: packed[5],
      stale: packed[6] === 1,
    });
  }

  /**
   * Advance every robot's drawn position towards its authoritative one.
   *
   * Called once per animation frame by the map. Robots report about once a second but the
   * display refreshes sixty times a second, so without this they would hop and then sit
   * still for sixty frames. Interpolating here means motion stays smooth no matter how
   * far apart the updates are -- the same separation of network rate from render rate the
   * backend makes on its side.
   *
   * Deliberately interpolation and not extrapolation: the drawn position never runs ahead
   * of the last thing the robot actually told us, so a late update makes a robot pause
   * rather than overshoot and snap back.
   */
  interpolate(now: number): void {
    const duration = this.moveInterval;
    for (const robot of this.robots.values()) {
      if (robot.rx === robot.x && robot.ry === robot.y) continue;
      const k = (now - robot.t0) / duration;
      if (k >= 1) {
        robot.rx = robot.x;
        robot.ry = robot.y;
      } else {
        robot.rx = robot.px + (robot.x - robot.px) * k;
        robot.ry = robot.py + (robot.y - robot.py) * k;
      }
    }
  }

  // -------------------------------------------------------------------- connection --

  connect(url = websocketUrl()): void {
    this.stopped = false;
    this.open(url);
  }

  private open(url: string): void {
    if (this.stopped) return;
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    this.ws = ws;

    ws.onopen = () => {
      this.connected = true;
      this.retryDelay = 500;
      this.emit();
    };

    ws.onmessage = (event) => {
      const text =
        typeof event.data === "string"
          ? event.data
          : new TextDecoder().decode(event.data as ArrayBuffer);
      let frame: Frame;
      try {
        frame = JSON.parse(text);
      } catch {
        return;
      }
      if (this.applyFrame(frame)) this.requestResync();
    };

    ws.onclose = () => {
      this.connected = false;
      this.ws = null;
      this.emit();
      this.scheduleRetry(url);
    };

    // An error is always followed by a close, so reconnection is handled in one place.
    ws.onerror = () => ws.close();
  }

  /**
   * Reconnect with exponential backoff and jitter, capped at 10 seconds.
   *
   * The jitter matters when the backend restarts: without it every open dashboard would
   * retry on exactly the same schedule and arrive together, which is the worst moment to
   * ask a service that has just come up for a full snapshot each.
   */
  private scheduleRetry(url: string): void {
    if (this.stopped || this.retryTimer !== null) return;
    const delay = this.retryDelay * (0.75 + Math.random() * 0.5);
    this.retryDelay = Math.min(this.retryDelay * 2, 10_000);
    this.retryTimer = window.setTimeout(() => {
      this.retryTimer = null;
      this.reconnects++;
      this.open(url);
    }, delay);
  }

  requestResync(): void {
    if (this.ws?.readyState !== WebSocket.OPEN) return;
    this.resyncs++;
    this.ws.send(JSON.stringify({ type: "resync" }));
  }

  disconnect(): void {
    this.stopped = true;
    if (this.retryTimer !== null) {
      window.clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    this.ws?.close();
    this.ws = null;
  }

  // ------------------------------------------------------------------- derived bits --

  statusName(code: number): string {
    return this.meta?.statuses[code] ?? "unknown";
  }

  statusColor(code: number): string {
    return this.meta?.status_colors[this.statusName(code)] ?? "#94a3b8";
  }

  typeName(code: number): string {
    return this.meta?.robot_types[code] ?? "robot";
  }

  needsAttention(robot: Robot): boolean {
    if (robot.stale) return true;
    return this.meta?.attention_statuses.includes(this.statusName(robot.status)) ?? false;
  }
}

export function websocketUrl(): string {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${location.host}/ws`;
}

/** One store for the page. */
export const store = new FleetStore();

/**
 * Re-render at most `hz` times a second, regardless of how fast frames arrive.
 *
 * The map does not use this -- it draws from the Map on its own animation frame. This is
 * for the React views, where a list of a few hundred rows does not need to be rebuilt at
 * the full broadcast rate to look live.
 */
export function useFleetTick(hz = 4): number {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const minGap = 1000 / hz;
    let last = 0;
    return store.subscribe(() => {
      const now = performance.now();
      if (now - last < minGap) return;
      last = now;
      setTick((value) => value + 1);
    });
  }, [hz]);
  return tick;
}
