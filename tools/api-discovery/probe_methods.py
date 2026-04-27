"""Phase 2 — header / method probes against known EZ1 endpoints.

Phase 1 (``probe_endpoints.py``) confirmed that the inverter exposes
exactly the documented seven endpoints; no hidden read paths surfaced
in the wordlist. This script asks a different question on the same
endpoints: does the server expose alternative response shapes via
content negotiation, or list other supported methods via OPTIONS?

Same safety rules as Phase 1: read-only methods only (``GET``, ``HEAD``,
``OPTIONS``), 1 s rate limit between probes, 5 s per-probe timeout.

# Usage

    uv run python tools/api-discovery/probe_methods.py \\
        --host 192.168.3.24 --port 8050

Writes ``methods-<UTC-timestamp>.json`` next to this script.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

DEFAULT_HOST = "192.168.3.24"
DEFAULT_PORT = 8050
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_RATE_LIMIT_SECONDS = 1.0

NON_JSON_BODY_CAPTURE_LIMIT = 500
JSON_BODY_FULL_CAPTURE_LIMIT = 8 * 1024


# Each tuple: (path, method, request-headers, why-we-care).
PROBES: tuple[tuple[str, str, Mapping[str, str], str], ...] = (
    # OPTIONS: surface allowed methods via the Allow header.
    ("/getDeviceInfo", "OPTIONS", {}, "list allowed methods on a known read endpoint"),
    ("/setMaxPower", "OPTIONS", {}, "list allowed methods on a known set endpoint"),
    ("/", "OPTIONS", {}, "list allowed methods on the root path"),
    # HEAD: response headers without body — sometimes reveals additional
    # server hints that the GET response is silent about.
    ("/getDeviceInfo", "HEAD", {}, "headers-only view of the canonical envelope"),
    # Content negotiation: does the server emit XML or another shape on
    # request? Some embedded HTTP frameworks ship XML mirrors of JSON
    # responses for legacy clients.
    (
        "/getDeviceInfo",
        "GET",
        {"Accept": "application/xml"},
        "alternative response format via content negotiation",
    ),
    (
        "/getDeviceInfo",
        "GET",
        {"Accept": "text/plain"},
        "plaintext response variant",
    ),
    # Debug-style request headers — vendor firmwares occasionally honour
    # an X-Debug or similar header to widen the response.
    (
        "/getDeviceInfo",
        "GET",
        {"X-Debug": "1"},
        "X-Debug widening (long shot, common embedded-firmware pattern)",
    ),
    (
        "/getOutputData",
        "GET",
        {"X-Debug": "1"},
        "same on the live-data endpoint",
    ),
)


@dataclass
class MethodProbeResult:
    path: str
    method: str
    request_headers: dict[str, str]
    rationale: str
    outcome: str  # "ok" | "client_error" | "server_error"
    #             | "unreachable" | "inconclusive"
    status_code: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    body_capture: str | None = None
    body_is_json: bool = False
    json_payload: Any = None
    roundtrip_seconds: float | None = None
    error_class: str | None = None
    error_detail: str | None = None
    notes: list[str] = field(default_factory=list)


def _capture_body(response: httpx.Response) -> tuple[str | None, bool, Any]:
    content_type = (response.headers.get("content-type") or "").lower()
    text = response.text or ""
    if "application/json" in content_type or text.lstrip().startswith(("{", "[")):
        try:
            payload = response.json()
            return text[:JSON_BODY_FULL_CAPTURE_LIMIT], True, payload
        except (ValueError, json.JSONDecodeError):
            return text[:NON_JSON_BODY_CAPTURE_LIMIT], False, None
    return text[:NON_JSON_BODY_CAPTURE_LIMIT], False, None


def run_method_probes(
    *,
    host: str,
    port: int,
    timeout_seconds: float,
    rate_limit_seconds: float,
) -> list[MethodProbeResult]:
    base_url = f"http://{host}:{port}"
    results: list[MethodProbeResult] = []
    print(f"[methods] target {base_url}, {len(PROBES)} probes", file=sys.stderr)

    with httpx.Client(
        base_url=base_url,
        timeout=timeout_seconds,
        follow_redirects=False,
    ) as client:
        for index, (path, method, headers, rationale) in enumerate(PROBES, start=1):
            started = time.monotonic()
            try:
                response = client.request(method, path, headers=dict(headers))
            except httpx.TimeoutException as exc:
                results.append(
                    MethodProbeResult(
                        path=path,
                        method=method,
                        request_headers=dict(headers),
                        rationale=rationale,
                        outcome="unreachable",
                        roundtrip_seconds=round(time.monotonic() - started, 3),
                        error_class=type(exc).__name__,
                        error_detail=str(exc) or "request timed out",
                    )
                )
                print(
                    f"[methods] {index:2d}/{len(PROBES)} {method:7s} {path:18s}"
                    f" → unreachable ({type(exc).__name__})",
                    file=sys.stderr,
                )
            except httpx.HTTPError as exc:
                results.append(
                    MethodProbeResult(
                        path=path,
                        method=method,
                        request_headers=dict(headers),
                        rationale=rationale,
                        outcome="unreachable",
                        roundtrip_seconds=round(time.monotonic() - started, 3),
                        error_class=type(exc).__name__,
                        error_detail=str(exc) or "http error",
                    )
                )
                print(
                    f"[methods] {index:2d}/{len(PROBES)} {method:7s} {path:18s}"
                    f" → unreachable ({type(exc).__name__})",
                    file=sys.stderr,
                )
            else:
                elapsed = time.monotonic() - started
                body_capture, body_is_json, json_payload = _capture_body(response)
                if 200 <= response.status_code < 400:
                    outcome = "ok"
                elif 400 <= response.status_code < 500:
                    outcome = "client_error"
                else:
                    outcome = "server_error"
                results.append(
                    MethodProbeResult(
                        path=path,
                        method=method,
                        request_headers=dict(headers),
                        rationale=rationale,
                        outcome=outcome,
                        status_code=response.status_code,
                        response_headers=dict(response.headers),
                        body_capture=body_capture,
                        body_is_json=body_is_json,
                        json_payload=json_payload,
                        roundtrip_seconds=round(elapsed, 3),
                    )
                )
                print(
                    f"[methods] {index:2d}/{len(PROBES)} {method:7s} {path:18s}"
                    f" → {outcome:14s} status={response.status_code}"
                    f" rtt={round(elapsed, 3)}s",
                    file=sys.stderr,
                )

            if index < len(PROBES):
                time.sleep(rate_limit_seconds)

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Probe known EZ1 endpoints with alternative methods and headers."
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--rate-limit-seconds", type=float, default=DEFAULT_RATE_LIMIT_SECONDS)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent)
    args = parser.parse_args(argv)

    started_at = datetime.now(tz=UTC)
    results = run_method_probes(
        host=args.host,
        port=args.port,
        timeout_seconds=args.timeout_seconds,
        rate_limit_seconds=args.rate_limit_seconds,
    )
    finished_at = datetime.now(tz=UTC)

    payload = {
        "metadata": {
            "started_at_utc": started_at.isoformat(),
            "finished_at_utc": finished_at.isoformat(),
            "host": args.host,
            "port": args.port,
            "timeout_seconds": args.timeout_seconds,
            "rate_limit_seconds": args.rate_limit_seconds,
            "total_probes": len(results),
        },
        "results": [asdict(r) for r in results],
    }

    timestamp = started_at.isoformat().replace(":", "-").split(".")[0]
    output_path = args.output_dir / f"methods-{timestamp}Z.json"
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"[methods] wrote {output_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
