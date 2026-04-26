"""Application entrypoint, signal handling, and CLI dispatch.

Currently exposes the read-only :func:`_probe` health check and an
argparse-based :func:`cli_entrypoint`. The full bridge service (poll
loop, command dispatcher, metrics server, signal handling) lands in
Phase 6 under the ``run`` subcommand, which currently raises
:class:`NotImplementedError` to keep the CLI surface visible.
"""

from __future__ import annotations

import argparse
import asyncio
import json as json_lib
import sys
from typing import Any, Final

from ez1_bridge import __version__
from ez1_bridge.adapters.ez1_http import EZ1Client

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
        help="Run the bridge service (implemented in Phase 6).",
    )

    return parser


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
        msg = "`run` is implemented in Phase 6."
        raise NotImplementedError(msg)

    parser.print_help(sys.stderr)
    return 2
