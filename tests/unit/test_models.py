"""Tests for the immutable domain models in :mod:`ez1_bridge.domain.models`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ez1_bridge.domain.models import (
    AlarmFlags,
    EnergyReading,
    InverterState,
    PowerReading,
)

# --- PowerReading -------------------------------------------------------


def test_power_reading_total_is_sum_of_channels() -> None:
    p = PowerReading(ch1_w=139.0, ch2_w=65.0)
    assert p.total_w == pytest.approx(204.0)


def test_power_reading_total_handles_zero() -> None:
    p = PowerReading(ch1_w=0.0, ch2_w=0.0)
    assert p.total_w == 0.0


def test_power_reading_rejects_negative_values() -> None:
    with pytest.raises(ValidationError):
        PowerReading(ch1_w=-1.0, ch2_w=0.0)
    with pytest.raises(ValidationError):
        PowerReading(ch1_w=0.0, ch2_w=-1.0)


def test_power_reading_strict_rejects_string_input() -> None:
    with pytest.raises(ValidationError):
        PowerReading(ch1_w="139", ch2_w=65)  # type: ignore[arg-type]


def test_power_reading_is_frozen() -> None:
    p = PowerReading(ch1_w=1.0, ch2_w=2.0)
    with pytest.raises(ValidationError):
        p.ch1_w = 5.0


def test_power_reading_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        PowerReading(ch1_w=1.0, ch2_w=2.0, ch3_w=3.0)  # type: ignore[call-arg]


# --- EnergyReading -----------------------------------------------------


def test_energy_reading_total_rounds_to_five_decimals() -> None:
    e = EnergyReading(ch1_kwh=0.28731, ch2_kwh=0.42653)
    assert e.total_kwh == pytest.approx(0.71384, abs=1e-6)


def test_energy_reading_total_avoids_float_drift() -> None:
    # 0.1 + 0.2 == 0.30000000000000004 in IEEE-754; rounding pins it at 0.3.
    e = EnergyReading(ch1_kwh=0.1, ch2_kwh=0.2)
    assert e.total_kwh == 0.3


def test_energy_reading_rejects_negative_values() -> None:
    with pytest.raises(ValidationError):
        EnergyReading(ch1_kwh=-0.1, ch2_kwh=0.0)


def test_energy_reading_is_frozen() -> None:
    e = EnergyReading(ch1_kwh=1.0, ch2_kwh=2.0)
    with pytest.raises(ValidationError):
        e.ch1_kwh = 5.0


# --- AlarmFlags ---------------------------------------------------------


def test_alarm_flags_all_clear() -> None:
    a = AlarmFlags(off_grid=False, output_fault=False, dc1_short=False, dc2_short=False)
    assert not a.off_grid
    assert not a.output_fault
    assert not a.dc1_short
    assert not a.dc2_short
    assert not a.any_active


@pytest.mark.parametrize(
    "field",
    ["off_grid", "output_fault", "dc1_short", "dc2_short"],
)
def test_alarm_flags_any_active_triggers_for_each_bit(field: str) -> None:
    kwargs = {"off_grid": False, "output_fault": False, "dc1_short": False, "dc2_short": False}
    kwargs[field] = True
    a = AlarmFlags(**kwargs)
    assert a.any_active is True


def test_alarm_flags_strict_rejects_string_input() -> None:
    with pytest.raises(ValidationError):
        AlarmFlags(
            off_grid="0",  # type: ignore[arg-type]
            output_fault=False,
            dc1_short=False,
            dc2_short=False,
        )


def test_alarm_flags_is_frozen() -> None:
    a = AlarmFlags(off_grid=False, output_fault=False, dc1_short=False, dc2_short=False)
    with pytest.raises(ValidationError):
        a.off_grid = True


# --- InverterState -----------------------------------------------------


def _state(**overrides: object) -> InverterState:
    base: dict[str, object] = {
        "ts": datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
        "device_id": "E17010000783",
        "power": PowerReading(ch1_w=139.0, ch2_w=65.0),
        "energy_today": EnergyReading(ch1_kwh=0.28731, ch2_kwh=0.42653),
        "energy_lifetime": EnergyReading(ch1_kwh=87.43068, ch2_kwh=111.24305),
        "max_power_w": 800,
        "status": "on",
        "alarms": AlarmFlags(off_grid=False, output_fault=False, dc1_short=False, dc2_short=False),
    }
    base.update(overrides)
    return InverterState(**base)  # type: ignore[arg-type]


def test_inverter_state_construction() -> None:
    s = _state()
    assert s.device_id == "E17010000783"
    assert s.power.total_w == pytest.approx(204.0)
    assert s.energy_today.total_kwh == pytest.approx(0.71384, abs=1e-6)
    assert s.energy_lifetime.total_kwh == pytest.approx(198.67373, abs=1e-6)
    assert s.max_power_w == 800
    assert s.status == "on"


def test_inverter_state_status_off_accepted() -> None:
    assert _state(status="off").status == "off"


def test_inverter_state_invalid_status_rejected() -> None:
    with pytest.raises(ValidationError):
        _state(status="paused")


def test_inverter_state_empty_device_id_rejected() -> None:
    with pytest.raises(ValidationError):
        _state(device_id="")


def test_inverter_state_negative_max_power_rejected() -> None:
    with pytest.raises(ValidationError):
        _state(max_power_w=-1)


def test_inverter_state_is_frozen() -> None:
    s = _state()
    with pytest.raises(ValidationError):
        s.max_power_w = 600


def test_inverter_state_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        _state(unexpected="oops")


def test_inverter_state_serialises_to_dict() -> None:
    s = _state()
    dumped = s.model_dump()
    assert dumped["status"] == "on"
    assert dumped["power"]["total_w"] == pytest.approx(204.0)
    assert dumped["alarms"]["any_active"] is False
