"""Application entrypoint and signal handling.

Wires the configuration, logging, MQTT, EZ1 client, metrics server, and the
poll/command/heartbeat coroutines into a single ``asyncio.TaskGroup``.

Implementation lands in Phase 4 (poll loop) and Phase 6 (CLI subcommands).
"""


def cli_entrypoint() -> None:
    """CLI entrypoint stub — replaced in Phase 6."""
    raise NotImplementedError("CLI entrypoint is implemented in Phase 6.")
