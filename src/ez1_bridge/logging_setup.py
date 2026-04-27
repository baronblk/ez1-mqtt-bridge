"""structlog configuration for the bridge service.

Two output formats:

* ``json`` -- one JSON object per line, ISO timestamps in UTC, suitable
  for log aggregators (Loki, journald, Splunk).
* ``text`` -- ANSI-coloured ``ConsoleRenderer`` output for human eyes.

The default ``auto`` setting picks ``text`` when stderr is a TTY and
``json`` otherwise. Distroless containers have no TTY, so production
deployments get JSON without an explicit override; developers running
the service in a terminal get coloured text. Detection is on
``sys.stderr`` (not stdout) because logs go there, and ``stdin``/
``stdout`` may be redirected without affecting log readability.

Processor order matters
-----------------------

The chain runs every processor in order on each event dict, then hands
the result to the renderer (which is the *last* processor). Three
constraints determine the order below:

1. ``merge_contextvars`` must run first so context-bound variables are
   visible to every later processor (e.g. correlation IDs in the level).
2. ``add_log_level`` and ``TimeStamper`` add fields the renderer needs;
   they must run before the renderer.
3. ``StackInfoRenderer`` and ``format_exc_info`` populate exception
   fields; they must run before the renderer or the trace gets dropped.

The renderer is always last. JSONRenderer serialises the entire event
dict; ConsoleRenderer formats it for humans.
"""

from __future__ import annotations

import logging
import sys
from typing import Final, Literal

import structlog

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
LogFormat = Literal["json", "text", "auto"]

_LEVEL_TO_INT: Final[dict[LogLevel, int]] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}


def resolve_format(setting: LogFormat) -> Literal["json", "text"]:
    """Resolve ``auto`` against TTY detection on stderr.

    Extracted from :func:`configure_logging` so tests can exercise the
    TTY logic without driving the rest of the configuration chain.
    """
    if setting == "auto":
        return "text" if sys.stderr.isatty() else "json"
    return setting


def configure_logging(*, level: LogLevel, format_: LogFormat) -> None:
    """Configure structlog for the chosen level and format.

    Idempotent: calling this function multiple times replaces the
    existing configuration without leaking state. The first call wins
    for ``cache_logger_on_first_use``; subsequent calls reconfigure but
    already-cached loggers keep their previous wrapper. Production
    calls this once at startup; tests reconfigure per scenario via
    ``cache_logger_on_first_use=False``.
    """
    resolved = resolve_format(format_)

    common_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: structlog.types.Processor
    if resolved == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[*common_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(_LEVEL_TO_INT[level]),
        cache_logger_on_first_use=False,
    )
