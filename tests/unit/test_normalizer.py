"""Tests for :mod:`ez1_bridge.domain.normalizer`.

The verified-payload fixtures from ``tests/fixtures/api_responses/``
double as integration smoke for the normalizer: anything Phase 2 will
hit on the wire from a healthy inverter must round-trip cleanly here
first.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Literal

import pytest

from ez1_bridge.domain.models import (
    AlarmFlags,
    DeviceInfo,
    EnergyReading,
    InverterState,
    PowerReading,
)
from ez1_bridge.domain.normalizer import (
    _STATUS_MAP,
    _bit_to_bool,
    _expect_success,
    _to_int_watt,
    build_state,
    parse_alarms,
    parse_device_id,
    parse_device_info,
    parse_max_power_w,
    parse_output_data,
    parse_status,
)

# --- _STATUS_MAP --------------------------------------------------------


@pytest.mark.parametrize(
    ("wire", "human"),
    [("0", "on"), ("1", "off")],
)
def test_status_map_table_driven(wire: str, human: Literal["on", "off"]) -> None:
    """Inverted on/off semantics — guard rail against future direction-flips."""
    assert _STATUS_MAP[wire] == human


def test_status_map_is_complete() -> None:
    assert set(_STATUS_MAP.keys()) == {"0", "1"}
    assert set(_STATUS_MAP.values()) == {"on", "off"}


# --- _to_int_watt -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("800", 800), ("30", 30), ("0", 0), ("  600  ", 600)],
)
def test_to_int_watt_accepts_clean_integer_strings(raw: str, expected: int) -> None:
    assert _to_int_watt(raw) == expected


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "800W", "800.0", "0x100", "abc", "8 0 0", "--1"],
)
def test_to_int_watt_rejects_non_integer_input(bad: str) -> None:
    with pytest.raises(ValueError, match="watt value"):
        _to_int_watt(bad)


# --- _expect_success ---------------------------------------------------


def test_expect_success_returns_data_on_success() -> None:
    envelope = {"data": {"x": 1}, "message": "SUCCESS", "deviceId": "E1"}
    result = _expect_success(envelope, "test")
    assert result == {"x": 1}


def test_expect_success_rejects_failed_message() -> None:
    envelope = {"data": {}, "message": "FAILED", "deviceId": "E1"}
    with pytest.raises(ValueError, match="non-success"):
        _expect_success(envelope, "getOutputData")


def test_expect_success_rejects_missing_data() -> None:
    envelope = {"message": "SUCCESS", "deviceId": "E1"}
    with pytest.raises(ValueError, match="malformed"):
        _expect_success(envelope, "getAlarm")


def test_expect_success_rejects_non_mapping_data() -> None:
    envelope = {"data": "not a mapping", "message": "SUCCESS", "deviceId": "E1"}
    with pytest.raises(ValueError, match="malformed"):
        _expect_success(envelope, "getAlarm")


# --- _bit_to_bool ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0", False), ("1", True)],
)
def test_bit_to_bool_accepts_zero_one_strings(raw: str, expected: bool) -> None:
    assert _bit_to_bool(raw, "test") is expected


@pytest.mark.parametrize("bad", ["2", "true", "", 0, 1, None])
def test_bit_to_bool_rejects_anything_else(bad: object) -> None:
    with pytest.raises(ValueError, match="must be '0' or '1'"):
        _bit_to_bool(bad, "og")


# --- parse_device_id ---------------------------------------------------


def test_parse_device_id_extracts_top_level_field(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    envelope = api_response("get_output_data")
    assert parse_device_id(envelope) == "E17010000783"


def test_parse_device_id_rejects_missing_field() -> None:
    with pytest.raises(ValueError, match="deviceId"):
        parse_device_id({"data": {}, "message": "SUCCESS"})


def test_parse_device_id_rejects_empty_string() -> None:
    with pytest.raises(ValueError, match="deviceId"):
        parse_device_id({"deviceId": ""})


# --- parse_status ------------------------------------------------------


def test_parse_status_round_trips_real_payload(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    envelope = api_response("get_on_off")
    assert parse_status(envelope) == "on"


def test_parse_status_off(api_response: Callable[[str], dict[str, Any]]) -> None:
    envelope = api_response("get_on_off")
    envelope["data"]["status"] = "1"
    assert parse_status(envelope) == "off"


def test_parse_status_rejects_unknown_value(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    envelope = api_response("get_on_off")
    envelope["data"]["status"] = "2"
    with pytest.raises(ValueError, match="unknown status"):
        parse_status(envelope)


def test_parse_status_rejects_non_string(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    envelope = api_response("get_on_off")
    envelope["data"]["status"] = 0
    with pytest.raises(ValueError, match="unknown status"):
        parse_status(envelope)


# --- parse_max_power_w -------------------------------------------------


def test_parse_max_power_w_round_trips_real_payload(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    envelope = api_response("get_max_power")
    assert parse_max_power_w(envelope) == 800


def test_parse_max_power_w_rejects_non_string(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    envelope = api_response("get_max_power")
    envelope["data"]["maxPower"] = 800
    with pytest.raises(ValueError, match="must be a string"):
        parse_max_power_w(envelope)


def test_parse_max_power_w_rejects_garbage_string(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    envelope = api_response("get_max_power")
    envelope["data"]["maxPower"] = "800W"
    with pytest.raises(ValueError, match="watt value"):
        parse_max_power_w(envelope)


# --- parse_output_data -------------------------------------------------


def test_parse_output_data_round_trips_real_payload(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    envelope = api_response("get_output_data")
    power, today, lifetime = parse_output_data(envelope)
    assert power == PowerReading(ch1_w=139.0, ch2_w=65.0)
    assert today == EnergyReading(ch1_kwh=0.28731, ch2_kwh=0.42653)
    assert lifetime == EnergyReading(ch1_kwh=87.43068, ch2_kwh=111.24305)


def test_parse_output_data_rejects_missing_key(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    envelope = deepcopy(api_response("get_output_data"))
    del envelope["data"]["p1"]
    with pytest.raises(ValueError, match="missing key"):
        parse_output_data(envelope)


def test_parse_output_data_handles_zero_values_at_night(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    envelope = deepcopy(api_response("get_output_data"))
    envelope["data"].update({"p1": 0, "p2": 0, "e1": 0, "e2": 0})
    power, today, _ = parse_output_data(envelope)
    assert power.total_w == 0
    assert today.total_kwh == 0


# --- parse_alarms ------------------------------------------------------


def test_parse_alarms_all_clear(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    envelope = api_response("get_alarm")
    alarms = parse_alarms(envelope)
    assert alarms == AlarmFlags(
        off_grid=False, output_fault=False, dc1_short=False, dc2_short=False
    )


@pytest.mark.parametrize(
    ("wire_key", "model_field"),
    [
        ("og", "off_grid"),
        ("oe", "output_fault"),
        ("isce1", "dc1_short"),
        ("isce2", "dc2_short"),
    ],
)
def test_parse_alarms_each_bit_maps_to_correct_field(
    api_response: Callable[[str], dict[str, Any]],
    wire_key: str,
    model_field: str,
) -> None:
    envelope = deepcopy(api_response("get_alarm"))
    envelope["data"][wire_key] = "1"
    alarms = parse_alarms(envelope)
    assert getattr(alarms, model_field) is True
    assert alarms.any_active is True


def test_parse_alarms_rejects_garbage_bit(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    envelope = deepcopy(api_response("get_alarm"))
    envelope["data"]["og"] = "yes"
    with pytest.raises(ValueError, match="must be '0' or '1'"):
        parse_alarms(envelope)


# --- parse_device_info -------------------------------------------------


def test_parse_device_info_round_trips_real_payload(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    envelope = api_response("get_device_info")
    info = parse_device_info(envelope)
    assert isinstance(info, DeviceInfo)
    assert info.device_id == "E17010000783"
    assert info.firmware_version == "EZ1 1.12.2t"
    assert info.ip_address == "192.168.3.24"
    assert info.min_power_w == 30
    assert info.max_power_w == 800


def test_parse_device_info_coerces_string_power_bounds_explicitly(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    """Watt fields are strings on the wire; _to_int_watt must reject garbage."""
    envelope = deepcopy(api_response("get_device_info"))
    envelope["data"]["maxPower"] = "800W"
    with pytest.raises(ValueError, match="watt value"):
        parse_device_info(envelope)


def test_parse_device_info_rejects_failed_envelope(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    envelope = deepcopy(api_response("get_device_info"))
    envelope["message"] = "FAILED"
    with pytest.raises(ValueError, match="non-success"):
        parse_device_info(envelope)


def test_parse_device_info_rejects_missing_required_field(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    envelope = deepcopy(api_response("get_device_info"))
    del envelope["data"]["devVer"]
    with pytest.raises(ValueError, match="missing key"):
        parse_device_info(envelope)


def test_parse_device_info_allows_empty_ssid(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    envelope = deepcopy(api_response("get_device_info"))
    envelope["data"]["ssid"] = ""
    info = parse_device_info(envelope)
    assert info.ssid == ""


def test_parse_device_info_rejects_non_string_field(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    envelope = deepcopy(api_response("get_device_info"))
    envelope["data"]["devVer"] = 123
    with pytest.raises(ValueError, match="devVer must be a string"):
        parse_device_info(envelope)


def test_parse_device_info_rejects_empty_required_field(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    envelope = deepcopy(api_response("get_device_info"))
    envelope["data"]["deviceId"] = ""
    with pytest.raises(ValueError, match="deviceId must be non-empty"):
        parse_device_info(envelope)


# --- build_state -------------------------------------------------------


def test_build_state_aggregates_real_payloads(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    fixed_ts = datetime(2026, 4, 26, 18, 0, tzinfo=UTC)
    state = build_state(
        output_data=api_response("get_output_data"),
        max_power=api_response("get_max_power"),
        alarm=api_response("get_alarm"),
        on_off=api_response("get_on_off"),
        ts=fixed_ts,
    )
    assert isinstance(state, InverterState)
    assert state.ts == fixed_ts
    assert state.device_id == "E17010000783"
    assert state.power.ch1_w == 139.0
    assert state.power.ch2_w == 65.0
    assert state.power.total_w == 204.0
    assert state.energy_today.total_kwh == pytest.approx(0.71384, abs=1e-6)
    assert state.energy_lifetime.total_kwh == pytest.approx(198.67373, abs=1e-6)
    assert state.max_power_w == 800
    assert state.status == "on"
    assert state.alarms.any_active is False


def test_build_state_defaults_timestamp_to_now_utc(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    before = datetime.now(tz=UTC)
    state = build_state(
        output_data=api_response("get_output_data"),
        max_power=api_response("get_max_power"),
        alarm=api_response("get_alarm"),
        on_off=api_response("get_on_off"),
    )
    after = datetime.now(tz=UTC)
    assert before <= state.ts <= after
    assert state.ts.tzinfo == UTC


def test_build_state_propagates_failed_envelope(
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    bad = deepcopy(api_response("get_max_power"))
    bad["message"] = "FAILED"
    with pytest.raises(ValueError, match="non-success"):
        build_state(
            output_data=api_response("get_output_data"),
            max_power=bad,
            alarm=api_response("get_alarm"),
            on_off=api_response("get_on_off"),
        )
