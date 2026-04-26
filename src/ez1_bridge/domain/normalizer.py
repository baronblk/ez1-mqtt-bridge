"""Normalize raw EZ1 API payloads into :class:`InverterState` domain models.

Each ``parse_*`` function takes the full envelope returned by the EZ1
(``{"data": {...}, "message": "...", "deviceId": "..."}``), validates the
``message`` field, and extracts the parts the rest of the system cares
about. :func:`build_state` aggregates four endpoint responses plus a
timestamp into a single :class:`InverterState`.

Two design choices worth flagging:

1. **The on/off semantics on the wire are inverted.** ``status="0"`` means
   *on*, ``status="1"`` means *off*. The mapping lives in a single
   module-level constant :data:`_STATUS_MAP` so future readers and
   refactorings cannot drift the direction.
2. **Power values are coerced explicitly, not implicitly.** Pydantic v2
   would happily turn ``"800"`` into an int, but it would also accept
   ``"800.0"`` or ``"800W"`` if a future EZ1 firmware decided to emit
   them. :func:`_to_int_watt` rejects anything that is not a clean
   integer string.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final, Literal

from ez1_bridge.domain.models import (
    AlarmFlags,
    EnergyReading,
    InverterState,
    PowerReading,
)

#: Map the inverter's wire-level on/off bit to a human-readable literal.
#: ``0`` is *on*, ``1`` is *off* — this is opposite to most intuitions and
#: is the EZ1's spec, not a typo.
_STATUS_MAP: Final[Mapping[str, Literal["on", "off"]]] = {
    "0": "on",
    "1": "off",
}

_SUCCESS: Final[str] = "SUCCESS"


# --- helpers ------------------------------------------------------------


def _to_int_watt(value: str) -> int:
    """Parse a watt value from the EZ1 API into an integer.

    Defends against firmware updates that might emit ``"800W"``,
    ``"800.0"``, or other non-clean integer strings by failing fast with
    :class:`ValueError` instead of silently coercing.

    Args:
        value: Raw string from the API response, expected to be a clean
            decimal integer with no units or whitespace surprises.

    Raises:
        ValueError: If ``value`` is empty, not numeric, or contains a
            decimal point or unit suffix.
    """
    stripped = value.strip()
    if not stripped:
        raise ValueError("empty watt value")
    try:
        return int(stripped)
    except ValueError as exc:
        msg = f"cannot parse watt value: {value!r}"
        raise ValueError(msg) from exc


def _expect_success(envelope: Mapping[str, Any], endpoint: str) -> Mapping[str, Any]:
    """Validate an EZ1 envelope and return the inner ``data`` field.

    Raises:
        ValueError: If ``message`` is not ``"SUCCESS"`` or ``data`` is
            missing or not a mapping.
    """
    message = envelope.get("message")
    if message != _SUCCESS:
        msg = f"{endpoint}: non-success message: {message!r}"
        raise ValueError(msg)
    data = envelope.get("data")
    if not isinstance(data, Mapping):
        msg = f"{endpoint}: missing or malformed `data` field"
        raise ValueError(msg)
    return data


def _bit_to_bool(raw: object, key: str) -> bool:
    """Convert the EZ1's ``"0"``/``"1"`` alarm bit into a Python bool.

    Raises:
        ValueError: If ``raw`` is anything other than ``"0"`` or ``"1"``.
    """
    if raw == "0":
        return False
    if raw == "1":
        return True
    msg = f"alarm bit {key!r} must be '0' or '1', got {raw!r}"
    raise ValueError(msg)


# --- parsers per endpoint ----------------------------------------------


def parse_device_id(envelope: Mapping[str, Any]) -> str:
    """Return the top-level ``deviceId`` field from any EZ1 envelope."""
    device_id = envelope.get("deviceId")
    if not isinstance(device_id, str) or not device_id:
        msg = f"missing or empty deviceId: {device_id!r}"
        raise ValueError(msg)
    return device_id


def parse_status(envelope: Mapping[str, Any]) -> Literal["on", "off"]:
    """Map a ``getOnOff`` response to ``"on"`` / ``"off"``.

    Inverted on the wire: ``"0"`` is on, ``"1"`` is off.
    """
    data = _expect_success(envelope, "getOnOff")
    raw = data.get("status")
    if not isinstance(raw, str) or raw not in _STATUS_MAP:
        msg = f"getOnOff: unknown status: {raw!r}"
        raise ValueError(msg)
    return _STATUS_MAP[raw]


def parse_max_power_w(envelope: Mapping[str, Any]) -> int:
    """Coerce ``getMaxPower``'s string value to an int."""
    data = _expect_success(envelope, "getMaxPower")
    raw = data.get("maxPower")
    if not isinstance(raw, str):
        msg = f"getMaxPower: maxPower must be a string, got {type(raw).__name__}"
        raise ValueError(msg)
    return _to_int_watt(raw)


def parse_output_data(
    envelope: Mapping[str, Any],
) -> tuple[PowerReading, EnergyReading, EnergyReading]:
    """Return ``(power, energy_today, energy_lifetime)`` from ``getOutputData``."""
    data = _expect_success(envelope, "getOutputData")
    try:
        power = PowerReading(ch1_w=float(data["p1"]), ch2_w=float(data["p2"]))
        today = EnergyReading(ch1_kwh=float(data["e1"]), ch2_kwh=float(data["e2"]))
        lifetime = EnergyReading(ch1_kwh=float(data["te1"]), ch2_kwh=float(data["te2"]))
    except KeyError as exc:
        msg = f"getOutputData: missing key {exc.args[0]!r}"
        raise ValueError(msg) from exc
    return power, today, lifetime


def parse_alarms(envelope: Mapping[str, Any]) -> AlarmFlags:
    """Convert ``getAlarm``'s four string bits into a typed :class:`AlarmFlags`."""
    data = _expect_success(envelope, "getAlarm")
    return AlarmFlags(
        off_grid=_bit_to_bool(data.get("og"), "og"),
        output_fault=_bit_to_bool(data.get("oe"), "oe"),
        dc1_short=_bit_to_bool(data.get("isce1"), "isce1"),
        dc2_short=_bit_to_bool(data.get("isce2"), "isce2"),
    )


# --- aggregation -------------------------------------------------------


def build_state(
    *,
    output_data: Mapping[str, Any],
    max_power: Mapping[str, Any],
    alarm: Mapping[str, Any],
    on_off: Mapping[str, Any],
    ts: datetime | None = None,
) -> InverterState:
    """Aggregate the four poll-cycle endpoints into a single :class:`InverterState`.

    Args:
        output_data: Envelope from ``GET /getOutputData``.
        max_power: Envelope from ``GET /getMaxPower``.
        alarm: Envelope from ``GET /getAlarm``.
        on_off: Envelope from ``GET /getOnOff``.
        ts: Snapshot timestamp. Defaults to ``datetime.now(tz=UTC)``.
    """
    power, energy_today, energy_lifetime = parse_output_data(output_data)
    return InverterState(
        ts=ts if ts is not None else datetime.now(tz=UTC),
        device_id=parse_device_id(output_data),
        power=power,
        energy_today=energy_today,
        energy_lifetime=energy_lifetime,
        max_power_w=parse_max_power_w(max_power),
        status=parse_status(on_off),
        alarms=parse_alarms(alarm),
    )
