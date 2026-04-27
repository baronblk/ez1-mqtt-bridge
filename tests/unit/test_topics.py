"""Tests for :mod:`ez1_bridge.topics`."""

from __future__ import annotations

from ez1_bridge import topics

# --- Builders ----------------------------------------------------------


def test_availability_topic() -> None:
    assert topics.availability("ez1", "E17010000783") == "ez1/E17010000783/availability"


def test_state_topic() -> None:
    assert topics.state("ez1", "E17010000783") == "ez1/E17010000783/state"


def test_flat_topic() -> None:
    assert (
        topics.flat("ez1", "E17010000783", "power", "total_w") == "ez1/E17010000783/power/total_w"
    )


def test_command_topic() -> None:
    assert topics.command("ez1", "E17010000783", "max_power") == "ez1/E17010000783/set/max_power"


def test_command_wildcard() -> None:
    assert topics.command_wildcard("ez1", "E17010000783") == "ez1/E17010000783/set/+"


def test_result_topic() -> None:
    assert topics.result("ez1", "E17010000783", "on_off") == "ez1/E17010000783/result/on_off"


def test_discovery_topic_sensor() -> None:
    assert (
        topics.discovery("homeassistant", "sensor", "E17010000783", "power_total")
        == "homeassistant/sensor/E17010000783/power_total/config"
    )


def test_discovery_topic_binary_sensor() -> None:
    assert (
        topics.discovery(
            "homeassistant",
            "binary_sensor",
            "E17010000783",
            "alarm_off_grid",
        )
        == "homeassistant/binary_sensor/E17010000783/alarm_off_grid/config"
    )


def test_custom_base_and_prefix() -> None:
    assert topics.availability("solar", "E1") == "solar/E1/availability"
    assert topics.discovery("ha", "sensor", "E1", "p1") == "ha/sensor/E1/p1/config"


# --- Retain semantics --------------------------------------------------


def test_retain_map_covers_expected_kinds() -> None:
    assert set(topics.RETAIN.keys()) == {
        "availability",
        "state",
        "flat",
        "result",
        "discovery",
    }


def test_retain_state_topics_are_retained() -> None:
    assert topics.RETAIN["availability"] is True
    assert topics.RETAIN["state"] is True
    assert topics.RETAIN["flat"] is True
    assert topics.RETAIN["discovery"] is True


def test_retain_result_is_not_retained() -> None:
    """Results are events, not state — retaining them would mislead late subscribers."""
    assert topics.RETAIN["result"] is False


# --- Availability payload constants -----------------------------------


def test_availability_payloads_are_distinct() -> None:
    assert topics.AVAILABILITY_ONLINE == "online"
    assert topics.AVAILABILITY_OFFLINE == "offline"
    assert topics.AVAILABILITY_ONLINE != topics.AVAILABILITY_OFFLINE
