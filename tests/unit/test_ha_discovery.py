"""Tests for :mod:`ez1_bridge.application.ha_discovery`."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from ez1_bridge.application.ha_discovery import (
    DiscoveryMessage,
    build_discovery_messages,
)
from ez1_bridge.domain.models import DeviceInfo


@pytest.fixture
def device() -> DeviceInfo:
    return DeviceInfo(
        device_id="E17010000783",
        firmware_version="EZ1 1.12.2t",
        ssid="my-wlan",
        ip_address="192.168.3.24",
        min_power_w=30,
        max_power_w=800,
    )


@pytest.fixture
def messages(device: DeviceInfo) -> list[DiscoveryMessage]:
    return build_discovery_messages(
        device,
        base_topic="ez1",
        discovery_prefix="homeassistant",
    )


# --- Cardinality + structure ------------------------------------------


def test_total_count_is_eleven_sensors_plus_four_binary(
    messages: list[DiscoveryMessage],
) -> None:
    sensor = [m for m in messages if "/sensor/" in m.topic]
    binary = [m for m in messages if "/binary_sensor/" in m.topic]
    assert len(sensor) == 11
    assert len(binary) == 4
    assert len(messages) == 15


def test_all_messages_are_retained(messages: list[DiscoveryMessage]) -> None:
    for m in messages:
        assert m.retain is True


def test_topics_carry_discovery_prefix_and_device_id(
    messages: list[DiscoveryMessage],
) -> None:
    for m in messages:
        assert m.topic.startswith("homeassistant/")
        assert "/E17010000783/" in m.topic
        assert m.topic.endswith("/config")


def test_custom_prefix_and_base(device: DeviceInfo) -> None:
    msgs = build_discovery_messages(
        device,
        base_topic="solar",
        discovery_prefix="ha",
    )
    for m in msgs:
        assert m.topic.startswith("ha/")
    state_topics = {m.payload["state_topic"] for m in msgs}
    assert state_topics == {"solar/E17010000783/state"}


def test_topic_order_is_stable(messages: list[DiscoveryMessage]) -> None:
    """First sensor section, then binary-sensor section, both in declaration order."""
    topics_in_order = [m.topic for m in messages]
    sensor_section = [t for t in topics_in_order if "/sensor/" in t]
    binary_section = [t for t in topics_in_order if "/binary_sensor/" in t]
    assert topics_in_order == sensor_section + binary_section


# --- Payload contents -------------------------------------------------


def test_unique_id_pattern(messages: list[DiscoveryMessage]) -> None:
    for m in messages:
        unique_id = m.payload["unique_id"]
        assert unique_id.startswith("ez1_E17010000783_")
        assert m.payload["object_id"] == unique_id


def test_every_payload_has_state_topic(messages: list[DiscoveryMessage]) -> None:
    expected = "ez1/E17010000783/state"
    for m in messages:
        assert m.payload["state_topic"] == expected


def test_every_payload_has_availability_block(messages: list[DiscoveryMessage]) -> None:
    expected_topic = "ez1/E17010000783/availability"
    for m in messages:
        assert m.payload["availability_topic"] == expected_topic
        assert m.payload["payload_available"] == "online"
        assert m.payload["payload_not_available"] == "offline"


def test_device_block_carries_firmware(messages: list[DiscoveryMessage]) -> None:
    for m in messages:
        device_block = m.payload["device"]
        assert device_block["identifiers"] == ["E17010000783"]
        assert device_block["manufacturer"] == "APsystems"
        assert device_block["model"] == "EZ1"
        assert device_block["sw_version"] == "EZ1 1.12.2t"


def test_power_sensors_are_watts(messages: list[DiscoveryMessage]) -> None:
    for m in messages:
        if m.topic.endswith(("/power_ch1/config", "/power_ch2/config", "/power_total/config")):
            assert m.payload["unit_of_measurement"] == "W"
            assert m.payload["device_class"] == "power"
            assert m.payload["state_class"] == "measurement"


def test_energy_sensors_are_kwh_total_increasing(messages: list[DiscoveryMessage]) -> None:
    for m in messages:
        if "/energy_" in m.topic and m.topic.endswith("/config"):
            assert m.payload["unit_of_measurement"] == "kWh"
            assert m.payload["device_class"] == "energy"
            assert m.payload["state_class"] == "total_increasing"


def test_max_power_is_diagnostic(messages: list[DiscoveryMessage]) -> None:
    matching = [m for m in messages if m.topic.endswith("/max_power/config")]
    assert len(matching) == 1
    assert matching[0].payload["entity_category"] == "diagnostic"


def test_status_sensor_has_no_unit(messages: list[DiscoveryMessage]) -> None:
    matching = [m for m in messages if m.topic.endswith("/status/config")]
    assert len(matching) == 1
    payload = matching[0].payload
    assert "unit_of_measurement" not in payload
    assert payload["icon"] == "mdi:power"


def test_binary_sensors_have_problem_device_class(
    messages: list[DiscoveryMessage],
) -> None:
    binary = [m for m in messages if "/binary_sensor/" in m.topic]
    for m in binary:
        assert m.payload["device_class"] == "problem"


def test_binary_sensor_value_templates_render_on_off(
    messages: list[DiscoveryMessage],
) -> None:
    binary = [m for m in messages if "/binary_sensor/" in m.topic]
    for m in binary:
        template = m.payload["value_template"]
        assert "ON" in template
        assert "OFF" in template
        assert "value_json.alarms" in template


def test_value_templates_resolve_against_state_json(
    messages: list[DiscoveryMessage],
) -> None:
    """Every value_template references value_json — guards against a
    refactor that switches to a different MQTT shape without updating
    discovery.
    """
    for m in messages:
        assert "value_json" in m.payload["value_template"]


# --- DiscoveryMessage dataclass --------------------------------------


def test_discovery_message_is_frozen() -> None:
    m = DiscoveryMessage(topic="x", payload={}, retain=True)
    with pytest.raises(FrozenInstanceError):
        m.topic = "y"  # type: ignore[misc]
