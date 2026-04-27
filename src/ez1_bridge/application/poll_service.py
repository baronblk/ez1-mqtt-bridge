"""Periodic poll loop and availability heartbeat coroutines.

Exposes the two coroutines that the main TaskGroup spawns in Phase 4:

* :func:`poll_loop` — every ``settings.poll_interval`` seconds, hits the
  four EZ1 read endpoints in parallel, builds an :class:`InverterState`,
  and publishes it. Also handles HA discovery: on the first successful
  cycle and every 24 h thereafter it fetches ``getDeviceInfo`` and
  publishes the 15 discovery messages built by :func:`build_discovery_messages`.
* :func:`availability_heartbeat` — every 30 s, re-publishes the
  ``availability=online`` retained message. Redundant with the
  CONNECT-time announcement, *and that is the point*: if the broker
  loses retained state for any reason (restart without ``persistence``,
  manual ``mosquitto_pub -r -n``, broker bug), the bridge re-asserts
  liveness within 30 s rather than letting Home Assistant's
  availability badge drift.

Both loops respect ``stop_event``: each iteration either runs to
completion and then waits ``min(interval, until-stop)``, exiting
promptly when the event is set. Errors inside an iteration are logged
and swallowed so a transient failure (Nacht-offline, broker hiccup)
does not bring the whole TaskGroup down.

Phase 5 will add ``command_loop`` and Phase 6 will add ``metrics_server``;
each is a sibling task in the same TaskGroup, sharing the same
``stop_event``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json as json_lib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import httpx
import structlog

from ez1_bridge.adapters.ez1_http import EZ1Client
from ez1_bridge.adapters.mqtt_publisher import MQTTPublisher
from ez1_bridge.application.ha_discovery import build_discovery_messages
from ez1_bridge.config import Settings
from ez1_bridge.domain.normalizer import build_state, parse_device_info

if TYPE_CHECKING:
    from ez1_bridge.adapters.prom_metrics import MetricsRegistry

_log = structlog.get_logger(__name__)

_HEARTBEAT_INTERVAL_SECONDS: Final[float] = 30.0
_DISCOVERY_REFRESH_SECONDS: Final[float] = 24 * 60 * 60  # 24 h


async def _wait_or_stop(stop_event: asyncio.Event, timeout: float) -> bool:
    """Wait for either ``timeout`` seconds or ``stop_event``.

    Returns ``True`` if the stop event fired (caller should exit),
    ``False`` if the timeout elapsed normally.
    """
    try:
        async with asyncio.timeout(timeout):
            await stop_event.wait()
    except TimeoutError:
        return False
    return True


async def _publish_discovery(
    *,
    ez1: EZ1Client,
    publisher: MQTTPublisher,
    settings: Settings,
) -> None:
    """Fetch device info and publish the 15 HA discovery messages.

    Called once on the first successful poll and again every 24 h.
    Raises whatever the underlying HTTP/MQTT calls raise -- the caller
    decides how to handle failure (typically: log + retry next cycle).
    """
    envelope = await ez1.get_device_info()
    info = parse_device_info(envelope)
    messages = build_discovery_messages(
        info,
        base_topic=settings.mqtt_base_topic,
        discovery_prefix=settings.mqtt_discovery_prefix,
    )
    for msg in messages:
        await publisher.publish(
            msg.topic,
            json_lib.dumps(msg.payload),
            retain=msg.retain,
        )
    _log.info(
        "ha_discovery_published",
        device_id=info.device_id,
        firmware=info.firmware_version,
        message_count=len(messages),
    )


async def poll_loop(
    *,
    ez1: EZ1Client,
    publisher: MQTTPublisher,
    settings: Settings,
    stop_event: asyncio.Event,
    discovery_refresh_seconds: float = _DISCOVERY_REFRESH_SECONDS,
    metrics: MetricsRegistry | None = None,
) -> None:
    """Run the poll cycle until ``stop_event`` is set.

    Each iteration:

    1. Fetches the four read endpoints in parallel via ``asyncio.gather``.
    2. Builds a typed :class:`InverterState` and publishes it.
    3. Mirrors the state onto the metrics registry's gauges (if provided).
    4. Republishes HA discovery if this is the first successful poll
       or 24 h have elapsed since the last refresh.
    5. Waits ``settings.poll_interval`` seconds (or exits immediately
       if ``stop_event`` is set during the wait).
    """
    last_discovery_at: datetime | None = None

    while not stop_event.is_set():
        try:
            output_data, max_power, alarm, on_off = await asyncio.gather(
                ez1.get_output_data(),
                ez1.get_max_power(),
                ez1.get_alarm(),
                ez1.get_on_off(),
            )
            now = datetime.now(tz=UTC)
            state = build_state(
                output_data=output_data,
                max_power=max_power,
                alarm=alarm,
                on_off=on_off,
                ts=now,
            )
            await publisher.publish_state(state)
            if metrics is not None:
                metrics.record_state(state)

            if (
                last_discovery_at is None
                or (now - last_discovery_at).total_seconds() >= discovery_refresh_seconds
            ):
                await _publish_discovery(ez1=ez1, publisher=publisher, settings=settings)
                last_discovery_at = now

        except httpx.ConnectError:
            _log.info("ez1_unreachable", action="mark_offline")
            with contextlib.suppress(Exception):
                await publisher.publish_availability(online=False)
        except Exception:
            _log.warning("poll_cycle_failed", exc_info=True)

        if await _wait_or_stop(stop_event, settings.poll_interval):
            return


async def availability_heartbeat(
    *,
    publisher: MQTTPublisher,
    stop_event: asyncio.Event,
    interval: float = _HEARTBEAT_INTERVAL_SECONDS,
) -> None:
    """Re-publish ``availability=online`` every ``interval`` seconds.

    Defends against the case where Mosquitto silently loses retained
    state -- after a restart without ``persistence true``, after a
    manual ``mosquitto_pub -r -n``, or due to a broker bug.
    """
    while not stop_event.is_set():
        try:
            await publisher.publish_availability(online=True)
        except Exception:
            _log.warning("availability_heartbeat_failed", exc_info=True)

        if await _wait_or_stop(stop_event, interval):
            return
