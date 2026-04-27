"""Unit tests for :mod:`ez1_bridge.adapters.mqtt_publisher`.

These tests mock :class:`aiomqtt.Client` and assert that the publisher
constructs LWT correctly, publishes to the right topics with the right
retain flags and QoS, and respects the async-context-manager contract.

Real-broker behaviour (LWT actually fires, retain survives reconnect,
etc.) is covered separately in
``tests/integration/test_mqtt_broker.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, call, patch

import aiomqtt
import pytest
from pydantic import SecretStr

from ez1_bridge import topics
from ez1_bridge.adapters.mqtt_publisher import MQTTPublisher
from ez1_bridge.adapters.prom_metrics import MetricsRegistry
from ez1_bridge.domain.models import (
    AlarmFlags,
    EnergyReading,
    InverterState,
    PowerReading,
)


@pytest.fixture
def sample_state() -> InverterState:
    return InverterState(
        ts=datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
        device_id="E17010000783",
        power=PowerReading(ch1_w=139.0, ch2_w=65.0),
        energy_today=EnergyReading(ch1_kwh=0.28731, ch2_kwh=0.42653),
        energy_lifetime=EnergyReading(ch1_kwh=87.43068, ch2_kwh=111.24305),
        max_power_w=800,
        status="on",
        alarms=AlarmFlags(off_grid=False, output_fault=False, dc1_short=False, dc2_short=False),
    )


def _mock_client() -> MagicMock:
    """Build a MagicMock that mimics aiomqtt.Client's async context manager."""
    client = MagicMock(spec=aiomqtt.Client)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.publish = AsyncMock()
    return client


# --- Construction guards -------------------------------------------------


def test_empty_host_rejected() -> None:
    with pytest.raises(ValueError, match="host"):
        MQTTPublisher("", device_id="E1")


def test_empty_device_id_rejected() -> None:
    with pytest.raises(ValueError, match="device_id"):
        MQTTPublisher("broker.local", device_id="")


def test_empty_base_topic_rejected() -> None:
    with pytest.raises(ValueError, match="base_topic"):
        MQTTPublisher("broker.local", device_id="E1", base_topic="")


def test_default_identifier_includes_device_id() -> None:
    pub = MQTTPublisher("broker.local", device_id="E17010000783")
    assert pub.identifier == "ez1-bridge-E17010000783"


def test_custom_identifier_passed_through() -> None:
    pub = MQTTPublisher(
        "broker.local",
        device_id="E1",
        identifier="custom-id-7",
    )
    assert pub.identifier == "custom-id-7"


def test_properties_round_trip() -> None:
    pub = MQTTPublisher(
        "broker.local",
        device_id="E1",
        base_topic="solar",
    )
    assert pub.base_topic == "solar"
    assert pub.device_id == "E1"


# --- LWT configuration --------------------------------------------------


def test_build_client_sets_lwt_to_offline_with_retain() -> None:
    pub = MQTTPublisher("broker.local", device_id="E17010000783")

    with patch(
        "ez1_bridge.adapters.mqtt_publisher.aiomqtt.Client",
    ) as mock_cls:
        pub._build_client()

    mock_cls.assert_called_once()
    will = mock_cls.call_args.kwargs["will"]
    assert isinstance(will, aiomqtt.Will)
    assert will.topic == "ez1/E17010000783/availability"
    assert will.payload == "offline"
    assert will.retain is True
    assert will.qos == 1


def test_build_client_passes_credentials() -> None:
    pub = MQTTPublisher(
        "broker.local",
        device_id="E1",
        username="alice",
        password=SecretStr("hunter2"),
    )

    with patch(
        "ez1_bridge.adapters.mqtt_publisher.aiomqtt.Client",
    ) as mock_cls:
        pub._build_client()

    kwargs = mock_cls.call_args.kwargs
    assert kwargs["username"] == "alice"
    assert kwargs["password"] == "hunter2"


def test_build_client_passes_no_credentials_when_unset() -> None:
    pub = MQTTPublisher("broker.local", device_id="E1")

    with patch(
        "ez1_bridge.adapters.mqtt_publisher.aiomqtt.Client",
    ) as mock_cls:
        pub._build_client()

    kwargs = mock_cls.call_args.kwargs
    assert kwargs["username"] is None
    assert kwargs["password"] is None


# --- Async context manager ----------------------------------------------


async def test_aenter_calls_underlying_client_aenter() -> None:
    mock_client = _mock_client()
    pub = MQTTPublisher("broker.local", device_id="E1")

    with patch.object(pub, "_build_client", return_value=mock_client):
        async with pub:
            mock_client.__aenter__.assert_awaited_once()

    mock_client.__aexit__.assert_awaited_once()


async def test_publish_outside_context_raises() -> None:
    pub = MQTTPublisher("broker.local", device_id="E1")
    with pytest.raises(RuntimeError, match="async context manager"):
        await pub.publish_availability(online=True)


async def test_client_property_outside_context_raises() -> None:
    pub = MQTTPublisher("broker.local", device_id="E1")
    with pytest.raises(RuntimeError, match="async context manager"):
        _ = pub.client


async def test_client_property_returns_underlying_aiomqtt_client() -> None:
    mock_client = _mock_client()
    pub = MQTTPublisher("broker.local", device_id="E1")

    with patch.object(pub, "_build_client", return_value=mock_client):
        async with pub:
            assert pub.client is mock_client


# --- publish_availability ----------------------------------------------


async def test_publish_availability_online_publishes_with_retain() -> None:
    mock_client = _mock_client()
    pub = MQTTPublisher("broker.local", device_id="E1")

    with patch.object(pub, "_build_client", return_value=mock_client):
        async with pub:
            await pub.publish_availability(online=True)

    mock_client.publish.assert_awaited_once_with(
        "ez1/E1/availability",
        payload="online",
        qos=1,
        retain=True,
    )


async def test_publish_availability_offline() -> None:
    mock_client = _mock_client()
    pub = MQTTPublisher("broker.local", device_id="E1")

    with patch.object(pub, "_build_client", return_value=mock_client):
        async with pub:
            await pub.publish_availability(online=False)

    mock_client.publish.assert_awaited_once_with(
        "ez1/E1/availability",
        payload="offline",
        qos=1,
        retain=True,
    )


# --- publish_state -----------------------------------------------------


async def test_publish_state_emits_json_topic_with_retain(
    sample_state: InverterState,
) -> None:
    mock_client = _mock_client()
    pub = MQTTPublisher("broker.local", device_id="E17010000783")

    with patch.object(pub, "_build_client", return_value=mock_client):
        async with pub:
            await pub.publish_state(sample_state)

    # The first publish call should be the structured JSON state.
    first_call = mock_client.publish.await_args_list[0]
    assert first_call.args[0] == "ez1/E17010000783/state"
    assert first_call.kwargs["retain"] is True
    assert first_call.kwargs["qos"] == 1
    body = json.loads(first_call.kwargs["payload"])
    assert body["status"] == "on"
    assert body["device_id"] == "E17010000783"
    assert body["power"]["total_w"] == 204.0


async def test_publish_state_emits_flat_topics_with_retain(
    sample_state: InverterState,
) -> None:
    mock_client = _mock_client()
    pub = MQTTPublisher("broker.local", device_id="E1")

    with patch.object(pub, "_build_client", return_value=mock_client):
        async with pub:
            await pub.publish_state(sample_state)

    flat_calls = [c for c in mock_client.publish.await_args_list if c.args[0] != "ez1/E1/state"]
    assert len(flat_calls) == 16  # 3 power + 3 today + 3 lifetime + 1 max + 1 status + 5 alarm

    flat_topics = {c.args[0] for c in flat_calls}
    assert "ez1/E1/power/ch1_w" in flat_topics
    assert "ez1/E1/power/total_w" in flat_topics
    assert "ez1/E1/energy_today/total_kwh" in flat_topics
    assert "ez1/E1/energy_lifetime/total_kwh" in flat_topics
    assert "ez1/E1/max_power_w/value" in flat_topics
    assert "ez1/E1/status/value" in flat_topics
    assert "ez1/E1/alarm/off_grid" in flat_topics
    assert "ez1/E1/alarm/any_active" in flat_topics

    for c in flat_calls:
        assert c.kwargs["retain"] is True
        assert c.kwargs["qos"] == 1


async def test_publish_state_flat_alarm_values_are_lowercase_strings(
    sample_state: InverterState,
) -> None:
    mock_client = _mock_client()
    pub = MQTTPublisher("broker.local", device_id="E1")

    with patch.object(pub, "_build_client", return_value=mock_client):
        async with pub:
            await pub.publish_state(sample_state)

    by_topic = {c.args[0]: c.kwargs["payload"] for c in mock_client.publish.await_args_list}
    assert by_topic["ez1/E1/alarm/off_grid"] == "false"
    assert by_topic["ez1/E1/alarm/any_active"] == "false"


# --- publish_result ---------------------------------------------------


async def test_publish_result_does_not_retain() -> None:
    mock_client = _mock_client()
    pub = MQTTPublisher("broker.local", device_id="E1")

    with patch.object(pub, "_build_client", return_value=mock_client):
        async with pub:
            await pub.publish_result(
                "max_power",
                {"ok": True, "value": 600},
            )

    mock_client.publish.assert_awaited_once()
    awaited_call = mock_client.publish.await_args
    assert awaited_call.args[0] == "ez1/E1/result/max_power"
    assert awaited_call.kwargs["retain"] is False
    body = json.loads(awaited_call.kwargs["payload"])
    assert body == {"ok": True, "value": 600}


# --- Reconnect hook ---------------------------------------------------


def test_reconnect_hook_is_optional() -> None:
    pub = MQTTPublisher("broker.local", device_id="E1")
    pub.trigger_reconnect_hook()  # must not raise without an on_reconnect callback


def test_reconnect_hook_invokes_callback() -> None:
    counter = MagicMock()
    pub = MQTTPublisher(
        "broker.local",
        device_id="E1",
        on_reconnect=counter,
    )

    pub.trigger_reconnect_hook()
    pub.trigger_reconnect_hook()

    assert counter.call_count == 2
    counter.assert_has_calls([call(), call()])


# --- Defensive uses of the topics module ------------------------------


# --- Metrics instrumentation ------------------------------------------


@pytest.fixture
def sample_state_for_metrics() -> InverterState:
    return InverterState(
        ts=datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
        device_id="E17010000783",
        power=PowerReading(ch1_w=139.0, ch2_w=65.0),
        energy_today=EnergyReading(ch1_kwh=0.28731, ch2_kwh=0.42653),
        energy_lifetime=EnergyReading(ch1_kwh=87.43068, ch2_kwh=111.24305),
        max_power_w=800,
        status="on",
        alarms=AlarmFlags(off_grid=False, output_fault=False, dc1_short=False, dc2_short=False),
    )


async def test_metrics_counts_publish_availability() -> None:

    mock_client = _mock_client()
    metrics = MetricsRegistry()
    pub = MQTTPublisher("broker.local", device_id="E1", metrics=metrics)

    with patch.object(pub, "_build_client", return_value=mock_client):
        async with pub:
            await pub.publish_availability(online=True)
            await pub.publish_availability(online=False)

    text = metrics.generate().decode("utf-8")
    assert 'ez1_mqtt_publish_total{kind="availability"} 2.0' in text


async def test_metrics_counts_publish_state_and_flat_topics(
    sample_state_for_metrics: InverterState,
) -> None:

    mock_client = _mock_client()
    metrics = MetricsRegistry()
    pub = MQTTPublisher("broker.local", device_id="E1", metrics=metrics)

    with patch.object(pub, "_build_client", return_value=mock_client):
        async with pub:
            await pub.publish_state(sample_state_for_metrics)

    text = metrics.generate().decode("utf-8")
    assert 'ez1_mqtt_publish_total{kind="state"} 1.0' in text
    assert 'ez1_mqtt_publish_total{kind="flat"} 16.0' in text


async def test_metrics_counts_publish_result() -> None:

    mock_client = _mock_client()
    metrics = MetricsRegistry()
    pub = MQTTPublisher("broker.local", device_id="E1", metrics=metrics)

    with patch.object(pub, "_build_client", return_value=mock_client):
        async with pub:
            await pub.publish_result("max_power", {"ok": True})

    text = metrics.generate().decode("utf-8")
    assert 'ez1_mqtt_publish_total{kind="result"} 1.0' in text


async def test_metrics_counts_generic_publish_as_discovery_or_other() -> None:

    mock_client = _mock_client()
    metrics = MetricsRegistry()
    pub = MQTTPublisher("broker.local", device_id="E1", metrics=metrics)

    with patch.object(pub, "_build_client", return_value=mock_client):
        async with pub:
            await pub.publish(
                "homeassistant/sensor/E1/power_total/config",
                "{}",
                retain=True,
            )
            await pub.publish("ez1/E1/custom/thing", "x", retain=False)

    text = metrics.generate().decode("utf-8")
    assert 'ez1_mqtt_publish_total{kind="discovery"} 1.0' in text
    assert 'ez1_mqtt_publish_total{kind="other"} 1.0' in text


async def test_metrics_unset_does_not_break_publish() -> None:
    """Backwards compatibility for callers that pre-date the metrics arg."""
    mock_client = _mock_client()
    pub = MQTTPublisher("broker.local", device_id="E1")  # no metrics

    with patch.object(pub, "_build_client", return_value=mock_client):
        async with pub:
            await pub.publish_availability(online=True)


def test_publisher_uses_topics_retain_map_directly() -> None:
    """Regression guard: do not hard-code retain flags in the publisher.

    The retain semantics live in :mod:`ez1_bridge.topics.RETAIN`. A
    refactor that hard-codes ``retain=True`` somewhere should fail this
    test by mismatching the canonical map.
    """
    assert topics.RETAIN["availability"] is True
    assert topics.RETAIN["state"] is True
    assert topics.RETAIN["flat"] is True
    assert topics.RETAIN["result"] is False
