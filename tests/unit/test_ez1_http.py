"""Tests for :mod:`ez1_bridge.adapters.ez1_http`.

Uses ``respx`` to mock the underlying ``httpx`` transport. The five
verified payload fixtures from ``tests/fixtures/api_responses/`` drive
the read-endpoint happy paths; retry classification and write-endpoint
query-string handling get dedicated tests.

Backoff sleeps are monkeypatched to zero in retry tests so the suite
stays fast — the *order* of attempts and the *number of calls* are
what we are checking, not the actual wall-clock waits.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

from ez1_bridge.adapters.ez1_http import (
    EZ1Client,
    _backoff_seconds,
    _is_transient,
)

# --- _is_transient classifier ------------------------------------------


def test_is_transient_timeout() -> None:
    assert _is_transient(httpx.TimeoutException("timeout")) is True


def test_is_transient_5xx() -> None:
    response = httpx.Response(503, request=httpx.Request("GET", "http://x"))
    exc = httpx.HTTPStatusError("503", request=response.request, response=response)
    assert _is_transient(exc) is True


def test_is_transient_4xx_is_not() -> None:
    response = httpx.Response(404, request=httpx.Request("GET", "http://x"))
    exc = httpx.HTTPStatusError("404", request=response.request, response=response)
    assert _is_transient(exc) is False


def test_is_transient_connect_error_is_not() -> None:
    assert _is_transient(httpx.ConnectError("refused")) is False


def test_is_transient_unrelated_exception() -> None:
    assert _is_transient(RuntimeError("boom")) is False


# --- _backoff_seconds --------------------------------------------------


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [(1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0), (5, 16.0)],
)
def test_backoff_seconds_doubles_per_attempt(attempt: int, expected: float) -> None:
    assert _backoff_seconds(attempt) == expected


def test_backoff_seconds_caps_at_300() -> None:
    # 2^9 = 512 > 300 — should cap.
    assert _backoff_seconds(10) == 300.0
    assert _backoff_seconds(20) == 300.0


# --- helpers -----------------------------------------------------------


@pytest.fixture
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace :func:`_backoff_seconds` with a zero-wait stub."""
    monkeypatch.setattr(
        "ez1_bridge.adapters.ez1_http._backoff_seconds",
        lambda _attempt: 0.0,
    )


_HOST = "192.168.3.24"
_BASE = f"http://{_HOST}:8050"


# --- Read endpoints — happy paths --------------------------------------


@respx.mock
async def test_get_device_info_returns_envelope(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    respx.get(f"{_BASE}/getDeviceInfo").respond(json=api_response("get_device_info"))
    async with EZ1Client(_HOST) as client:
        envelope = await client.get_device_info()
    assert envelope["message"] == "SUCCESS"
    assert envelope["data"]["deviceId"] == "E17010000783"
    assert envelope["data"]["maxPower"] == "800"


@respx.mock
async def test_get_output_data_returns_envelope(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    respx.get(f"{_BASE}/getOutputData").respond(json=api_response("get_output_data"))
    async with EZ1Client(_HOST) as client:
        envelope = await client.get_output_data()
    assert envelope["data"]["p1"] == 139
    assert envelope["data"]["te2"] == 111.24305


@respx.mock
async def test_get_max_power_returns_envelope(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    respx.get(f"{_BASE}/getMaxPower").respond(json=api_response("get_max_power"))
    async with EZ1Client(_HOST) as client:
        envelope = await client.get_max_power()
    assert envelope["data"]["maxPower"] == "800"


@respx.mock
async def test_get_alarm_returns_envelope(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    respx.get(f"{_BASE}/getAlarm").respond(json=api_response("get_alarm"))
    async with EZ1Client(_HOST) as client:
        envelope = await client.get_alarm()
    assert envelope["data"]["og"] == "0"


@respx.mock
async def test_get_on_off_returns_envelope(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    respx.get(f"{_BASE}/getOnOff").respond(json=api_response("get_on_off"))
    async with EZ1Client(_HOST) as client:
        envelope = await client.get_on_off()
    assert envelope["data"]["status"] == "0"


# --- Write endpoints — query string assertions -------------------------


@respx.mock
async def test_set_max_power_sends_p_param() -> None:
    route = respx.get(f"{_BASE}/setMaxPower").respond(
        json={"data": {"maxPower": "600"}, "message": "SUCCESS", "deviceId": "E17010000783"},
    )
    async with EZ1Client(_HOST) as client:
        envelope = await client.set_max_power(600)
    assert envelope["message"] == "SUCCESS"
    assert route.call_count == 1
    request = route.calls.last.request
    assert request.url.params["p"] == "600"


@respx.mock
async def test_set_on_off_on_sends_status_zero() -> None:
    route = respx.get(f"{_BASE}/setOnOff").respond(
        json={"data": {"status": "0"}, "message": "SUCCESS", "deviceId": "E17010000783"},
    )
    async with EZ1Client(_HOST) as client:
        await client.set_on_off(on=True)
    assert route.calls.last.request.url.params["status"] == "0"


@respx.mock
async def test_set_on_off_off_sends_status_one() -> None:
    route = respx.get(f"{_BASE}/setOnOff").respond(
        json={"data": {"status": "1"}, "message": "SUCCESS", "deviceId": "E17010000783"},
    )
    async with EZ1Client(_HOST) as client:
        await client.set_on_off(on=False)
    assert route.calls.last.request.url.params["status"] == "1"


# --- Retry behaviour ---------------------------------------------------


@respx.mock
async def test_timeout_retries_then_succeeds(
    api_response: Callable[[str], dict[str, Any]],
    fast_backoff: None,
) -> None:
    """Timeout is transient → retried until max_attempts is reached."""
    route = respx.get(f"{_BASE}/getOutputData").mock(
        side_effect=[
            httpx.TimeoutException("first attempt timed out"),
            httpx.TimeoutException("second attempt timed out"),
            httpx.Response(200, json=api_response("get_output_data")),
        ],
    )
    async with EZ1Client(_HOST, max_attempts=3) as client:
        envelope = await client.get_output_data()
    assert envelope["message"] == "SUCCESS"
    assert route.call_count == 3


@respx.mock
async def test_timeout_exhausts_attempts(fast_backoff: None) -> None:
    route = respx.get(f"{_BASE}/getOutputData").mock(
        side_effect=httpx.TimeoutException("always timeout"),
    )
    async with EZ1Client(_HOST, max_attempts=3) as client:
        with pytest.raises(httpx.TimeoutException):
            await client.get_output_data()
    assert route.call_count == 3


@respx.mock
async def test_5xx_retries_then_succeeds(
    api_response: Callable[[str], dict[str, Any]],
    fast_backoff: None,
) -> None:
    """5xx is transient (broker hiccup) → retried."""
    route = respx.get(f"{_BASE}/getOutputData").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(502),
            httpx.Response(200, json=api_response("get_output_data")),
        ],
    )
    async with EZ1Client(_HOST, max_attempts=3) as client:
        envelope = await client.get_output_data()
    assert envelope["message"] == "SUCCESS"
    assert route.call_count == 3


@respx.mock
async def test_4xx_fails_fast() -> None:
    """4xx is a programming error — never retried."""
    route = respx.get(f"{_BASE}/getOutputData").respond(404)
    async with EZ1Client(_HOST, max_attempts=5) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_output_data()
    assert route.call_count == 1


@respx.mock
async def test_connect_error_fails_fast() -> None:
    """ConnectError means the device is offline; retrying does not help."""
    route = respx.get(f"{_BASE}/getOutputData").mock(
        side_effect=httpx.ConnectError("Connection refused"),
    )
    async with EZ1Client(_HOST, max_attempts=5) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get_output_data()
    assert route.call_count == 1


# --- Context manager enforcement ---------------------------------------


async def test_call_outside_context_manager_raises() -> None:
    client = EZ1Client(_HOST)
    with pytest.raises(RuntimeError, match="async context manager"):
        await client.get_output_data()


# --- Construction guards -----------------------------------------------


def test_empty_host_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        EZ1Client("")


def test_zero_max_attempts_rejected() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        EZ1Client(_HOST, max_attempts=0)


def test_base_url_is_resolvable() -> None:
    client = EZ1Client("10.0.0.5", port=9999)
    assert client.base_url == "http://10.0.0.5:9999"


# --- Defensive type guards on the response -----------------------------


@respx.mock
async def test_non_object_json_response_rejected() -> None:
    respx.get(f"{_BASE}/getMaxPower").respond(json=["not", "an", "object"])
    async with EZ1Client(_HOST) as client:
        with pytest.raises(TypeError, match="JSON object"):
            await client.get_max_power()
