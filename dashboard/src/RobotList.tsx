/**
 * Finding one robot, or the ones that need looking at.
 *
 * The rows are virtualised, so the DOM holds the twenty-odd rows on screen whether the
 * fleet is eight robots or five thousand. The filter runs over the live Map at 3 Hz
 * rather than on every frame: a list that rewrites itself five times a second is harder
 * to read and click than one that settles, and the map next to it is already showing
 * movement at full rate.
 */

import { useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { store, useFleetTick, type Robot } from "./fleetStore";

interface Props {
  selectedId: string | null;
  onSelect: (id: string) => void;
  onFilteredChange: (ids: Set<string> | null) => void;
}

type Sort = "attention" | "battery" | "id";

export default function RobotList({ selectedId, onSelect, onFilteredChange }: Props) {
  const tick = useFleetTick(3);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [attentionOnly, setAttentionOnly] = useState(false);
  const [sort, setSort] = useState<Sort>("attention");
  const scrollRef = useRef<HTMLDivElement>(null);

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const list: Robot[] = [];
    for (const robot of store.robots.values()) {
      if (needle && !robot.id.toLowerCase().includes(needle)) continue;
      if (statusFilter && store.statusName(robot.status) !== statusFilter) continue;
      if (attentionOnly && !store.needsAttention(robot)) continue;
      list.push(robot);
    }

    if (sort === "battery") {
      list.sort((a, b) => a.battery - b.battery);
    } else if (sort === "id") {
      list.sort((a, b) =>
        a.id.localeCompare(b.id, undefined, { numeric: true, sensitivity: "base" }),
      );
    } else {
      // Most urgent first: anything needing attention, then the flattest battery.
      list.sort((a, b) => {
        const urgency = Number(store.needsAttention(b)) - Number(store.needsAttention(a));
        return urgency !== 0 ? urgency : a.battery - b.battery;
      });
    }
    return list;
    // `tick` is the dependency that actually drives this: the Map is mutated in place,
    // so its identity never changes and cannot be a dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick, query, statusFilter, attentionOnly, sort]);

  // Tell the map which robots survive the filter, so it can dim the rest.
  const filtering = Boolean(query.trim() || statusFilter || attentionOnly);
  const signature = filtering ? `${rows.length}:${query}:${statusFilter}:${attentionOnly}` : "";
  useMemo(() => {
    onFilteredChange(filtering ? new Set(rows.map((r) => r.id)) : null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature, tick]);

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 44,
    overscan: 8,
  });

  const statuses = store.meta?.statuses ?? [];

  return (
    <section className="panel list">
      <header className="panel-head">
        <h2>Robots</h2>
        <span className="muted">
          {rows.length}
          {rows.length !== store.robots.size ? ` / ${store.robots.size}` : ""}
        </span>
      </header>

      <div className="filters">
        <input
          className="search"
          placeholder="Find a robot, e.g. r42"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <div className="filter-row">
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="">All statuses</option>
            {statuses.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
          <select value={sort} onChange={(e) => setSort(e.target.value as Sort)}>
            <option value="attention">Sort: urgency</option>
            <option value="battery">Sort: battery</option>
            <option value="id">Sort: id</option>
          </select>
        </div>
        <label className="check">
          <input
            type="checkbox"
            checked={attentionOnly}
            onChange={(event) => setAttentionOnly(event.target.checked)}
          />
          Needs attention only
        </label>
      </div>

      <div className="rows" ref={scrollRef}>
        <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
          {virtualizer.getVirtualItems().map((item) => {
            const robot = rows[item.index];
            if (!robot) return null;
            const status = store.statusName(robot.status);
            const attention = store.needsAttention(robot);
            const low = robot.battery < (store.meta?.battery_low ?? 30);
            return (
              <button
                key={robot.id}
                className={`row${robot.id === selectedId ? " selected" : ""}`}
                style={{
                  position: "absolute",
                  top: 0,
                  left: 0,
                  width: "100%",
                  height: item.size,
                  transform: `translateY(${item.start}px)`,
                }}
                onClick={() => onSelect(robot.id)}
              >
                <span
                  className="dot"
                  style={{ background: store.statusColor(robot.status) }}
                />
                <span className="row-id">{robot.id}</span>
                <span className="row-status">{robot.stale ? "no signal" : status}</span>
                <span className={`row-batt${low ? " low" : ""}`}>
                  {robot.battery.toFixed(0)}%
                </span>
                {attention && <span className="flag" title="Needs attention" />}
              </button>
            );
          })}
        </div>
        {rows.length === 0 && <p className="empty">No robot matches that filter.</p>}
      </div>
    </section>
  );
}
