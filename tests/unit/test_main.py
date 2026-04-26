"""Tests for :mod:`ez1_bridge.main` — the probe CLI and dispatch shim."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

from ez1_bridge.main import _probe, cli_entrypoint

_HOST = "192.168.3.24"
_PORT = 8050
_BASE = f"http://{_HOST}:{_PORT}"


def _arm_all_success(api_response: Callable[[str], dict[str, Any]]) -> None:
    """Configure respx to answer every read endpoint with a verified payload."""
    respx.get(f"{_BASE}/getDeviceInfo").respond(json=api_response("get_device_info"))
    respx.get(f"{_BASE}/getOutputData").respond(json=api_response("get_output_data"))
    respx.get(f"{_BASE}/getMaxPower").respond(json=api_response("get_max_power"))
    respx.get(f"{_BASE}/getAlarm").respond(json=api_response("get_alarm"))
    respx.get(f"{_BASE}/getOnOff").respond(json=api_response("get_on_off"))


# --- _probe ------------------------------------------------------------


@respx.mock
async def test_probe_returns_zero_when_all_endpoints_succeed(
    api_response: Callable[[str], dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _arm_all_success(api_response)

    exit_code = await _probe(host=_HOST, port=_PORT, json_output=False)

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "EZ1 probe" in captured.out
    assert "[OK  ]" in captured.out
    assert "getDeviceInfo" in captured.out
    assert "getOnOff" in captured.out


@respx.mock
async def test_probe_returns_one_when_an_endpoint_fails_with_message(
    api_response: Callable[[str], dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _arm_all_success(api_response)
    failing = api_response("get_alarm").copy()
    failing["message"] = "FAILED"
    respx.get(f"{_BASE}/getAlarm").respond(json=failing)

    exit_code = await _probe(host=_HOST, port=_PORT, json_output=False)

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "message='FAILED'" in out


@respx.mock
async def test_probe_returns_one_when_endpoint_raises(
    api_response: Callable[[str], dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _arm_all_success(api_response)
    respx.get(f"{_BASE}/getOutputData").mock(
        side_effect=httpx.ConnectError("Connection refused"),
    )

    exit_code = await _probe(host=_HOST, port=_PORT, json_output=False)

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "ConnectError" in out


@respx.mock
async def test_probe_emits_json_when_requested(
    api_response: Callable[[str], dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _arm_all_success(api_response)

    exit_code = await _probe(host=_HOST, port=_PORT, json_output=True)

    assert exit_code == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["host"] == _HOST
    assert parsed["port"] == _PORT
    assert len(parsed["results"]) == 5
    assert all(r["ok"] is True for r in parsed["results"])
    endpoint_names = [r["endpoint"] for r in parsed["results"]]
    assert endpoint_names == [
        "getDeviceInfo",
        "getOutputData",
        "getMaxPower",
        "getAlarm",
        "getOnOff",
    ]


@respx.mock
async def test_probe_does_not_call_write_endpoints(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    """`probe` is read-only by design — guard rail against accidental destructive refactors."""
    _arm_all_success(api_response)
    write_set_max = respx.get(f"{_BASE}/setMaxPower").respond(json={"unused": True})
    write_set_on_off = respx.get(f"{_BASE}/setOnOff").respond(json={"unused": True})

    await _probe(host=_HOST, port=_PORT, json_output=False)

    assert write_set_max.call_count == 0
    assert write_set_on_off.call_count == 0


# --- cli_entrypoint dispatch ------------------------------------------


@respx.mock
def test_cli_entrypoint_dispatches_probe(
    api_response: Callable[[str], dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _arm_all_success(api_response)

    exit_code = cli_entrypoint(["probe", "--host", _HOST])

    assert exit_code == 0
    assert "EZ1 probe" in capsys.readouterr().out


@respx.mock
def test_cli_entrypoint_propagates_probe_failure(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    _arm_all_success(api_response)
    failing = api_response("get_max_power").copy()
    failing["message"] = "FAILED"
    respx.get(f"{_BASE}/getMaxPower").respond(json=failing)

    exit_code = cli_entrypoint(["probe", "--host", _HOST])

    assert exit_code == 1


@respx.mock
def test_cli_entrypoint_probe_json_flag(
    api_response: Callable[[str], dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _arm_all_success(api_response)

    exit_code = cli_entrypoint(["probe", "--host", _HOST, "--json"])

    assert exit_code == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["results"][0]["endpoint"] == "getDeviceInfo"


def test_cli_entrypoint_probe_requires_host() -> None:
    with pytest.raises(SystemExit):
        cli_entrypoint(["probe"])


def test_cli_entrypoint_run_raises_until_phase_6() -> None:
    with pytest.raises(NotImplementedError, match="Phase 6"):
        cli_entrypoint(["run"])


def test_cli_entrypoint_no_command_returns_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli_entrypoint([])

    assert exit_code == 2
    # argparse prints help to stderr per our wiring.
    assert "usage" in capsys.readouterr().err.lower()


def test_cli_entrypoint_version_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli_entrypoint(["--version"])
    assert excinfo.value.code == 0
    assert "ez1-bridge" in capsys.readouterr().out
