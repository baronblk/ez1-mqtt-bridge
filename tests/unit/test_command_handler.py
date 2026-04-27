"""Unit tests for :mod:`ez1_bridge.application.command_handler`.

Hand-rolled async iterator helps simulate aiomqtt.Client.messages so the
``command_loop`` can be driven deterministically without a real broker.
The real-broker round-trip lives in
``tests/integration/test_command_e2e.py``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import aiomqtt
import pytest
from pydantic import ValidationError

from ez1_bridge.application.command_handler import (
    _KNOWN_COMMANDS,
    CommandResult,
    _decode_payload,
    _dispatch,
    _emit_dispatch_failure,
    command_loop,
    handle_max_power,
    handle_on_off,
    parse_command_topic,
    parse_max_power_payload,
    parse_on_off_payload,
    validate_max_power_in_range,
    verify_max_power,
)
from ez1_bridge.config import Settings
from ez1_bridge.domain.models import DeviceInfo


def _make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "ez1_host": "192.168.3.24",
        "mqtt_host": "192.168.2.10",
        "mqtt_base_topic": "ez1",
        "mqtt_discovery_prefix": "homeassistant",
        "setmaxpower_verify": True,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


@pytest.fixture
def device_info() -> DeviceInfo:
    return DeviceInfo(
        device_id="E17010000783",
        firmware_version="EZ1 1.12.2t",
        ssid="my-wlan",
        ip_address="192.168.3.24",
        min_power_w=30,
        max_power_w=800,
    )


# --- Payload parsers ---------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [("600", 600), ("30", 30), ("0", 0), ("  450  ", 450)],
)
def test_parse_max_power_payload_accepts_clean_int(payload: str, expected: int) -> None:
    assert parse_max_power_payload(payload) == expected


@pytest.mark.parametrize("bad", ["", "   ", "600W", "600.0", "abc", "0x100", "--1"])
def test_parse_max_power_payload_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError, match=r"empty payload|integer watts"):
        parse_max_power_payload(bad)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("on", True),
        ("ON", True),
        ("On", True),
        ("1", True),
        ("off", False),
        ("OFF", False),
        ("0", False),
        ("  on  ", True),
    ],
)
def test_parse_on_off_payload(payload: str, expected: bool) -> None:
    assert parse_on_off_payload(payload) is expected


@pytest.mark.parametrize("bad", ["", "maybe", "true", "yes", "2"])
def test_parse_on_off_payload_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError, match="expected"):
        parse_on_off_payload(bad)


# --- Range validation -------------------------------------------------


def test_validate_max_power_accepts_in_range(device_info: DeviceInfo) -> None:
    validate_max_power_in_range(30, device_info)
    validate_max_power_in_range(450, device_info)
    validate_max_power_in_range(800, device_info)


@pytest.mark.parametrize("watts", [-10, 0, 29, 801, 9999])
def test_validate_max_power_rejects_out_of_range(
    device_info: DeviceInfo,
    watts: int,
) -> None:
    with pytest.raises(ValueError, match="outside"):
        validate_max_power_in_range(watts, device_info)


# --- parse_command_topic ----------------------------------------------


def test_parse_command_topic_extracts_name() -> None:
    assert parse_command_topic("ez1/E1/set/max_power", "ez1", "E1") == "max_power"
    assert parse_command_topic("ez1/E1/set/on_off", "ez1", "E1") == "on_off"


def test_parse_command_topic_returns_none_for_state_topic() -> None:
    assert parse_command_topic("ez1/E1/state", "ez1", "E1") is None


def test_parse_command_topic_returns_none_for_other_device() -> None:
    assert parse_command_topic("ez1/OTHER/set/max_power", "ez1", "E1") is None


def test_parse_command_topic_returns_none_for_other_base() -> None:
    assert parse_command_topic("solar/E1/set/max_power", "ez1", "E1") is None


# --- _decode_payload --------------------------------------------------


def test_decode_payload_bytes() -> None:
    assert _decode_payload(b"hello") == "hello"


def test_decode_payload_str() -> None:
    assert _decode_payload("hello") == "hello"


def test_decode_payload_none() -> None:
    assert _decode_payload(None) == ""


def test_decode_payload_int_falls_back_to_str() -> None:
    assert _decode_payload(42) == "42"


def test_decode_payload_invalid_utf8_replaced() -> None:
    assert _decode_payload(b"\xff\xfe") == "��"


# --- handle_max_power -------------------------------------------------


@pytest.fixture
def mock_publisher() -> MagicMock:
    pub = MagicMock()
    pub.publish_result = AsyncMock()
    return pub


@pytest.fixture
def mock_ez1() -> MagicMock:
    ez1 = MagicMock()
    ez1.set_max_power = AsyncMock(return_value={"data": {"maxPower": "600"}, "message": "SUCCESS"})
    ez1.set_on_off = AsyncMock(return_value={"data": {"status": "0"}, "message": "SUCCESS"})
    ez1.get_max_power = AsyncMock(
        return_value={"data": {"maxPower": "600"}, "message": "SUCCESS", "deviceId": "E1"},
    )
    return ez1


def _last_result(mock_publisher: MagicMock) -> dict[str, Any]:
    """Return the dict payload of the most recent publish_result call."""
    args = mock_publisher.publish_result.await_args
    payload = args.args[1] if len(args.args) > 1 else args.kwargs["payload"]
    return cast("dict[str, Any]", payload)


async def test_handle_max_power_success_publishes_ok_true(
    mock_ez1: MagicMock,
    mock_publisher: MagicMock,
    device_info: DeviceInfo,
) -> None:
    await handle_max_power(
        "600",
        ez1=mock_ez1,
        publisher=mock_publisher,
        device_info=device_info,
        verify=False,
    )

    mock_ez1.set_max_power.assert_awaited_once_with(600)
    result = _last_result(mock_publisher)
    assert result["ok"] is True
    assert result["value"] == "600"
    assert "error" not in result


async def test_handle_max_power_invalid_payload(
    mock_ez1: MagicMock,
    mock_publisher: MagicMock,
    device_info: DeviceInfo,
) -> None:
    await handle_max_power(
        "abc",
        ez1=mock_ez1,
        publisher=mock_publisher,
        device_info=device_info,
        verify=False,
    )

    mock_ez1.set_max_power.assert_not_awaited()
    result = _last_result(mock_publisher)
    assert result["ok"] is False
    assert result["error"] == "invalid_payload"
    assert "abc" in result["detail"]


async def test_handle_max_power_out_of_range(
    mock_ez1: MagicMock,
    mock_publisher: MagicMock,
    device_info: DeviceInfo,
) -> None:
    await handle_max_power(
        "1000",
        ez1=mock_ez1,
        publisher=mock_publisher,
        device_info=device_info,
        verify=False,
    )

    mock_ez1.set_max_power.assert_not_awaited()
    result = _last_result(mock_publisher)
    assert result["ok"] is False
    assert result["error"] == "out_of_range"
    assert "1000" in result["detail"]
    assert "800" in result["detail"]


async def test_handle_max_power_transport_error(
    mock_ez1: MagicMock,
    mock_publisher: MagicMock,
    device_info: DeviceInfo,
) -> None:
    mock_ez1.set_max_power.side_effect = RuntimeError("connection refused")
    await handle_max_power(
        "600",
        ez1=mock_ez1,
        publisher=mock_publisher,
        device_info=device_info,
        verify=False,
    )

    result = _last_result(mock_publisher)
    assert result["ok"] is False
    assert result["error"] == "transport_error"
    assert "RuntimeError" in result["detail"]


async def test_handle_max_power_verify_mismatch(
    mock_ez1: MagicMock,
    mock_publisher: MagicMock,
    device_info: DeviceInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the read-back returns a different value, publish verify_mismatch."""
    monkeypatch.setattr(
        "ez1_bridge.application.command_handler._VERIFY_DELAY_SECONDS",
        0.0,
    )
    mock_ez1.get_max_power.return_value = {
        "data": {"maxPower": "800"},
        "message": "SUCCESS",
        "deviceId": "E1",
    }

    await handle_max_power(
        "600",
        ez1=mock_ez1,
        publisher=mock_publisher,
        device_info=device_info,
        verify=True,
    )

    result = _last_result(mock_publisher)
    assert result["ok"] is False
    assert result["error"] == "verify_mismatch"
    assert result["expected"] == 600
    assert result["actual"] == 800


async def test_handle_max_power_verify_match_publishes_ok(
    mock_ez1: MagicMock,
    mock_publisher: MagicMock,
    device_info: DeviceInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ez1_bridge.application.command_handler._VERIFY_DELAY_SECONDS",
        0.0,
    )
    mock_ez1.get_max_power.return_value = {
        "data": {"maxPower": "600"},
        "message": "SUCCESS",
        "deviceId": "E1",
    }

    await handle_max_power(
        "600",
        ez1=mock_ez1,
        publisher=mock_publisher,
        device_info=device_info,
        verify=True,
    )

    result = _last_result(mock_publisher)
    assert result["ok"] is True


async def test_handle_max_power_verify_off_skips_readback(
    mock_ez1: MagicMock,
    mock_publisher: MagicMock,
    device_info: DeviceInfo,
) -> None:
    await handle_max_power(
        "600",
        ez1=mock_ez1,
        publisher=mock_publisher,
        device_info=device_info,
        verify=False,
    )

    mock_ez1.get_max_power.assert_not_awaited()
    assert _last_result(mock_publisher)["ok"] is True


async def test_handle_max_power_verify_readback_fails(
    mock_ez1: MagicMock,
    mock_publisher: MagicMock,
    device_info: DeviceInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If get_max_power itself raises, publish transport_error, not verify_mismatch."""
    monkeypatch.setattr(
        "ez1_bridge.application.command_handler._VERIFY_DELAY_SECONDS",
        0.0,
    )
    mock_ez1.get_max_power.side_effect = RuntimeError("read-back unreachable")

    await handle_max_power(
        "600",
        ez1=mock_ez1,
        publisher=mock_publisher,
        device_info=device_info,
        verify=True,
    )

    result = _last_result(mock_publisher)
    assert result["ok"] is False
    assert result["error"] == "transport_error"
    assert "verify read-back" in result["detail"]


# --- handle_on_off ----------------------------------------------------


async def test_handle_on_off_on(
    mock_ez1: MagicMock,
    mock_publisher: MagicMock,
) -> None:
    await handle_on_off("on", ez1=mock_ez1, publisher=mock_publisher)
    mock_ez1.set_on_off.assert_awaited_once_with(on=True)
    result = _last_result(mock_publisher)
    assert result["ok"] is True
    assert result["value"] == "on"


async def test_handle_on_off_off(
    mock_ez1: MagicMock,
    mock_publisher: MagicMock,
) -> None:
    await handle_on_off("off", ez1=mock_ez1, publisher=mock_publisher)
    mock_ez1.set_on_off.assert_awaited_once_with(on=False)
    result = _last_result(mock_publisher)
    assert result["ok"] is True
    assert result["value"] == "off"


async def test_handle_on_off_zero_one_aliases(
    mock_ez1: MagicMock,
    mock_publisher: MagicMock,
) -> None:
    await handle_on_off("1", ez1=mock_ez1, publisher=mock_publisher)
    mock_ez1.set_on_off.assert_awaited_with(on=True)
    await handle_on_off("0", ez1=mock_ez1, publisher=mock_publisher)
    mock_ez1.set_on_off.assert_awaited_with(on=False)


async def test_handle_on_off_invalid_payload(
    mock_ez1: MagicMock,
    mock_publisher: MagicMock,
) -> None:
    await handle_on_off("maybe", ez1=mock_ez1, publisher=mock_publisher)
    mock_ez1.set_on_off.assert_not_awaited()
    result = _last_result(mock_publisher)
    assert result["ok"] is False
    assert result["error"] == "invalid_payload"


async def test_handle_on_off_transport_error(
    mock_ez1: MagicMock,
    mock_publisher: MagicMock,
) -> None:
    mock_ez1.set_on_off.side_effect = RuntimeError("boom")
    await handle_on_off("on", ez1=mock_ez1, publisher=mock_publisher)
    result = _last_result(mock_publisher)
    assert result["ok"] is False
    assert result["error"] == "transport_error"


# --- verify_max_power -------------------------------------------------


async def test_verify_max_power_reads_after_delay(
    mock_ez1: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    mock_ez1.get_max_power.return_value = {
        "data": {"maxPower": "750"},
        "message": "SUCCESS",
        "deviceId": "E1",
    }

    result = await verify_max_power(mock_ez1, delay_s=2.0)

    assert result == 750
    assert sleep_calls == [2.0]


# --- _dispatch --------------------------------------------------------


async def _msg(topic: str, payload: bytes) -> aiomqtt.Message:
    """Build a minimal aiomqtt.Message-like object for dispatch tests."""
    msg = MagicMock(spec=aiomqtt.Message)
    msg.topic = MagicMock()
    msg.topic.__str__ = lambda _self: topic
    msg.payload = payload
    return cast("aiomqtt.Message", msg)


async def test_dispatch_routes_max_power(
    mock_ez1: MagicMock,
    mock_publisher: MagicMock,
    device_info: DeviceInfo,
) -> None:
    settings = _make_settings(setmaxpower_verify=False)
    msg = await _msg("ez1/E17010000783/set/max_power", b"600")

    await _dispatch(
        msg=msg,
        ez1=mock_ez1,
        publisher=mock_publisher,
        device_info=device_info,
        settings=settings,
    )

    mock_ez1.set_max_power.assert_awaited_once_with(600)


async def test_dispatch_routes_on_off(
    mock_ez1: MagicMock,
    mock_publisher: MagicMock,
    device_info: DeviceInfo,
) -> None:
    settings = _make_settings()
    msg = await _msg("ez1/E17010000783/set/on_off", b"on")

    await _dispatch(
        msg=msg,
        ez1=mock_ez1,
        publisher=mock_publisher,
        device_info=device_info,
        settings=settings,
    )

    mock_ez1.set_on_off.assert_awaited_once_with(on=True)


async def test_dispatch_ignores_unknown_command(
    mock_ez1: MagicMock,
    mock_publisher: MagicMock,
    device_info: DeviceInfo,
) -> None:
    settings = _make_settings()
    msg = await _msg("ez1/E17010000783/set/foobar", b"value")

    await _dispatch(
        msg=msg,
        ez1=mock_ez1,
        publisher=mock_publisher,
        device_info=device_info,
        settings=settings,
    )

    mock_ez1.set_max_power.assert_not_awaited()
    mock_ez1.set_on_off.assert_not_awaited()
    mock_publisher.publish_result.assert_not_awaited()


async def test_dispatch_ignores_unrelated_topic(
    mock_ez1: MagicMock,
    mock_publisher: MagicMock,
    device_info: DeviceInfo,
) -> None:
    settings = _make_settings()
    msg = await _msg("ez1/E17010000783/state", b"...")

    await _dispatch(
        msg=msg,
        ez1=mock_ez1,
        publisher=mock_publisher,
        device_info=device_info,
        settings=settings,
    )

    mock_publisher.publish_result.assert_not_awaited()


# --- command_loop -----------------------------------------------------


class _FakeMessageStream:
    """Async-iterable that yields a fixed set of messages then blocks forever.

    Mimics aiomqtt.Client.messages closely enough for command_loop's
    contract: subscribe + async for with cancellation support.
    """

    def __init__(self, messages: list[aiomqtt.Message]) -> None:
        self._queue: asyncio.Queue[aiomqtt.Message] = asyncio.Queue()
        for m in messages:
            self._queue.put_nowait(m)

    def __aiter__(self) -> AsyncIterator[aiomqtt.Message]:
        return self

    async def __anext__(self) -> aiomqtt.Message:
        return await self._queue.get()


def _fake_client(messages: list[aiomqtt.Message]) -> MagicMock:
    client = MagicMock(spec=aiomqtt.Client)
    client.subscribe = AsyncMock()
    client.messages = _FakeMessageStream(messages)
    return client


async def test_command_loop_subscribes_and_dispatches(
    mock_ez1: MagicMock,
    mock_publisher: MagicMock,
    device_info: DeviceInfo,
) -> None:
    settings = _make_settings(setmaxpower_verify=False)
    msg1 = await _msg("ez1/E17010000783/set/max_power", b"600")
    msg2 = await _msg("ez1/E17010000783/set/on_off", b"off")

    stop_event = asyncio.Event()
    client = _fake_client([msg1, msg2])

    async def stop_after_two() -> None:
        # Wait for both publish_result calls, then signal stop.
        while mock_publisher.publish_result.await_count < 2:
            await asyncio.sleep(0.01)
        stop_event.set()

    _trigger = asyncio.create_task(stop_after_two())
    loop_task = asyncio.create_task(
        command_loop(
            client=client,
            ez1=mock_ez1,
            publisher=mock_publisher,
            device_info=device_info,
            settings=settings,
            stop_event=stop_event,
        ),
    )
    # Loop will hang on the empty queue after two messages -- cancel it
    # once both dispatched, mirroring run_service's stop pattern.
    await _trigger
    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task

    client.subscribe.assert_awaited_once_with("ez1/E17010000783/set/+", qos=1)
    mock_ez1.set_max_power.assert_awaited_once_with(600)
    mock_ez1.set_on_off.assert_awaited_once_with(on=False)


async def test_command_loop_exits_cleanly_on_cancel(
    mock_ez1: MagicMock,
    mock_publisher: MagicMock,
    device_info: DeviceInfo,
) -> None:
    """Cancellation of the task while waiting on async-for must not hang.

    This is the lackmustest the user asked for: TaskGroup-style external
    cancel of command_loop must complete within 1 s.
    """
    settings = _make_settings()
    stop_event = asyncio.Event()
    client = _fake_client([])  # empty queue -> async-for hangs forever

    loop_task = asyncio.create_task(
        command_loop(
            client=client,
            ez1=mock_ez1,
            publisher=mock_publisher,
            device_info=device_info,
            settings=settings,
            stop_event=stop_event,
        ),
    )
    await asyncio.sleep(0.05)  # let it subscribe + start blocking on async-for
    loop_task.cancel()

    async with asyncio.timeout(1.0):
        with pytest.raises(asyncio.CancelledError):
            await loop_task


async def test_command_loop_stop_event_observed_between_messages(
    mock_ez1: MagicMock,
    mock_publisher: MagicMock,
    device_info: DeviceInfo,
) -> None:
    """If stop_event fires while a message is being processed, the loop
    returns on the *next* iteration without dispatching that message.
    """
    settings = _make_settings(setmaxpower_verify=False)
    msg1 = await _msg("ez1/E17010000783/set/max_power", b"600")
    msg2 = await _msg("ez1/E17010000783/set/on_off", b"on")

    stop_event = asyncio.Event()
    client = _fake_client([msg1, msg2])

    async def fake_set_max_power(_w: int) -> dict[str, Any]:
        stop_event.set()
        return {"data": {"maxPower": "600"}, "message": "SUCCESS"}

    mock_ez1.set_max_power = AsyncMock(side_effect=fake_set_max_power)

    await asyncio.wait_for(
        command_loop(
            client=client,
            ez1=mock_ez1,
            publisher=mock_publisher,
            device_info=device_info,
            settings=settings,
            stop_event=stop_event,
        ),
        timeout=1.0,
    )

    mock_ez1.set_max_power.assert_awaited_once()
    mock_ez1.set_on_off.assert_not_awaited()


async def test_command_loop_swallows_handler_exception(
    mock_ez1: MagicMock,
    mock_publisher: MagicMock,
    device_info: DeviceInfo,
) -> None:
    """A catastrophic raise inside _dispatch must not kill the loop.

    The fallback emits a transport_error result on the (best-guess)
    result topic so HA does not silently drop the command.
    """
    settings = _make_settings(setmaxpower_verify=False)
    msg1 = await _msg("ez1/E17010000783/set/max_power", b"600")
    msg2 = await _msg("ez1/E17010000783/set/on_off", b"on")

    stop_event = asyncio.Event()
    client = _fake_client([msg1, msg2])

    # First publish_result raises; second must still be reached.
    raised_once = {"flag": False}

    async def maybe_raise(*args: Any, **kwargs: Any) -> None:
        if not raised_once["flag"]:
            raised_once["flag"] = True
            msg = "broker stalled"
            raise RuntimeError(msg)

    mock_publisher.publish_result.side_effect = maybe_raise

    async def stop_when_done() -> None:
        while mock_publisher.publish_result.await_count < 3:
            await asyncio.sleep(0.01)
        stop_event.set()

    _trigger = asyncio.create_task(stop_when_done())
    loop_task = asyncio.create_task(
        command_loop(
            client=client,
            ez1=mock_ez1,
            publisher=mock_publisher,
            device_info=device_info,
            settings=settings,
            stop_event=stop_event,
        ),
    )
    await _trigger
    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task

    # Three calls expected: original failure, dispatch-failure fallback,
    # then the second message's success.
    assert mock_publisher.publish_result.await_count >= 2


# --- _emit_dispatch_failure -------------------------------------------


async def test_emit_dispatch_failure_skips_unknown_command(
    mock_publisher: MagicMock,
    device_info: DeviceInfo,
) -> None:
    settings = _make_settings()
    msg = await _msg("ez1/E17010000783/set/unknown", b"x")

    await _emit_dispatch_failure(msg, mock_publisher, settings, device_info)

    mock_publisher.publish_result.assert_not_awaited()


# --- CommandResult ----------------------------------------------------


def test_command_result_serialises_compact_on_success() -> None:
    result = CommandResult(
        ok=True,
        ts=datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
        value="600",
    )
    dumped = json.loads(result.model_dump_json(exclude_none=True))
    assert set(dumped.keys()) == {"ok", "ts", "value"}


def test_command_result_serialises_with_detail_on_error() -> None:
    result = CommandResult(
        ok=False,
        ts=datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
        error="out_of_range",
        detail="value 900 outside [30, 800]",
    )
    dumped = json.loads(result.model_dump_json(exclude_none=True))
    assert dumped["ok"] is False
    assert dumped["error"] == "out_of_range"
    assert "value" not in dumped


def test_command_result_is_frozen() -> None:
    result = CommandResult(ok=True, ts=datetime(2026, 4, 26, tzinfo=UTC), value="x")
    with pytest.raises(ValidationError):
        result.value = "y"


# --- helper-only static checks ----------------------------------------


def test_known_commands_set_unchanged() -> None:
    """Regression guard against silently growing the dispatch table."""
    assert frozenset({"max_power", "on_off"}) == _KNOWN_COMMANDS
