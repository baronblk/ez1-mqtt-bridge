"""Integration tests for :class:`MQTTPublisher` against a real Mosquitto broker.

Run end-to-end against an ``eclipse-mosquitto:2.0.20`` container managed
by ``testcontainers``. Each test owns a unique device_id so retained
messages from earlier tests never bleed into later ones, even though
the broker is session-scoped.

Skipped automatically when Docker is unavailable on the host (see
``conftest.py``); CI on Linux runners always has Docker.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime

import aiomqtt
import pytest
from pydantic import SecretStr

from ez1_bridge import topics
from ez1_bridge.adapters.mqtt_publisher import MQTTPublisher
from ez1_bridge.domain.models import (
    AlarmFlags,
    EnergyReading,
    InverterState,
    PowerReading,
)

from .conftest import AUTH_PASSWORD, AUTH_USERNAME, BrokerEndpoint

pytestmark = pytest.mark.integration

_MESSAGE_TIMEOUT_SECONDS = 5.0


@pytest.fixture
def device_id() -> str:
    """Unique per-test device_id so retained messages don't leak across tests."""
    return f"E{uuid.uuid4().hex[:12].upper()}"


@pytest.fixture
def sample_state(device_id: str) -> InverterState:
    return InverterState(
        ts=datetime(2026, 4, 26, 18, 0, tzinfo=UTC),
        device_id=device_id,
        power=PowerReading(ch1_w=139.0, ch2_w=65.0),
        energy_today=EnergyReading(ch1_kwh=0.28731, ch2_kwh=0.42653),
        energy_lifetime=EnergyReading(ch1_kwh=87.43068, ch2_kwh=111.24305),
        max_power_w=800,
        status="on",
        alarms=AlarmFlags(off_grid=False, output_fault=False, dc1_short=False, dc2_short=False),
    )


async def _wait_for_message_on(
    client: aiomqtt.Client,
    expected_topic: str,
    *,
    timeout: float = _MESSAGE_TIMEOUT_SECONDS,
) -> aiomqtt.Message:
    """Block until ``client`` receives a message on ``expected_topic``."""
    async with asyncio.timeout(timeout):
        async for msg in client.messages:
            if str(msg.topic) == expected_topic:
                return msg
    err = f"no message on {expected_topic} within {timeout}s"
    raise TimeoutError(err)


# --- Connection + publish ----------------------------------------------


async def test_publish_availability_arrives_at_subscriber(
    mosquitto_broker: BrokerEndpoint,
    device_id: str,
) -> None:
    """The publisher's online announcement reaches a concurrent subscriber."""
    availability_topic = topics.availability("ez1", device_id)

    async with aiomqtt.Client(
        hostname=mosquitto_broker.host,
        port=mosquitto_broker.port,
        identifier=f"observer-{device_id}",
    ) as observer:
        await observer.subscribe(availability_topic, qos=1)

        async with MQTTPublisher(
            host=mosquitto_broker.host,
            port=mosquitto_broker.port,
            device_id=device_id,
        ) as pub:
            await pub.publish_availability(online=True)

            msg = await _wait_for_message_on(observer, availability_topic)

    assert msg.payload == b"online"


async def test_publish_state_arrives_at_subscriber(
    mosquitto_broker: BrokerEndpoint,
    device_id: str,
    sample_state: InverterState,
) -> None:
    """The structured JSON state payload is delivered intact."""
    state_topic = topics.state("ez1", device_id)

    async with aiomqtt.Client(
        hostname=mosquitto_broker.host,
        port=mosquitto_broker.port,
        identifier=f"observer-{device_id}",
    ) as observer:
        await observer.subscribe(state_topic, qos=1)

        async with MQTTPublisher(
            host=mosquitto_broker.host,
            port=mosquitto_broker.port,
            device_id=device_id,
        ) as pub:
            await pub.publish_state(sample_state)

            msg = await _wait_for_message_on(observer, state_topic)

    assert isinstance(msg.payload, bytes | bytearray)
    body = json.loads(bytes(msg.payload).decode("utf-8"))
    assert body["device_id"] == device_id
    assert body["status"] == "on"
    assert body["power"]["total_w"] == 204.0


# --- Retain semantics --------------------------------------------------


async def test_state_retain_delivers_to_late_subscriber(
    mosquitto_broker: BrokerEndpoint,
    device_id: str,
    sample_state: InverterState,
) -> None:
    """A subscriber that connects *after* publish receives the retained state.

    This is the regression that bites HA integrations: if retain is
    misconfigured, dashboards stay empty after a broker or HA restart.
    """
    state_topic = topics.state("ez1", device_id)

    # Publish first, while no subscriber is listening.
    async with MQTTPublisher(
        host=mosquitto_broker.host,
        port=mosquitto_broker.port,
        device_id=device_id,
    ) as pub:
        await pub.publish_state(sample_state)

    # Now subscribe — broker should immediately deliver the retained message.
    async with aiomqtt.Client(
        hostname=mosquitto_broker.host,
        port=mosquitto_broker.port,
        identifier=f"late-observer-{device_id}",
    ) as observer:
        await observer.subscribe(state_topic, qos=1)
        msg = await _wait_for_message_on(observer, state_topic)

    body = json.loads(bytes(msg.payload).decode("utf-8"))
    assert body["device_id"] == device_id
    assert body["status"] == "on"
    assert msg.retain is True


async def test_result_topic_is_not_retained(
    mosquitto_broker: BrokerEndpoint,
    device_id: str,
) -> None:
    """A late subscriber must NOT see old result events — they are events, not state."""
    result_topic = topics.result("ez1", device_id, "max_power")

    # Publish a result while nobody is listening.
    async with MQTTPublisher(
        host=mosquitto_broker.host,
        port=mosquitto_broker.port,
        device_id=device_id,
    ) as pub:
        await pub.publish_result("max_power", {"ok": True, "value": 600})

    # Subscribe afterwards; expect NOTHING within the short timeout.
    async with aiomqtt.Client(
        hostname=mosquitto_broker.host,
        port=mosquitto_broker.port,
        identifier=f"late-observer-{device_id}",
    ) as observer:
        await observer.subscribe(result_topic, qos=1)

        with pytest.raises(TimeoutError):
            await _wait_for_message_on(observer, result_topic, timeout=1.5)


async def test_flat_topics_retained(
    mosquitto_broker: BrokerEndpoint,
    device_id: str,
    sample_state: InverterState,
) -> None:
    """The flat per-metric topics are retained alongside the JSON state."""
    flat_topic = topics.flat("ez1", device_id, "power", "total_w")

    async with MQTTPublisher(
        host=mosquitto_broker.host,
        port=mosquitto_broker.port,
        device_id=device_id,
    ) as pub:
        await pub.publish_state(sample_state)

    async with aiomqtt.Client(
        hostname=mosquitto_broker.host,
        port=mosquitto_broker.port,
        identifier=f"flat-observer-{device_id}",
    ) as observer:
        await observer.subscribe(flat_topic, qos=1)
        msg = await _wait_for_message_on(observer, flat_topic)

    assert bytes(msg.payload).decode("utf-8") == "204.0"
    assert msg.retain is True


# --- Authentication ----------------------------------------------------


async def test_publisher_authenticates_with_username_password(
    mosquitto_auth_broker: BrokerEndpoint,
    device_id: str,
) -> None:
    """With matching credentials the publisher connects and publishes successfully."""
    availability_topic = topics.availability("ez1", device_id)

    async with aiomqtt.Client(
        hostname=mosquitto_auth_broker.host,
        port=mosquitto_auth_broker.port,
        username=AUTH_USERNAME,
        password=AUTH_PASSWORD,
        identifier=f"auth-observer-{device_id}",
    ) as observer:
        await observer.subscribe(availability_topic, qos=1)

        async with MQTTPublisher(
            host=mosquitto_auth_broker.host,
            port=mosquitto_auth_broker.port,
            username=AUTH_USERNAME,
            password=SecretStr(AUTH_PASSWORD),
            device_id=device_id,
        ) as pub:
            await pub.publish_availability(online=True)

            msg = await _wait_for_message_on(observer, availability_topic)

    assert msg.payload == b"online"


async def test_publisher_rejected_without_credentials(
    mosquitto_auth_broker: BrokerEndpoint,
    device_id: str,
) -> None:
    """Without credentials, the auth broker refuses the CONNECT."""
    pub = MQTTPublisher(
        host=mosquitto_auth_broker.host,
        port=mosquitto_auth_broker.port,
        device_id=device_id,
    )

    with pytest.raises(aiomqtt.MqttError):
        await pub.__aenter__()
