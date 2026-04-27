"""Async MQTT publisher (``aiomqtt``) with LWT, retain semantics, and reconnect hook.

Designed as a thin, retain-aware wrapper around :class:`aiomqtt.Client`.
The bridge owns one publisher instance for its entire lifetime and
re-uses the underlying TCP connection — broker reconnect orchestration
lives one layer up (Phase 6) rather than in the publisher itself, so the
publisher can stay focused on "construct the right MQTT messages" and
the application can decide what to do when the network drops.

LWT is set in the client *constructor*, not in any publish call; if the
process dies ungracefully, the broker fires the configured
``availability=offline`` retained message on its own. Re-instantiating
the publisher after a catastrophic failure re-issues the LWT, since
``Will`` is bound to the underlying client object.

The ``on_reconnect`` callback is wired here as a hook only; Phase 6
will call :meth:`MQTTPublisher.trigger_reconnect_hook` from its
reconnect loop to bump the ``ez1_mqtt_reconnects_total`` Prometheus
counter. Until then, the hook is just a no-op default.
"""

from __future__ import annotations

import json as json_lib
from collections.abc import Callable, Mapping
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final, Self

import aiomqtt
from pydantic import SecretStr

from ez1_bridge import topics
from ez1_bridge.domain.models import InverterState

if TYPE_CHECKING:
    from ez1_bridge.adapters.prom_metrics import MetricsRegistry

_DEFAULT_QOS: Final[int] = 1


def _flat_pairs(state: InverterState) -> list[tuple[str, str, str]]:
    """Yield ``(group, key, value)`` triples for the per-metric flat topics.

    The value is already a string so the publisher can hand it straight to
    :meth:`aiomqtt.Client.publish` without per-call formatting branches.
    """
    return [
        ("power", "ch1_w", str(state.power.ch1_w)),
        ("power", "ch2_w", str(state.power.ch2_w)),
        ("power", "total_w", str(state.power.total_w)),
        ("energy_today", "ch1_kwh", str(state.energy_today.ch1_kwh)),
        ("energy_today", "ch2_kwh", str(state.energy_today.ch2_kwh)),
        ("energy_today", "total_kwh", str(state.energy_today.total_kwh)),
        ("energy_lifetime", "ch1_kwh", str(state.energy_lifetime.ch1_kwh)),
        ("energy_lifetime", "ch2_kwh", str(state.energy_lifetime.ch2_kwh)),
        ("energy_lifetime", "total_kwh", str(state.energy_lifetime.total_kwh)),
        ("max_power_w", "value", str(state.max_power_w)),
        ("status", "value", state.status),
        ("alarm", "off_grid", str(state.alarms.off_grid).lower()),
        ("alarm", "output_fault", str(state.alarms.output_fault).lower()),
        ("alarm", "dc1_short", str(state.alarms.dc1_short).lower()),
        ("alarm", "dc2_short", str(state.alarms.dc2_short).lower()),
        ("alarm", "any_active", str(state.alarms.any_active).lower()),
    ]


class MQTTPublisher:
    """Retain-aware MQTT publisher with LWT and a reconnect-counter hook.

    Use as an async context manager so the underlying
    :class:`aiomqtt.Client` is created and torn down exactly once::

        async with MQTTPublisher(host="broker", device_id="E17...") as pub:
            await pub.publish_availability(online=True)
            await pub.publish_state(state)

    Calling a publish method outside the context manager raises
    :class:`RuntimeError`.
    """

    def __init__(
        self,
        host: str,
        port: int = 1883,
        *,
        username: str | None = None,
        password: SecretStr | None = None,
        base_topic: str = "ez1",
        device_id: str,
        identifier: str | None = None,
        on_reconnect: Callable[[], None] | None = None,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        if not host:
            msg = "host must be a non-empty string"
            raise ValueError(msg)
        if not device_id:
            msg = "device_id must be a non-empty string"
            raise ValueError(msg)
        if not base_topic:
            msg = "base_topic must be a non-empty string"
            raise ValueError(msg)

        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._base = base_topic
        self._device_id = device_id
        self._identifier = identifier or f"ez1-bridge-{device_id}"
        self._on_reconnect = on_reconnect
        self._metrics = metrics
        self._client: aiomqtt.Client | None = None

    @property
    def base_topic(self) -> str:
        """Configured MQTT topic root (e.g. ``"ez1"``)."""
        return self._base

    @property
    def device_id(self) -> str:
        """Configured EZ1 device ID, used in every topic path."""
        return self._device_id

    @property
    def identifier(self) -> str:
        """MQTT client_id used during CONNECT."""
        return self._identifier

    @property
    def client(self) -> aiomqtt.Client:
        """The underlying ``aiomqtt.Client`` (for code paths that need to
        subscribe and consume messages, e.g. the command handler).

        Raises :class:`RuntimeError` if accessed outside the async context
        manager. The publisher remains the owner of the client lifecycle;
        callers must not close it.
        """
        return self._ensure_client()

    def _build_client(self) -> aiomqtt.Client:
        """Create the underlying ``aiomqtt.Client`` with LWT preset.

        Extracted from :meth:`__aenter__` so tests can introspect the
        constructor arguments without driving the full async lifecycle.
        """
        return aiomqtt.Client(
            hostname=self._host,
            port=self._port,
            username=self._username,
            password=(self._password.get_secret_value() if self._password is not None else None),
            identifier=self._identifier,
            will=aiomqtt.Will(
                topic=topics.availability(self._base, self._device_id),
                payload=topics.AVAILABILITY_OFFLINE,
                qos=_DEFAULT_QOS,
                retain=topics.RETAIN["availability"],
            ),
        )

    async def __aenter__(self) -> Self:
        client = self._build_client()
        await client.__aenter__()
        self._client = client
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.__aexit__(exc_type, exc, tb)
            self._client = None

    def _ensure_client(self) -> aiomqtt.Client:
        if self._client is None:
            msg = "MQTTPublisher must be used as an async context manager"
            raise RuntimeError(msg)
        return self._client

    # --- Publish methods -------------------------------------------------

    def _record_publish(self, kind: str) -> None:
        """Bump the ``ez1_mqtt_publish_total`` counter, if instrumented."""
        if self._metrics is not None:
            self._metrics.increment_mqtt_publish(kind)

    async def publish_availability(self, *, online: bool) -> None:
        """Publish ``"online"`` or ``"offline"`` to the availability topic.

        The bridge calls this with ``online=True`` immediately after
        connect; the LWT handles ``offline`` automatically on ungraceful
        disconnect.
        """
        client = self._ensure_client()
        payload = topics.AVAILABILITY_ONLINE if online else topics.AVAILABILITY_OFFLINE
        await client.publish(
            topics.availability(self._base, self._device_id),
            payload=payload,
            qos=_DEFAULT_QOS,
            retain=topics.RETAIN["availability"],
        )
        self._record_publish("availability")

    async def publish_state(self, state: InverterState) -> None:
        """Publish the structured JSON state plus all flat per-metric topics.

        Both the JSON state topic and the per-metric flat topics are
        retained so a fresh subscriber (HA, mosquitto_sub, etc.) sees
        the latest snapshot immediately.
        """
        client = self._ensure_client()
        await client.publish(
            topics.state(self._base, self._device_id),
            payload=state.model_dump_json(),
            qos=_DEFAULT_QOS,
            retain=topics.RETAIN["state"],
        )
        self._record_publish("state")
        for group, key, value in _flat_pairs(state):
            await client.publish(
                topics.flat(self._base, self._device_id, group, key),
                payload=value,
                qos=_DEFAULT_QOS,
                retain=topics.RETAIN["flat"],
            )
            self._record_publish("flat")

    async def publish(
        self,
        topic: str,
        payload: bytes | str | int | float,
        *,
        retain: bool,
        qos: int = _DEFAULT_QOS,
    ) -> None:
        """Publish an arbitrary message with explicit retain semantics.

        Used by callers that build their own topic / payload (e.g. the
        discovery-publishing path in :mod:`ez1_bridge.application.poll_service`)
        rather than going through the typed convenience methods. Retain
        is required as a keyword argument so it is never forgotten.
        """
        client = self._ensure_client()
        await client.publish(topic, payload=payload, qos=qos, retain=retain)
        # Best-effort kind detection from the topic shape so the counter
        # stays bucketed without a mandatory caller-side hint.
        kind = "discovery" if "/sensor/" in topic or "/binary_sensor/" in topic else "other"
        self._record_publish(kind)

    async def publish_result(self, command_name: str, payload: Mapping[str, Any]) -> None:
        """Publish a command-result event (``retain=False``).

        Used by the Phase-5 command handler to acknowledge writes.
        """
        client = self._ensure_client()
        await client.publish(
            topics.result(self._base, self._device_id, command_name),
            payload=json_lib.dumps(payload),
            qos=_DEFAULT_QOS,
            retain=topics.RETAIN["result"],
        )
        self._record_publish("result")

    # --- Reconnect-counter hook -----------------------------------------

    def trigger_reconnect_hook(self) -> None:
        """Invoke the ``on_reconnect`` callback if one was wired.

        Phase 6's reconnect-orchestration loop calls this each time it
        successfully re-establishes the broker connection so the
        ``ez1_mqtt_reconnects_total`` Prometheus counter advances. The
        publisher itself does not own the reconnect loop — that
        responsibility lives in :mod:`ez1_bridge.main`.
        """
        if self._on_reconnect is not None:
            self._on_reconnect()
