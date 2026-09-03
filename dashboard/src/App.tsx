/**
 * Layout and the fleet-wide readouts.
 *
 * The KPI row answers "is the fleet healthy" at a glance; the map answers "where"; the
 * list answers "which one"; the trend answers "is this getting better or worse". Those
 * are the four questions an operator actually has, and they are the four regions of the
 * screen.
 */

import { useEffect, useState } from "react";
import AdminPanel from "./AdminPanel";
import FleetMap from "./FleetMap";
import RobotDetail from "./RobotDetail";
import RobotList from "./RobotList";
import TrendChart from "./TrendChart";
import { store, useFleetTick } from "./fleetStore";

export default function App() {
  const tick = useFleetTick(4);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [highlightIds, setHighlightIds] = useState<Set<string> | null>(null);

  useEffect(() => {
    store.connect();
    return () => store.disconnect();
  }, []);

  const summary = store.summary;
  const ready = Boolean(store.meta);
  const staleFeed = store.lastFrameAt > 0 && Date.now() - store.lastFrameAt > 5000;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo" />
          <div>
            <h1>Fleet Operations</h1>
            <p className="muted">Live warehouse telemetry</p>
          </div>
        </div>

        <div className="kpis">
          <Kpi label="Robots" value={summary ? summary.total : "--"} />
          <Kpi
            label="Working"
            value={summary ? `${summary.working_pct.toFixed(0)}%` : "--"}
            sub={summary ? `${summary.working} of ${summary.total}` : ""}
            tone="good"
          />
          <Kpi
            label="Needs attention"
            value={summary ? summary.attention : "--"}
            sub={summary && summary.stale ? `${summary.stale} silent` : ""}
            tone={summary && summary.attention > 0 ? "bad" : undefined}
          />
          <Kpi
            label="Avg battery"
            value={summary ? `${summary.avg_battery.toFixed(0)}%` : "--"}
            sub={summary ? `${summary.low_battery} low` : ""}
          />
        </div>

        <div className="conn">
          <span className={`pill ${store.connected ? (staleFeed ? "warn" : "live") : "down"}`}>
            {store.connected ? (staleFeed ? "stalled" : "live") : "reconnecting"}
          </span>
          <span className="muted">
            seq {store.seq} &middot; {store.frames} frames
            {store.gaps > 0 ? ` · ${store.gaps} gaps recovered` : ""}
            {store.reconnects > 0 ? ` · ${store.reconnects} reconnects` : ""}
          </span>
          <AdminPanel />
        </div>
      </header>

      {!ready ? (
        <div className="loading">Connecting to the fleet…</div>
      ) : (
        <main className="grid">
          <div className="col-map">
            <FleetMap
              selectedId={selectedId}
              onSelect={setSelectedId}
              highlightIds={highlightIds}
            />
            <TrendChart />
          </div>
          <aside className="col-side">
            <RobotList
              selectedId={selectedId}
              onSelect={setSelectedId}
              onFilteredChange={setHighlightIds}
            />
            <RobotDetail robotId={selectedId} onClose={() => setSelectedId(null)} />
          </aside>
        </main>
      )}

      <footer className="legend">
        {(store.meta?.statuses ?? []).map((name) => (
          <span key={name} className="legend-item">
            <span
              className="dot"
              style={{ background: store.meta?.status_colors[name] ?? "#94a3b8" }}
            />
            {name}
            <span className="muted">{summary?.by_status[name] ?? 0}</span>
          </span>
        ))}
        <span className="legend-item muted" key="tick" data-tick={tick}>
          working = {(store.meta?.working_statuses ?? []).join(", ")} &nbsp;|&nbsp; attention ={" "}
          {(store.meta?.attention_statuses ?? []).join(", ")} or no signal
        </span>
      </footer>
    </div>
  );
}

function Kpi({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string | number;
  sub?: string;
  tone?: "good" | "bad";
}) {
  return (
    <div className={`kpi${tone ? ` ${tone}` : ""}`}>
      <span className="kpi-label">{label}</span>
      <span className="kpi-value">{value}</span>
      <span className="kpi-sub">{sub ?? ""}</span>
    </div>
  );
}
