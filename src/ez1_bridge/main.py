"""Application entrypoint, signal handling, and CLI dispatch.

Two operating modes:

* ``probe`` -- the read-only health check from Phase 2.
* ``run`` -- the full bridge service (Phase 4 onwards): connects to
  the broker, brings up an :class:`MQTTPublisher` and an
  :class:`EZ1Client`, and starts an :class:`asyncio.TaskGroup` with
  the poll loop and availability heartbeat.

Phase 5 will add the command-handler task to the same TaskGroup;
Phase 6 the metrics server. The TaskGroup skeleton in
:func:`run_service` is the canonical orchestration point so those
later phases only add ``tg.create_task(...)`` lines, not refactor.

Signal handling routes ``SIGINT`` / ``SIGTERM`` to a single
:class:`asyncio.Event` that every coroutine in the TaskGroup observes.
On shutdown, ``availability=offline`` is published explicitly before
the MQTT connection closes -- a graceful disconnect does NOT trigger
the broker's LWT, so without this the availability badge in Home
Assistant would briefly show stale ``online``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json as json_lib
import signal
import sys
from typing import Any, Final

import structlog

from ez1_bridge import __version__
from ez1_bridge.adapters.ez1_http import EZ1Client
from ez1_bridge.adapters.mqtt_publisher import MQTTPublisher
from ez1_bridge.application.command_handler import command_loop
from ez1_bridge.application.poll_service import availability_heartbeat, poll_loop
from ez1_bridge.config import Settings
from ez1_bridge.domain.normalizer import parse_device_info

_log = structlog.get_logger(__name__)

#: Tuple of (wire-name, EZ1Client method name) for the five read endpoints.
#: ``probe`` is read-only by design — write endpoints are not listed here so
#: an accidental refactor cannot turn the health check destructive.
_READ_ENDPOINTS: Final[tuple[tuple[str, str], ...]] = (
    ("getDeviceInfo", "get_device_info"),
    ("getOutputData", "get_output_data"),
    ("getMaxPower", "get_max_power"),
    ("getAlarm", "get_alarm"),
    ("getOnOff", "get_on_off"),
)


async def _probe(*, host: str, port: int, json_output: bool) -> int:
    """Run a read-only health check against the five EZ1 read endpoints.

    Returns ``0`` if every endpoint responds with ``message == "SUCCESS"``,
    ``1`` otherwise. Designed for use as a CI smoke test against real
    hardware (Phase 7) and as a quick local diagnostic.

    Never issues a write call. Adding a write endpoint to this routine
    would require a new fixture name and changes to the CLI surface --
    keep it that way.
    """
    results: list[dict[str, Any]] = []

    async with EZ1Client(host=host, port=port) as client:
        for endpoint_name, method_name in _READ_ENDPOINTS:
            method = getattr(client, method_name)
            try:
                envelope = await method()
            except Exception as exc:
                results.append(
                    {
                        "endpoint": endpoint_name,
                        "ok": False,
                        "detail": f"{type(exc).__name__}: {exc}",
                    },
                )
                continue

            ok = envelope.get("message") == "SUCCESS"
            detail = "OK" if ok else f"message={envelope.get('message')!r}"
            results.append({"endpoint": endpoint_name, "ok": ok, "detail": detail})

    if json_output:
        sys.stdout.write(
            json_lib.dumps({"host": host, "port": port, "results": results}) + "\n",
        )
    else:
        sys.stdout.write(f"EZ1 probe -> {host}:{port}\n")
        for r in results:
            mark = "OK  " if r["ok"] else "FAIL"
            sys.stdout.write(f"  [{mark}] {r['endpoint']:<15} {r['detail']}\n")

    return 0 if all(r["ok"] for r in results) else 1


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser. Extracted for testability."""
    parser = argparse.ArgumentParser(
        prog="ez1-bridge",
        description="MQTT bridge for the APsystems EZ1-M micro inverter.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"ez1-bridge {__version__}",
    )
    sub = parser.add_subparsers(dest="command")

    probe = sub.add_parser(
        "probe",
        help="Read-only health check of the EZ1 local API.",
        description=(
            "Hit each of the five EZ1 read endpoints and report SUCCESS or "
            "FAILED. Exit code 0 if all endpoints respond cleanly, 1 "
            "otherwise. No write calls are issued."
        ),
    )
    probe.add_argument("--host", required=True, help="EZ1 host or IP address")
    probe.add_argument("--port", type=int, default=8050, help="TCP port (default: 8050)")
    probe.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit a JSON object instead of human-readable output",
    )

    sub.add_parser(
        "run",
        help="Run the bridge service (poll EZ1, publish to MQTT, heartbeat).",
    )

    return parser


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    """Wire SIGINT and SIGTERM to set ``stop_event`` exactly once each.

    POSIX-only -- ``loop.add_signal_handler`` is not implemented on
    Windows. The bridge ships in a Linux container so this is fine; the
    function is a no-op on platforms where the call would raise (tests
    can drive ``stop_event`` directly).
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)


async def run_service(
    settings: Settings,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run the bridge service until ``stop_event`` is set or SIGTERM arrives.

    If ``stop_event`` is ``None``, signal handlers are installed and a
    fresh event is created. Tests pass an event explicitly so the
    function can be exercised without touching process-wide signals.

    Phase 4 starts two tasks (poll loop + availability heartbeat).
    Phase 5 adds the command handler; Phase 6 the metrics server. The
    TaskGroup is the single orchestration point, and the surrounding
    ``async with`` blocks ensure the EZ1 HTTP client and the MQTT
    connection are torn down cleanly on any exit path.
    """
    own_stop_event = stop_event is None
    stop_event = stop_event or asyncio.Event()
    if own_stop_event:
        _install_signal_handlers(stop_event)

    _log.info(
        "bridge_starting",
        ez1=f"{settings.ez1_host}:{settings.ez1_port}",
        mqtt=f"{settings.mqtt_host}:{settings.mqtt_port}",
        poll_interval=settings.poll_interval,
    )

    async with EZ1Client(
        host=settings.ez1_host,
        port=settings.ez1_port,
        timeout=settings.request_timeout,
    ) as ez1:
        # Resolve device_id up front -- the LWT topic baked into the MQTT
        # CONNECT depends on it, and there is no clean way to update it
        # later. If the inverter is offline at startup the bridge fails
        # fast; container restart policies (Docker / systemd) handle it.
        device_info = parse_device_info(await ez1.get_device_info())
        _log.info(
            "ez1_device_resolved",
            device_id=device_info.device_id,
            firmware=device_info.firmware_version,
        )

        async with MQTTPublisher(
            host=settings.mqtt_host,
            port=settings.mqtt_port,
            username=(
                settings.mqtt_user.get_secret_value() if settings.mqtt_user is not None else None
            ),
            password=settings.mqtt_password,
            base_topic=settings.mqtt_base_topic,
            device_id=device_info.device_id,
        ) as publisher:
            try:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(
                        poll_loop(
                            ez1=ez1,
                            publisher=publisher,
                            settings=settings,
                            stop_event=stop_event,
                        ),
                        name="poll_loop",
                    )
                    tg.create_task(
                        availability_heartbeat(
                            publisher=publisher,
                            stop_event=stop_event,
                        ),
                        name="availability_heartbeat",
                    )
                    # command_loop subscribes and blocks on async-for; it
                    # cannot poll stop_event while waiting for a message,
                    # so we cancel it explicitly once stop_event fires.
                    # See command_handler.py "Cancellation" docstring.
                    command_task = tg.create_task(
                        command_loop(
                            client=publisher.client,
                            ez1=ez1,
                            publisher=publisher,
                            device_info=device_info,
                            settings=settings,
                            stop_event=stop_event,
                        ),
                        name="command_loop",
                    )

                    await stop_event.wait()
                    command_task.cancel()
            finally:
                with contextlib.suppress(Exception):
                    await publisher.publish_availability(online=False)
                _log.info("bridge_stopped")


def cli_entrypoint(argv: list[str] | None = None) -> int:
    """Top-level CLI dispatch — invoked by ``python -m ez1_bridge``.

    Returns the process exit code. The :mod:`ez1_bridge.__main__` shim
    wraps this in :func:`sys.exit`.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "probe":
        return asyncio.run(
            _probe(host=args.host, port=args.port, json_output=args.json_output),
        )
    if args.command == "run":
        settings = Settings()  # type: ignore[call-arg]  # loaded from env / .env
        asyncio.run(run_service(settings))
        return 0

    parser.print_help(sys.stderr)
    return 2
