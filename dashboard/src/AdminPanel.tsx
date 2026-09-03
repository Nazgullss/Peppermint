/**
 * The live controls: change the fleet without a redeploy.
 *
 * The token is held in component state and sent as a bearer header. It is deliberately
 * not persisted to localStorage -- an admin token that survives in a shared browser is a
 * worse problem than retyping it, and the backend rejects an absent one outright.
 */

import { useEffect, useState } from "react";

interface SimulatorConfig {
  fleet_size?: number;
  update_interval_ms?: number;
  payload_padding_bytes?: number;
  publish_batch_size?: number;
  overruns?: number;
  last_publish_seconds?: number;
  connected?: boolean;
}

export default function AdminPanel() {
  const [open, setOpen] = useState(false);
  const [token, setToken] = useState("");
  const [config, setConfig] = useState<SimulatorConfig>({});
  const [enabled, setEnabled] = useState(true);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const [fleetSize, setFleetSize] = useState("");
  const [interval, setIntervalMs] = useState("");
  const [padding, setPadding] = useState("");
  const [batch, setBatch] = useState("");

  useEffect(() => {
    if (!open) return;
    const load = () =>
      fetch("/api/admin/config")
        .then((response) => response.json())
        .then((data) => {
          setConfig(data.simulator ?? {});
          setEnabled(Boolean(data.admin_enabled));
        })
        .catch(() => undefined);
    load();
    const id = window.setInterval(load, 2000);
    return () => window.clearInterval(id);
  }, [open]);

  const apply = async () => {
    const body: Record<string, number> = {};
    if (fleetSize.trim()) body.fleet_size = Number(fleetSize);
    if (interval.trim()) body.update_interval_ms = Number(interval);
    if (padding.trim()) body.payload_padding_bytes = Number(padding);
    if (batch.trim()) body.publish_batch_size = Number(batch);

    if (Object.keys(body).length === 0) {
      setMessage({ ok: false, text: "Nothing to change." });
      return;
    }

    setBusy(true);
    try {
      const response = await fetch("/api/admin/config", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify(body),
      });
      if (response.ok) {
        setMessage({ ok: true, text: "Applied. The simulator picks this up on its next tick." });
      } else {
        const detail = await response.json().catch(() => ({}));
        setMessage({
          ok: false,
          text:
            response.status === 401
              ? "Rejected: wrong or missing token."
              : response.status === 503
                ? "Admin controls are disabled on this deployment."
                : `Rejected (${response.status}): ${JSON.stringify(detail.detail ?? detail)}`,
        });
      }
    } catch {
      setMessage({ ok: false, text: "Could not reach the backend." });
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <button className="admin-toggle" onClick={() => setOpen(true)}>
        Fleet controls
      </button>
    );
  }

  const budget =
    config.last_publish_seconds && config.update_interval_ms
      ? (100 * config.last_publish_seconds) / (config.update_interval_ms / 1000)
      : null;

  return (
    <div className="admin">
      <header className="panel-head">
        <h2>Fleet controls</h2>
        <button className="link" onClick={() => setOpen(false)}>
          close
        </button>
      </header>

      {!enabled && (
        <p className="alert">
          Disabled on this deployment: no admin token is configured on the backend.
        </p>
      )}

      <dl className="kv compact">
        <div>
          <dt>Fleet size</dt>
          <dd>{config.fleet_size ?? "--"}</dd>
        </div>
        <div>
          <dt>Interval</dt>
          <dd>{config.update_interval_ms ?? "--"} ms</dd>
        </div>
        <div>
          <dt>Batch</dt>
          <dd>{config.publish_batch_size ?? "--"}</dd>
        </div>
        <div>
          <dt>Padding</dt>
          <dd>{config.payload_padding_bytes ?? 0} B</dd>
        </div>
      </dl>

      {budget !== null && (
        <p className={`budget${budget > 80 ? " hot" : ""}`}>
          Publish uses <strong>{budget.toFixed(0)}%</strong> of its interval
          {config.overruns ? ` - ${config.overruns} overruns so far` : ""}
        </p>
      )}

      <label className="field">
        <span>Admin token</span>
        <input
          type="password"
          value={token}
          onChange={(event) => setToken(event.target.value)}
          placeholder="BACKEND_ADMIN_TOKEN"
        />
      </label>

      <div className="field-grid">
        <label className="field">
          <span>Fleet size</span>
          <input
            type="number"
            min={0}
            max={20000}
            value={fleetSize}
            onChange={(event) => setFleetSize(event.target.value)}
            placeholder={String(config.fleet_size ?? "")}
          />
        </label>
        <label className="field">
          <span>Interval (ms)</span>
          <input
            type="number"
            min={20}
            max={60000}
            value={interval}
            onChange={(event) => setIntervalMs(event.target.value)}
            placeholder={String(config.update_interval_ms ?? "")}
          />
        </label>
        <label className="field">
          <span>Payload padding (B)</span>
          <input
            type="number"
            min={0}
            max={64000}
            value={padding}
            onChange={(event) => setPadding(event.target.value)}
            placeholder={String(config.payload_padding_bytes ?? 0)}
          />
        </label>
        <label className="field">
          <span>Robots per message</span>
          <input
            type="number"
            min={1}
            max={1000}
            value={batch}
            onChange={(event) => setBatch(event.target.value)}
            placeholder={String(config.publish_batch_size ?? 1)}
          />
        </label>
      </div>

      <div className="presets">
        <span className="muted">Presets</span>
        {[
          { label: "8", size: "8", interval: "1000", batch: "1" },
          { label: "250", size: "250", interval: "1000", batch: "1" },
          { label: "800", size: "800", interval: "500", batch: "50" },
          { label: "2000", size: "2000", interval: "500", batch: "100" },
          { label: "5000", size: "5000", interval: "1000", batch: "100" },
        ].map((preset) => (
          <button
            key={preset.label}
            onClick={() => {
              setFleetSize(preset.size);
              setIntervalMs(preset.interval);
              setBatch(preset.batch);
            }}
          >
            {preset.label}
          </button>
        ))}
      </div>

      <button className="primary" onClick={apply} disabled={busy || !enabled}>
        {busy ? "Applying..." : "Apply"}
      </button>

      {message && <p className={message.ok ? "ok" : "alert"}>{message.text}</p>}
    </div>
  );
}
