"""Unit tests for :mod:`ez1_bridge.application.poll_service`."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import httpx

from ez1_bridge.application.poll_service import (
    _wait_or_stop,
    availability_heartbeat,
    poll_loop,
)
from ez1_bridge.config import Settings


def _make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "ez1_host": "192.168.3.24",
        "mqtt_host": "192.168.2.10",
        "poll_interval": 1,
        "mqtt_base_topic": "ez1",
        "mqtt_discovery_prefix": "homeassistant",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def _arm_ez1_mock(
    api_response: Callable[[str], dict[str, Any]],
) -> AsyncMock:
    ez1 = AsyncMock()
    ez1.get_output_data.return_value = api_response("get_output_data")
    ez1.get_max_power.return_value = api_response("get_max_power")
    ez1.get_alarm.return_value = api_response("get_alarm")
    ez1.get_on_off.return_value = api_response("get_on_off")
    ez1.get_device_info.return_value = api_response("get_device_info")
    return ez1


# --- _wait_or_stop -----------------------------------------------------


async def test_wait_or_stop_returns_true_when_event_set() -> None:
    stop_event = asyncio.Event()
    stop_event.set()
    assert await _wait_or_stop(stop_event, timeout=1.0) is True


async def test_wait_or_stop_returns_false_after_timeout() -> None:
    stop_event = asyncio.Event()
    assert await _wait_or_stop(stop_event, timeout=0.05) is False


async def test_wait_or_stop_returns_true_on_event_during_wait() -> None:
    stop_event = asyncio.Event()

    async def trigger() -> None:
        await asyncio.sleep(0.02)
        stop_event.set()

    _trigger = asyncio.create_task(trigger())  # noqa: RUF006
    assert await _wait_or_stop(stop_event, timeout=1.0) is True


# --- poll_loop ---------------------------------------------------------


async def test_poll_loop_publishes_state_and_discovery_on_first_cycle(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    stop_event = asyncio.Event()
    ez1 = _arm_ez1_mock(api_response)
    publisher = AsyncMock()

    async def stop_after(_state: object) -> None:
        stop_event.set()

    publisher.publish_state.side_effect = stop_after

    await asyncio.wait_for(
        poll_loop(
            ez1=ez1,
            publisher=publisher,
            settings=_make_settings(),
            stop_event=stop_event,
        ),
        timeout=2.0,
    )

    publisher.publish_state.assert_awaited_once()
    state = publisher.publish_state.await_args.args[0]
    assert state.device_id == "E17010000783"
    assert state.power.total_w == 204.0

    ez1.get_device_info.assert_awaited_once()
    assert publisher.publish.await_count == 15  # 11 sensor + 4 binary_sensor


async def test_poll_loop_does_not_republish_discovery_within_refresh_window(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    stop_event = asyncio.Event()
    ez1 = _arm_ez1_mock(api_response)
    publisher = AsyncMock()

    cycles = 0

    async def stop_after_two(_state: object) -> None:
        nonlocal cycles
        cycles += 1
        if cycles >= 2:
            stop_event.set()

    publisher.publish_state.side_effect = stop_after_two

    await asyncio.wait_for(
        poll_loop(
            ez1=ez1,
            publisher=publisher,
            settings=_make_settings(poll_interval=1),
            stop_event=stop_event,
        ),
        timeout=4.0,
    )

    assert publisher.publish_state.await_count == 2
    # Discovery only fetched once even though we polled twice.
    assert ez1.get_device_info.await_count == 1
    assert publisher.publish.await_count == 15


async def test_poll_loop_republishes_discovery_after_refresh_interval(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    stop_event = asyncio.Event()
    ez1 = _arm_ez1_mock(api_response)
    publisher = AsyncMock()

    cycles = 0

    async def stop_after_two(_state: object) -> None:
        nonlocal cycles
        cycles += 1
        if cycles >= 2:
            stop_event.set()

    publisher.publish_state.side_effect = stop_after_two

    # Force every cycle to refresh discovery.
    await asyncio.wait_for(
        poll_loop(
            ez1=ez1,
            publisher=publisher,
            settings=_make_settings(poll_interval=1),
            stop_event=stop_event,
            discovery_refresh_seconds=0.0,
        ),
        timeout=4.0,
    )

    assert publisher.publish_state.await_count == 2
    assert ez1.get_device_info.await_count == 2
    assert publisher.publish.await_count == 30


async def test_poll_loop_marks_offline_on_connect_error(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    stop_event = asyncio.Event()
    ez1 = _arm_ez1_mock(api_response)
    ez1.get_output_data.side_effect = httpx.ConnectError("refused")
    publisher = AsyncMock()

    async def trigger() -> None:
        await asyncio.sleep(0.05)
        stop_event.set()

    _trigger = asyncio.create_task(trigger())  # noqa: RUF006

    await asyncio.wait_for(
        poll_loop(
            ez1=ez1,
            publisher=publisher,
            settings=_make_settings(poll_interval=1),
            stop_event=stop_event,
        ),
        timeout=2.0,
    )

    publisher.publish_availability.assert_awaited_with(online=False)
    publisher.publish_state.assert_not_awaited()


async def test_poll_loop_swallows_unexpected_exceptions(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    stop_event = asyncio.Event()
    ez1 = _arm_ez1_mock(api_response)
    ez1.get_output_data.side_effect = RuntimeError("boom")
    publisher = AsyncMock()

    async def trigger() -> None:
        await asyncio.sleep(0.05)
        stop_event.set()

    _trigger = asyncio.create_task(trigger())  # noqa: RUF006

    # The loop must not propagate the RuntimeError -- it logs and continues.
    await asyncio.wait_for(
        poll_loop(
            ez1=ez1,
            publisher=publisher,
            settings=_make_settings(poll_interval=1),
            stop_event=stop_event,
        ),
        timeout=2.0,
    )

    publisher.publish_state.assert_not_awaited()


async def test_poll_loop_exits_immediately_when_stop_event_pre_set(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    stop_event = asyncio.Event()
    stop_event.set()
    ez1 = _arm_ez1_mock(api_response)
    publisher = AsyncMock()

    await asyncio.wait_for(
        poll_loop(
            ez1=ez1,
            publisher=publisher,
            settings=_make_settings(poll_interval=60),
            stop_event=stop_event,
        ),
        timeout=0.5,
    )

    publisher.publish_state.assert_not_awaited()
    ez1.get_output_data.assert_not_awaited()


async def test_poll_loop_publishes_offline_swallows_secondary_failure(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    """If publishing the offline marker also fails, the loop must keep going."""
    stop_event = asyncio.Event()
    ez1 = _arm_ez1_mock(api_response)
    ez1.get_output_data.side_effect = httpx.ConnectError("refused")
    publisher = AsyncMock()
    publisher.publish_availability.side_effect = RuntimeError("broker dead too")

    async def trigger() -> None:
        await asyncio.sleep(0.05)
        stop_event.set()

    _trigger = asyncio.create_task(trigger())  # noqa: RUF006

    await asyncio.wait_for(
        poll_loop(
            ez1=ez1,
            publisher=publisher,
            settings=_make_settings(poll_interval=1),
            stop_event=stop_event,
        ),
        timeout=2.0,
    )


# --- availability_heartbeat -------------------------------------------


async def test_heartbeat_publishes_online_each_iteration() -> None:
    stop_event = asyncio.Event()
    publisher = AsyncMock()

    cycles = 0

    async def stop_after_two(*, online: bool) -> None:
        nonlocal cycles
        assert online is True
        cycles += 1
        if cycles >= 2:
            stop_event.set()

    publisher.publish_availability.side_effect = stop_after_two

    await asyncio.wait_for(
        availability_heartbeat(
            publisher=publisher,
            stop_event=stop_event,
            interval=0.05,
        ),
        timeout=2.0,
    )

    assert publisher.publish_availability.await_count == 2


async def test_heartbeat_exits_on_stop_event() -> None:
    stop_event = asyncio.Event()
    stop_event.set()
    publisher = AsyncMock()

    await asyncio.wait_for(
        availability_heartbeat(
            publisher=publisher,
            stop_event=stop_event,
            interval=60.0,
        ),
        timeout=0.5,
    )

    publisher.publish_availability.assert_not_awaited()


async def test_heartbeat_swallows_publish_errors() -> None:
    stop_event = asyncio.Event()
    publisher = AsyncMock()
    cycles = 0

    async def fail_once_then_stop(*, online: bool) -> None:
        nonlocal cycles
        del online
        cycles += 1
        if cycles == 1:
            msg = "transient broker error"
            raise RuntimeError(msg)
        stop_event.set()

    publisher.publish_availability.side_effect = fail_once_then_stop

    await asyncio.wait_for(
        availability_heartbeat(
            publisher=publisher,
            stop_event=stop_event,
            interval=0.02,
        ),
        timeout=2.0,
    )

    assert publisher.publish_availability.await_count == 2
