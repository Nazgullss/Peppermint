/**
 * Fleet trends over time.
 *
 * Seeded from the backend's own history so the chart is populated the moment the page
 * opens, then extended live from the broadcast summary. uPlot rather than a React chart
 * library: it renders to canvas, redraws thousands of points without touching the DOM,
 * and has drag-to-zoom on the time axis built in. A React charting library re-rendering
 * an SVG once a second is exactly the cost this dashboard cannot afford.
 *
 * Zoom: drag across the plot to zoom into a range, double-click to zoom back out. The
 * window buttons choose how much history is fetched in the first place.
 */

import { useEffect, useRef, useState } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";
import { store, useFleetTick } from "./fleetStore";

const WINDOWS = [
  { label: "1m", minutes: 1 },
  { label: "5m", minutes: 5 },
  { label: "15m", minutes: 15 },
  { label: "1h", minutes: 60 },
];

type Columns = [number[], number[], number[], number[]];

export default function TrendChart() {
  const wrapRef = useRef<HTMLDivElement>(null);
  const plotRef = useRef<uPlot | null>(null);
  const dataRef = useRef<Columns>([[], [], [], []]);
  const [minutes, setMinutes] = useState(15);
  const [points, setPoints] = useState(0);
  const tick = useFleetTick(1);

  // Create the plot once; series and axes never change shape afterwards.
  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;

    const options: uPlot.Options = {
      width: wrap.clientWidth,
      height: 190,
      padding: [12, 12, 0, 0],
      cursor: { drag: { x: true, y: false } },
      legend: { show: true, live: true },
      scales: {
        x: { time: true },
        pct: { range: [0, 100] },
        count: { range: (_u, _min, max) => [0, Math.max(5, max * 1.2)] },
      },
      axes: [
        { stroke: "#94a3b8", grid: { stroke: "#1e293b" }, ticks: { stroke: "#1e293b" } },
        {
          scale: "pct",
          stroke: "#94a3b8",
          grid: { stroke: "#1e293b" },
          ticks: { stroke: "#1e293b" },
          values: (_u, splits) => splits.map((v) => `${v}%`),
        },
        {
          scale: "count",
          side: 1,
          stroke: "#94a3b8",
          grid: { show: false },
          ticks: { stroke: "#1e293b" },
        },
      ],
      series: [
        { label: "time" },
        { label: "working", scale: "pct", stroke: "#22c55e", width: 2,
          value: (_u, v) => (v == null ? "--" : `${v.toFixed(1)}%`) },
        { label: "avg battery", scale: "pct", stroke: "#38bdf8", width: 1.5, dash: [4, 4],
          value: (_u, v) => (v == null ? "--" : `${v.toFixed(1)}%`) },
        { label: "needs attention", scale: "count", stroke: "#f87171", width: 2,
          value: (_u, v) => (v == null ? "--" : String(v)) },
      ],
    };

    const plot = new uPlot(options, dataRef.current as unknown as uPlot.AlignedData, wrap);
    plotRef.current = plot;

    const observer = new ResizeObserver(() => {
      plot.setSize({ width: wrap.clientWidth, height: 190 });
    });
    observer.observe(wrap);

    return () => {
      observer.disconnect();
      plot.destroy();
      plotRef.current = null;
    };
  }, []);

  // Refetch whenever the window changes: the backend trims to the requested range, so
  // the browser never holds more points than it is drawing.
  useEffect(() => {
    let cancelled = false;
    fetch(`/api/history/summary?minutes=${minutes}`)
      .then((response) => response.json())
      .then((history) => {
        if (cancelled) return;
        dataRef.current = [
          history.t,
          history.working_pct,
          history.avg_battery,
          history.attention,
        ];
        setPoints(history.t.length);
        plotRef.current?.setData(dataRef.current as unknown as uPlot.AlignedData);
      })
      .catch(() => {
        /* the reconnect loop will bring the socket back; the chart catches up then */
      });
    return () => {
      cancelled = true;
    };
  }, [minutes]);

  // Extend live, one point a second, dropping anything that has fallen out of the window.
  useEffect(() => {
    const summary = store.summary;
    const plot = plotRef.current;
    if (!summary || !plot) return;

    const now = Date.now() / 1000;
    const columns = dataRef.current;
    const last = columns[0][columns[0].length - 1];
    if (last !== undefined && now - last < 0.9) return;

    columns[0].push(now);
    columns[1].push(summary.working_pct);
    columns[2].push(summary.avg_battery);
    columns[3].push(summary.attention);

    const cutoff = now - minutes * 60;
    let drop = 0;
    while (drop < columns[0].length && columns[0][drop] < cutoff) drop++;
    if (drop > 0) for (const column of columns) column.splice(0, drop);

    setPoints(columns[0].length);
    plot.setData(columns as unknown as uPlot.AlignedData);
  }, [tick, minutes]);

  return (
    <section className="panel trend">
      <header className="panel-head">
        <h2>Fleet trend</h2>
        <div className="window-picker">
          {WINDOWS.map((w) => (
            <button
              key={w.label}
              className={w.minutes === minutes ? "active" : ""}
              onClick={() => setMinutes(w.minutes)}
            >
              {w.label}
            </button>
          ))}
          <span className="muted">{points} pts</span>
          <button onClick={() => plotRef.current?.setScale("x", { min: null!, max: null! })}>
            Reset zoom
          </button>
        </div>
      </header>
      <div className="trend-plot" ref={wrapRef} />
      <p className="muted trend-hint">
        Drag across the plot to zoom the time axis; double-click to zoom out.
      </p>
    </section>
  );
}
