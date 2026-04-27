"""MQTT command dispatcher: forwards write requests to the EZ1 inverter.

Subscribes to ``{base}/{device_id}/set/+``, validates each incoming
payload, calls the matching :class:`EZ1Client` write method, optionally
re-reads the affected value to verify the inverter accepted it, and
publishes a structured result on
``{base}/{device_id}/result/{command}``.

Result-topic payload schema
---------------------------

Success::

    {"ok": true, "ts": "2026-04-26T18:00:00+00:00", "value": "600"}

Failure (codes are stable for HA automations to match against)::

    {"ok": false, "ts": "...", "error": "invalid_payload", "detail": "..."}
    {"ok": false, "ts": "...", "error": "out_of_range", "detail": "..."}
    {"ok": false, "ts": "...", "error": "transport_error", "detail": "..."}
    {
        "ok": false,
        "ts": "...",
        "error": "verify_mismatch",
        "detail": "...",
        "expected": 600,
        "actual": 800,
    }

Concurrency
-----------

Commands are dispatched sequentially within :func:`command_loop`. The
EZ1 inverter serialises its own HTTP requests anyway, so two
back-to-back set commands queue at the broker (QoS 1) and process in
order. If concurrent dispatch ever becomes a need, hand each handler
to ``tg.create_task`` instead of awaiting it inline -- the result-publish
flow does not depend on serialisation.

Cancellation
------------

The loop's ``async for`` over the MQTT message stream is the only
blocking point that does not observe ``stop_event`` between events.
:func:`run_service` therefore awaits ``stop_event`` itself and calls
``command_task.cancel()`` to break the iterator; :class:`asyncio.CancelledError`
propagates out cleanly.
"""

from __future__ import annotations

import asyncio
import json as json_lib
from datetime import UTC, datetime
from typing import Final, Literal

import aiomqtt
import structlog
from pydantic import BaseModel, ConfigDict

from ez1_bridge import topics
from ez1_bridge.adapters.ez1_http import EZ1Client
from ez1_bridge.adapters.mqtt_publisher import MQTTPublisher
from ez1_bridge.config import Settings
from ez1_bridge.domain.models import DeviceInfo
from ez1_bridge.domain.normalizer import parse_max_power_w

_log = structlog.get_logger(__name__)

#: Default delay between a setMaxPower write and the verify read-back. The
#: EZ1 firmware needs ~1-2 s to reflect the change on getMaxPower.
_VERIFY_DELAY_SECONDS: Final[float] = 2.0

CommandName = Literal["max_power", "on_off"]
ErrorCode = Literal[
    "invalid_payload",
    "out_of_range",
    "transport_error",
    "verify_mismatch",
]


class CommandResult(BaseModel):
    """Outgoing payload for ``{base}/{device_id}/result/{command}``.

    All fields except ``ok`` and ``ts`` are optional and serialised only
    when present, so the wire format stays minimal per result kind.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    ts: datetime
    value: str | None = None
    error: ErrorCode | None = None
    detail: str | None = None
    expected: int | None = None
    actual: int | None = None


# --- Payload parsing ---------------------------------------------------


def parse_max_power_payload(payload: str) -> int:
    """Parse a ``setMaxPower`` payload into watts.

    Accepts a clean integer string. Whitespace is tolerated; anything
    else (units, decimals, hex, empty) raises :class:`ValueError` so
    the dispatcher can publish ``invalid_payload``.
    """
    stripped = payload.strip()
    if not stripped:
        msg = "empty payload"
        raise ValueError(msg)
    try:
        return int(stripped)
    except ValueError as exc:
        msg = f"expected integer watts, got {payload!r}"
        raise ValueError(msg) from exc


def parse_on_off_payload(payload: str) -> bool:
    """Parse a ``setOnOff`` payload into a Python ``bool``.

    Accepts ``"on"``/``"off"`` (case-insensitive, the documented
    convention) and ``"1"``/``"0"`` (the inverter's wire format, kept
    as a safety valve for HA users who type the raw value).
    """
    stripped = payload.strip().lower()
    if stripped in {"on", "1"}:
        return True
    if stripped in {"off", "0"}:
        return False
    msg = f"expected 'on'/'off' or '1'/'0', got {payload!r}"
    raise ValueError(msg)


# --- Range validation --------------------------------------------------


def validate_max_power_in_range(watts: int, info: DeviceInfo) -> None:
    """Reject ``watts`` outside ``[info.min_power_w, info.max_power_w]``.

    Bounds come from the live ``getDeviceInfo`` response, not hard-coded
    constants -- a future inverter with a different range works without
    code changes.
    """
    if watts < info.min_power_w or watts > info.max_power_w:
        msg = f"value {watts} outside [{info.min_power_w}, {info.max_power_w}]"
        raise ValueError(msg)


# --- Verify read-back --------------------------------------------------


async def verify_max_power(
    ez1: EZ1Client,
    *,
    delay_s: float = _VERIFY_DELAY_SECONDS,
) -> int:
    """Wait briefly, then return the current ``getMaxPower`` value.

    Caller compares the result against the value it wrote and emits a
    ``verify_mismatch`` result if they differ. The sleep gives the
    inverter time to reflect the write on getMaxPower (1-2 s in
    practice on firmware EZ1 1.12.2t).
    """
    await asyncio.sleep(delay_s)
    return parse_max_power_w(await ez1.get_max_power())


# --- Result publishing -------------------------------------------------


async def _publish_result(
    publisher: MQTTPublisher,
    command_name: CommandName,
    result: CommandResult,
) -> None:
    payload = result.model_dump(mode="json", exclude_none=True)
    await publisher.publish_result(command_name, payload)


# --- Command handlers --------------------------------------------------


async def handle_max_power(
    payload: str,
    *,
    ez1: EZ1Client,
    publisher: MQTTPublisher,
    device_info: DeviceInfo,
    verify: bool,
) -> None:
    """Process a single ``setMaxPower`` command and publish its result."""
    started = datetime.now(tz=UTC)
    try:
        watts = parse_max_power_payload(payload)
    except ValueError as exc:
        await _publish_result(
            publisher,
            "max_power",
            CommandResult(ok=False, ts=started, error="invalid_payload", detail=str(exc)),
        )
        return

    try:
        validate_max_power_in_range(watts, device_info)
    except ValueError as exc:
        await _publish_result(
            publisher,
            "max_power",
            CommandResult(ok=False, ts=started, error="out_of_range", detail=str(exc)),
        )
        return

    try:
        await ez1.set_max_power(watts)
    except Exception as exc:
        await _publish_result(
            publisher,
            "max_power",
            CommandResult(
                ok=False,
                ts=started,
                error="transport_error",
                detail=f"{type(exc).__name__}: {exc}",
            ),
        )
        return

    if verify:
        try:
            actual = await verify_max_power(ez1)
        except Exception as exc:
            await _publish_result(
                publisher,
                "max_power",
                CommandResult(
                    ok=False,
                    ts=started,
                    error="transport_error",
                    detail=f"verify read-back failed: {type(exc).__name__}: {exc}",
                ),
            )
            return
        if actual != watts:
            await _publish_result(
                publisher,
                "max_power",
                CommandResult(
                    ok=False,
                    ts=started,
                    error="verify_mismatch",
                    detail=f"expected {watts}, actual {actual}",
                    expected=watts,
                    actual=actual,
                ),
            )
            return

    await _publish_result(
        publisher,
        "max_power",
        CommandResult(ok=True, ts=datetime.now(tz=UTC), value=str(watts)),
    )


async def handle_on_off(
    payload: str,
    *,
    ez1: EZ1Client,
    publisher: MQTTPublisher,
) -> None:
    """Process a single ``setOnOff`` command and publish its result."""
    started = datetime.now(tz=UTC)
    try:
        on = parse_on_off_payload(payload)
    except ValueError as exc:
        await _publish_result(
            publisher,
            "on_off",
            CommandResult(ok=False, ts=started, error="invalid_payload", detail=str(exc)),
        )
        return

    try:
        await ez1.set_on_off(on=on)
    except Exception as exc:
        await _publish_result(
            publisher,
            "on_off",
            CommandResult(
                ok=False,
                ts=started,
                error="transport_error",
                detail=f"{type(exc).__name__}: {exc}",
            ),
        )
        return

    await _publish_result(
        publisher,
        "on_off",
        CommandResult(
            ok=True,
            ts=datetime.now(tz=UTC),
            value="on" if on else "off",
        ),
    )


# --- Topic dispatch ----------------------------------------------------


_KNOWN_COMMANDS: Final[frozenset[CommandName]] = frozenset({"max_power", "on_off"})


def parse_command_topic(topic: str, base_topic: str, device_id: str) -> str | None:
    """Extract the command name from ``{base}/{device_id}/set/{name}``.

    Returns ``None`` if the topic does not match the expected pattern,
    so the loop can log and ignore stray messages without raising.
    """
    expected_prefix = f"{base_topic}/{device_id}/set/"
    if not topic.startswith(expected_prefix):
        return None
    return topic[len(expected_prefix) :]


def _decode_payload(raw: bytes | bytearray | str | int | float | None) -> str:
    """Best-effort decode of the MQTT payload to a UTF-8 string."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, bytes | bytearray):
        return bytes(raw).decode("utf-8", errors="replace")
    return str(raw)


async def _dispatch(
    *,
    msg: aiomqtt.Message,
    ez1: EZ1Client,
    publisher: MQTTPublisher,
    device_info: DeviceInfo,
    settings: Settings,
) -> None:
    """Route a single message to the matching handler."""
    topic_str = str(msg.topic)
    command_name = parse_command_topic(
        topic_str,
        settings.mqtt_base_topic,
        device_info.device_id,
    )
    if command_name is None or command_name not in _KNOWN_COMMANDS:
        _log.warning("unknown_command_topic", topic=topic_str)
        return

    payload = _decode_payload(msg.payload)
    _log.info("command_received", command=command_name, payload=payload)

    if command_name == "max_power":
        await handle_max_power(
            payload,
            ez1=ez1,
            publisher=publisher,
            device_info=device_info,
            verify=settings.setmaxpower_verify,
        )
    elif command_name == "on_off":
        await handle_on_off(payload, ez1=ez1, publisher=publisher)


# --- Top-level loop ----------------------------------------------------


async def command_loop(
    *,
    client: aiomqtt.Client,
    ez1: EZ1Client,
    publisher: MQTTPublisher,
    device_info: DeviceInfo,
    settings: Settings,
    stop_event: asyncio.Event,
) -> None:
    """Subscribe to ``set/+`` and dispatch commands until cancelled.

    The loop exits via two paths:

    * ``stop_event`` is observed between messages -- the next iteration
      sees it set and returns. Reasonable when commands keep arriving.
    * The task is cancelled by :func:`run_service` after ``stop_event``
      fires while no message is in flight. The ``async for`` raises
      :class:`asyncio.CancelledError` and propagates cleanly.
    """
    subscription = topics.command_wildcard(
        settings.mqtt_base_topic,
        device_info.device_id,
    )
    await client.subscribe(subscription, qos=1)
    _log.info("command_loop_subscribed", topic=subscription)

    async for msg in client.messages:
        if stop_event.is_set():
            return
        try:
            await _dispatch(
                msg=msg,
                ez1=ez1,
                publisher=publisher,
                device_info=device_info,
                settings=settings,
            )
        except Exception:
            # A handler-level catastrophe must not kill the whole loop;
            # the result topic already conveys per-command failures.
            _log.warning("command_dispatch_failed", exc_info=True)
            # Best-effort: emit a generic failure on the (best-guess)
            # result topic so HA does not silently drop the command.
            await _emit_dispatch_failure(msg, publisher, settings, device_info)


async def _emit_dispatch_failure(
    msg: aiomqtt.Message,
    publisher: MQTTPublisher,
    settings: Settings,
    device_info: DeviceInfo,
) -> None:
    """Publish a generic transport_error result on dispatch-loop failure."""
    command_name = parse_command_topic(
        str(msg.topic),
        settings.mqtt_base_topic,
        device_info.device_id,
    )
    if command_name not in _KNOWN_COMMANDS:
        return
    fallback = CommandResult(
        ok=False,
        ts=datetime.now(tz=UTC),
        error="transport_error",
        detail="dispatch loop raised an unexpected exception",
    )
    payload = json_lib.loads(fallback.model_dump_json(exclude_none=True))
    try:
        await publisher.publish_result(command_name, payload)
    except Exception:
        _log.error("failed_to_publish_dispatch_failure", exc_info=True)
