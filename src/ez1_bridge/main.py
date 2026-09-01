"""Application entrypoint, signal handling, and CLI dispatch.

Two operating modes:

* ``probe`` -- a read-only health check that exercises every EZ1
  endpoint once and reports the result. Mirrors the Docker
  ``HEALTHCHECK`` in spirit but talks to the inverter directly.
* ``run`` -- the full bridge service. Starts the ``/metrics`` HTTP
  server, waits (with retry) until the inverter answers
  ``getDeviceInfo``, connects to the broker, and then runs the poll
  loop, the availability heartbeat and the command handler as sibling
  tasks in an :class:`asyncio.TaskGroup`.

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

import httpx
import structlog

from ez1_bridge import __version__
from ez1_bridge.adapters.ez1_http import EZ1Client
from ez1_bridge.adapters.mqtt_publisher import MQTTPublisher
from ez1_bridge.adapters.prom_metrics import MetricsRegistry, metrics_server
from ez1_bridge.application.command_handler import command_loop
from ez1_bridge.application.poll_service import availability_heartbeat, poll_loop
from ez1_bridge.config import Settings
from ez1_bridge.domain.models import DeviceInfo
from ez1_bridge.domain.normalizer import parse_device_info
from ez1_bridge.logging_setup import configure_logging

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
    hardware and as a quick local diagnostic.

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

    Two nested TaskGroups: the outer one owns only the ``/metrics``
    server so it is reachable from the very first second -- including
    while :func:`_resolve_device_info` waits for a powered-down
    inverter. The inner one (in :func:`_run_bridge`) spawns the poll
    loop, availability heartbeat and command handler once the device_id
    is known. The surrounding ``async with`` blocks ensure the EZ1 HTTP
    client and the MQTT connection are torn down cleanly on any exit
    path.
    """
    own_stop_event = stop_event is None
    stop_event = stop_event or asyncio.Event()
    if own_stop_event:
        _install_signal_handlers(stop_event)

    metrics = MetricsRegistry()
    # ``ez1_bridge_up`` stays 0 until the inverter has been resolved; the
    # Docker HEALTHCHECK only probes ``/metrics`` reachability, so a bridge
    # waiting for a dark inverter reports healthy-but-idle, not crash-looping.
    metrics.set_bridge_up(up=False)

    _log.info(
        "bridge_starting",
        ez1=f"{settings.ez1_host}:{settings.ez1_port}",
        mqtt=f"{settings.mqtt_host}:{settings.mqtt_port}",
        metrics=f"{settings.metrics_bind}:{settings.metrics_port}",
        poll_interval=settings.poll_interval,
    )

    # The metrics server is started *before* the inverter is resolved so
    # the container healthcheck and Prometheus see the bridge while it
    # waits for a powered-down EZ1. It is the only task in the outer
    # group; ``stop_event`` is set on every exit path below so it
    # always terminates.
    async with (
        EZ1Client(
            host=settings.ez1_host,
            port=settings.ez1_port,
            timeout=settings.request_timeout,
            metrics=metrics,
        ) as ez1,
        asyncio.TaskGroup() as outer,
    ):
        outer.create_task(
            metrics_server(
                metrics=metrics,
                host=settings.metrics_bind,
                port=settings.metrics_port,
                stop_event=stop_event,
            ),
            name="metrics_server",
        )
        try:
            await _run_bridge(
                settings=settings,
                ez1=ez1,
                metrics=metrics,
                stop_event=stop_event,
            )
        finally:
            stop_event.set()


async def _resolve_device_info(
    ez1: EZ1Client,
    *,
    retry_interval: float,
    stop_event: asyncio.Event,
) -> DeviceInfo | None:
    """Call ``getDeviceInfo`` until it succeeds or ``stop_event`` fires.

    The EZ1-M switches off its WLAN when the panels deliver no power, so
    a (re)start at night must not fail fast -- it would crash-loop until
    sunrise and keep the container permanently ``unhealthy``. Transport
    errors (connect refused / unreachable, timeouts, HTTP 5xx) are logged
    at ``info`` with the next retry and swallowed; anything else -- a
    malformed envelope, a parse error -- is a real bug and propagates.

    Returns ``None`` only if the stop event fired before the inverter
    answered, so the caller can shut down cleanly without ever touching
    MQTT (the LWT topic depends on the device_id).
    """
    attempt = 0
    while not stop_event.is_set():
        attempt += 1
        try:
            info = parse_device_info(await ez1.get_device_info())
        except httpx.TransportError as exc:
            _log.info(
                "ez1_unreachable_at_startup",
                attempt=attempt,
                error=type(exc).__name__,
                retry_in_seconds=retry_interval,
                hint="inverter is probably powered down (no DC input)",
            )
        except httpx.HTTPStatusError as exc:
            _log.warning(
                "ez1_http_error_at_startup",
                attempt=attempt,
                status_code=exc.response.status_code,
                retry_in_seconds=retry_interval,
            )
        else:
            _log.info(
                "ez1_device_resolved",
                device_id=info.device_id,
                firmware=info.firmware_version,
                attempt=attempt,
            )
            return info

        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(retry_interval):
                await stop_event.wait()
    return None


async def _run_bridge(
    *,
    settings: Settings,
    ez1: EZ1Client,
    metrics: MetricsRegistry,
    stop_event: asyncio.Event,
) -> None:
    """Resolve the inverter, connect to MQTT, and run the worker tasks.

    Split out of :func:`run_service` so the metrics server can outlive
    the whole sequence in the outer TaskGroup. Returns when
    ``stop_event`` fires (or immediately if it fires while still waiting
    for the inverter).
    """
    # Resolve device_id up front -- the LWT topic baked into the MQTT
    # CONNECT depends on it, and there is no clean way to update it later.
    device_info = await _resolve_device_info(
        ez1,
        retry_interval=settings.startup_retry_interval,
        stop_event=stop_event,
    )
    if device_info is None:
        _log.info("bridge_stopped", reason="stopped_before_ez1_resolved")
        return

    async with MQTTPublisher(
        host=settings.mqtt_host,
        port=settings.mqtt_port,
        username=(
            settings.mqtt_user.get_secret_value() if settings.mqtt_user is not None else None
        ),
        password=settings.mqtt_password,
        base_topic=settings.mqtt_base_topic,
        device_id=device_info.device_id,
        on_reconnect=metrics.increment_mqtt_reconnect,
        metrics=metrics,
    ) as publisher:
        metrics.set_bridge_up(up=True)
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(
                    poll_loop(
                        ez1=ez1,
                        publisher=publisher,
                        settings=settings,
                        stop_event=stop_event,
                        metrics=metrics,
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
            metrics.set_bridge_up(up=False)
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
        configure_logging(level=settings.log_level, format_=settings.log_format)
        asyncio.run(run_service(settings))
        return 0

    parser.print_help(sys.stderr)
    return 2
