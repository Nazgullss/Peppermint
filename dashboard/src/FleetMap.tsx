/**
 * The site map.
 *
 * Draws on a single canvas from its own requestAnimationFrame loop, reading the robot
 * Map directly rather than going through React. Three things keep it usable at a few
 * thousand robots:
 *
 *  - Robots are bucketed by colour and each bucket drawn as one path. Eight fill calls
 *    per frame instead of one per robot: `fillStyle` changes are the expensive part of
 *    canvas 2D, not the geometry.
 *  - Above a threshold the marker switches from an arc to a rect, which is several times
 *    cheaper per robot and indistinguishable at the size a dot is drawn when 2000 of them
 *    share a screen.
 *  - Drawing is paced by the animation frame, not by the arrival of data. Frames arriving
 *    faster than the display refreshes cost nothing extra.
 */

import { useEffect, useRef, useState } from "react";
import { store, type Robot } from "./fleetStore";

interface Props {
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  /** Robots the current filter is interested in; others are dimmed. Null means all. */
  highlightIds: Set<string> | null;
}

interface View {
  scale: number;
  offsetX: number;
  offsetY: number;
}

/** Above this many robots, markers become rects and the dot shrinks. */
const DENSE_FLEET = 900;

export default function FleetMap({ selectedId, onSelect, highlightIds }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<View>({ scale: 1, offsetX: 0, offsetY: 0 });
  const fittedRef = useRef(false);
  const dragRef = useRef<{ x: number; y: number; ox: number; oy: number } | null>(null);
  const selectedRef = useRef(selectedId);
  const highlightRef = useRef(highlightIds);
  const [zoomLabel, setZoomLabel] = useState(100);

  // Kept in refs so the render loop never has to be torn down and rebuilt when the
  // selection changes -- restarting the loop mid-drag would drop frames.
  selectedRef.current = selectedId;
  highlightRef.current = highlightIds;

  useEffect(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;
    const ctx = canvas.getContext("2d", { alpha: false });
    if (!ctx) return;

    let raf = 0;
    let width = 0;
    let height = 0;

    // Reused across frames. Rebuilding this Map and its arrays every frame would hand the
    // collector 5000 pushes and a fresh Map sixty times a second at large fleet sizes,
    // and that churn is itself a source of the stutter interpolation is meant to remove.
    const buckets = new Map<string, Robot[]>();

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const rect = wrap.getBoundingClientRect();
      width = rect.width;
      height = rect.height;
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      fittedRef.current = false;
    };

    const fit = () => {
      const site = store.meta?.site;
      if (!site || !width || !height) return;
      const pad = 16;
      const scale = Math.min(
        (width - pad * 2) / site.width,
        (height - pad * 2) / site.height,
      );
      viewRef.current = {
        scale,
        offsetX: (width - site.width * scale) / 2,
        offsetY: (height - site.height * scale) / 2,
      };
      fittedRef.current = true;
      setZoomLabel(Math.round(scale * 100));
    };

    const draw = (now: number) => {
      raf = requestAnimationFrame(draw);
      const meta = store.meta;
      if (!meta) return;
      if (!fittedRef.current) fit();

      // Advance every robot towards its last reported position before drawing anything.
      store.interpolate(now);

      const { scale, offsetX, offsetY } = viewRef.current;
      const toX = (x: number) => x * scale + offsetX;
      const toY = (y: number) => y * scale + offsetY;

      ctx.fillStyle = "#0b1120";
      ctx.fillRect(0, 0, width, height);

      // Floor
      ctx.fillStyle = "#111c33";
      ctx.fillRect(toX(0), toY(0), meta.site.width * scale, meta.site.height * scale);
      ctx.strokeStyle = "#22304d";
      ctx.lineWidth = 1;
      ctx.strokeRect(toX(0), toY(0), meta.site.width * scale, meta.site.height * scale);

      // Racks
      ctx.fillStyle = "#1e293b";
      ctx.strokeStyle = "#33415c";
      for (const o of meta.obstacles) {
        const x = toX(o.x0);
        const y = toY(o.y0);
        const w = (o.x1 - o.x0) * scale;
        const h = (o.y1 - o.y0) * scale;
        ctx.fillRect(x, y, w, h);
        ctx.strokeRect(x, y, w, h);
      }

      // Charging docks
      ctx.strokeStyle = "#a78bfa";
      ctx.lineWidth = 1.5;
      for (const d of meta.docks) {
        const r = Math.max(4, 9 * scale);
        ctx.beginPath();
        ctx.arc(toX(d.x), toY(d.y), r, 0, Math.PI * 2);
        ctx.stroke();
      }

      // Robots, bucketed by colour so each colour is one fill call.
      const robots = store.robots;
      const dense = robots.size > DENSE_FLEET;
      const radius = dense
        ? Math.max(1.4, 2.6 * scale)
        : Math.max(2.2, 4.2 * scale);
      const highlight = highlightRef.current;
      for (const list of buckets.values()) list.length = 0;

      for (const robot of robots.values()) {
        const dim = highlight !== null && !highlight.has(robot.id);
        const color = dim ? "#334155" : store.statusColor(robot.status);
        let bucket = buckets.get(color);
        if (!bucket) buckets.set(color, (bucket = []));
        bucket.push(robot);
      }

      for (const [color, list] of buckets) {
        if (list.length === 0) continue;
        ctx.fillStyle = color;
        if (dense) {
          const size = radius * 2;
          for (const robot of list) {
            ctx.fillRect(toX(robot.rx) - radius, toY(robot.ry) - radius, size, size);
          }
        } else {
          ctx.beginPath();
          for (const robot of list) {
            const x = toX(robot.rx);
            const y = toY(robot.ry);
            ctx.moveTo(x + radius, y);
            ctx.arc(x, y, radius, 0, Math.PI * 2);
          }
          ctx.fill();
        }
      }

      // Robots needing attention get a ring, so they are findable without hunting for a
      // colour among two thousand dots. Drawn after the bulk pass so nothing covers them.
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = "#f87171";
      ctx.beginPath();
      let attention = 0;
      for (const robot of robots.values()) {
        if (!store.needsAttention(robot)) continue;
        if (++attention > 400) break; // a ring per robot stops being information past this
        const x = toX(robot.rx);
        const y = toY(robot.ry);
        ctx.moveTo(x + radius + 3, y);
        ctx.arc(x, y, radius + 3, 0, Math.PI * 2);
      }
      ctx.stroke();

      // Selection
      const selected = selectedRef.current ? robots.get(selectedRef.current) : undefined;
      if (selected) {
        const x = toX(selected.rx);
        const y = toY(selected.ry);
        ctx.strokeStyle = "#f8fafc";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(x, y, radius + 7, 0, Math.PI * 2);
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(x - radius - 14, y);
        ctx.lineTo(x - radius - 8, y);
        ctx.moveTo(x + radius + 8, y);
        ctx.lineTo(x + radius + 14, y);
        ctx.moveTo(x, y - radius - 14);
        ctx.lineTo(x, y - radius - 8);
        ctx.moveTo(x, y + radius + 8);
        ctx.lineTo(x, y + radius + 14);
        ctx.stroke();

        ctx.fillStyle = "#f8fafc";
        ctx.font = "600 12px ui-sans-serif, system-ui, sans-serif";
        ctx.fillText(selected.id, x + radius + 12, y - radius - 10);
      }
    };

    const observer = new ResizeObserver(resize);
    observer.observe(wrap);
    resize();
    raf = requestAnimationFrame(draw);

    return () => {
      cancelAnimationFrame(raf);
      observer.disconnect();
    };
  }, []);

  // ------------------------------------------------------------------ interaction --

  const worldFromEvent = (event: React.MouseEvent) => {
    const canvas = canvasRef.current!;
    const rect = canvas.getBoundingClientRect();
    const { scale, offsetX, offsetY } = viewRef.current;
    return {
      x: (event.clientX - rect.left - offsetX) / scale,
      y: (event.clientY - rect.top - offsetY) / scale,
    };
  };

  const handleWheel = (event: React.WheelEvent) => {
    const canvas = canvasRef.current!;
    const rect = canvas.getBoundingClientRect();
    const view = viewRef.current;
    const px = event.clientX - rect.left;
    const py = event.clientY - rect.top;
    const factor = event.deltaY < 0 ? 1.15 : 1 / 1.15;
    const next = Math.max(0.2, Math.min(view.scale * factor, 12));
    // Keep the point under the cursor fixed while zooming.
    view.offsetX = px - ((px - view.offsetX) * next) / view.scale;
    view.offsetY = py - ((py - view.offsetY) * next) / view.scale;
    view.scale = next;
    setZoomLabel(Math.round(next * 100));
  };

  const handleMouseDown = (event: React.MouseEvent) => {
    const view = viewRef.current;
    dragRef.current = {
      x: event.clientX,
      y: event.clientY,
      ox: view.offsetX,
      oy: view.offsetY,
    };
  };

  const handleMouseMove = (event: React.MouseEvent) => {
    const drag = dragRef.current;
    if (!drag) return;
    const view = viewRef.current;
    view.offsetX = drag.ox + (event.clientX - drag.x);
    view.offsetY = drag.oy + (event.clientY - drag.y);
  };

  const handleMouseUp = (event: React.MouseEvent) => {
    const drag = dragRef.current;
    dragRef.current = null;
    if (!drag) return;
    const moved = Math.hypot(event.clientX - drag.x, event.clientY - drag.y);
    if (moved > 4) return; // a drag, not a click

    // Nearest robot within a generous radius. Linear over the fleet, but this runs once
    // per click, not once per frame.
    const { x, y } = worldFromEvent(event);
    const reach = 14 / viewRef.current.scale;
    let best: string | null = null;
    let bestDistance = reach;
    for (const robot of store.robots.values()) {
      // Hit-test against the drawn position, not the authoritative one, so a click lands
      // on the dot the operator is actually looking at.
      const distance = Math.hypot(robot.rx - x, robot.ry - y);
      if (distance < bestDistance) {
        bestDistance = distance;
        best = robot.id;
      }
    }
    onSelect(best);
  };

  const resetView = () => {
    fittedRef.current = false;
  };

  return (
    <div className="map" ref={wrapRef}>
      <canvas
        ref={canvasRef}
        onWheel={handleWheel}
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onMouseLeave={() => (dragRef.current = null)}
      />
      <div className="map-controls">
        <span className="map-zoom">{zoomLabel}%</span>
        <button onClick={resetView} title="Fit the whole site">
          Fit
        </button>
      </div>
      <div className="map-hint">scroll to zoom &middot; drag to pan &middot; click a robot</div>
    </div>
  );
}
