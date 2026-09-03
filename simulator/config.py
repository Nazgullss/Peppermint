"""Simulator configuration.

Every knob here is settable from the environment with no code change. The three the
challenge calls out -- fleet size, update interval and payload size -- are additionally
settable at runtime on the deployed instance over the MQTT control topic, which the
backend publishes to after authenticating the operator. See README.md.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class SimulatorSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SIM_", env_file=".env", extra="ignore")

    # --- the three knobs the challenge asks to be turned up ---------------------------
    fleet_size: int = 8
    update_interval_ms: int = 1000
    payload_padding_bytes: int = 0

    # --- transport --------------------------------------------------------------------
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_keepalive: int = 30
    topic_prefix: str = "fleet"

    # Robots per MQTT message. 1 means one message per robot per interval, which is what a
    # real fleet looks like on the wire. Larger values model an edge gateway aggregating
    # for a group of robots and are how we get past the per-message publish cost when
    # load testing; see FINDINGS.md for the measured difference.
    publish_batch_size: int = 1

    # --- behaviour --------------------------------------------------------------------
    seed: int | None = None
    # Publishing thousands of messages takes time; if a tick overruns its interval we log
    # it rather than letting the loop silently drift.
    warn_on_overrun: bool = True

    @property
    def interval_seconds(self) -> float:
        return max(self.update_interval_ms, 10) / 1000.0

    @property
    def telemetry_topic_pattern(self) -> str:
        return f"{self.topic_prefix}/+/telemetry"

    @property
    def batch_topic(self) -> str:
        return f"{self.topic_prefix}/_batch/telemetry"

    @property
    def control_topic(self) -> str:
        return f"{self.topic_prefix}/control"

    @property
    def sim_status_topic(self) -> str:
        return f"{self.topic_prefix}/_sim/status"


settings = SimulatorSettings()
