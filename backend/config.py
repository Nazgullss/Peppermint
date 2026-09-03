"""Backend configuration. Every knob is settable from the environment."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class BackendSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BACKEND_", env_file=".env", extra="ignore")

    # --- where the robots publish ------------------------------------------------
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    topic_prefix: str = "fleet"

    # --- broadcast to dashboards --------------------------------------------------
    # How often we push a frame to the browser. Deliberately decoupled from how often
    # robots publish: robots can report at 20 Hz and dashboards still get 5 frames a
    # second, so render cost does not follow ingest rate.
    broadcast_hz: float = 5.0

    # A full snapshot every N ticks lets a client that missed deltas resynchronise
    # without asking, and bounds how long a dropped frame can matter.
    snapshot_every_n_ticks: int = 50

    # --- backpressure ---------------------------------------------------------------
    # Frames held per dashboard client. 1 means "only the newest frame matters": a slow
    # client loses stale frames instead of building a backlog that would stall everyone.
    client_queue_size: int = 1

    # --- liveness -------------------------------------------------------------------
    # A robot we have not heard from for this long is flagged stale. This is how a robot
    # dying mid-task becomes visible rather than freezing on the map forever.
    stale_after_seconds: float = 10.0

    # --- history (the optional stretch goal) ------------------------------------------
    history_enabled: bool = True
    history_db_path: str = "data/history.db"
    history_flush_seconds: float = 2.0
    history_retention_minutes: int = 60

    # --- admin ----------------------------------------------------------------------
    # Bearer token guarding the live knob controls. Empty disables the endpoints
    # entirely, which is safer than shipping a default password.
    admin_token: str = ""

    # Browser origins allowed to talk to us. In the deployed setup the dashboard is
    # served from the same origin, so this stays empty there.
    cors_origins: str = "http://localhost:5173"

    @property
    def broadcast_interval(self) -> float:
        return 1.0 / max(self.broadcast_hz, 0.1)

    @property
    def telemetry_topic(self) -> str:
        return f"{self.topic_prefix}/+/telemetry"

    @property
    def batch_topic(self) -> str:
        return f"{self.topic_prefix}/_batch/telemetry"

    @property
    def lifecycle_topic(self) -> str:
        return f"{self.topic_prefix}/+/lifecycle"

    @property
    def control_topic(self) -> str:
        return f"{self.topic_prefix}/control"

    @property
    def sim_status_topic(self) -> str:
        return f"{self.topic_prefix}/_sim/status"

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = BackendSettings()
