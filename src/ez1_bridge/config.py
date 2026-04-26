"""Runtime configuration loaded from environment variables.

All settings are sourced from environment variables prefixed with
``EZ1_BRIDGE_``, optionally backed by a ``.env`` file in the working
directory. A typed, frozen :class:`Settings` instance is loaded once at
startup and passed to the rest of the application via dependency
injection — there are no module-level singletons.

See ``.env.example`` for the documented variable set.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
LogFormat = Literal["json", "text", "auto"]

# Type aliases for IPv4-or-name + TCP-port-bounded ints.
Port = Annotated[int, Field(ge=1, le=65535)]
PositivePort = Annotated[int, Field(ge=1, le=65535)]


class Settings(BaseSettings):
    """Frozen, env-driven configuration for the bridge service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="EZ1_BRIDGE_",
        extra="forbid",
        frozen=True,
        case_sensitive=False,
    )

    # --- EZ1 inverter -----------------------------------------------------
    ez1_host: Annotated[str, Field(min_length=1)]
    ez1_port: Port = 8050

    # --- Polling ----------------------------------------------------------
    poll_interval: Annotated[int, Field(ge=1, le=3600)] = 20
    request_timeout: Annotated[int, Field(ge=1, le=60)] = 5

    # --- Commands ---------------------------------------------------------
    setmaxpower_verify: bool = True
    """Re-read getMaxPower after a setMaxPower write to confirm the device
    accepted the value. Set to False for fire-and-forget on latency-sensitive
    automations -- a verify mismatch is then silent."""

    # --- MQTT broker ------------------------------------------------------
    mqtt_host: Annotated[str, Field(min_length=1)]
    mqtt_port: Port = 1883
    mqtt_user: SecretStr | None = None
    mqtt_password: SecretStr | None = None
    mqtt_base_topic: Annotated[str, Field(min_length=1, pattern=r"^[^/#+\s]+$")] = "ez1"
    mqtt_discovery_prefix: Annotated[str, Field(min_length=1, pattern=r"^[^/#+\s]+$")] = (
        "homeassistant"
    )

    # --- Prometheus -------------------------------------------------------
    metrics_port: PositivePort = 9100

    # --- Logging ----------------------------------------------------------
    log_level: LogLevel = "INFO"
    log_format: LogFormat = "auto"
