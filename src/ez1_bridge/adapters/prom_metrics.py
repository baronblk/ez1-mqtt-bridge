"""Prometheus metrics: dedicated registry, instrumentation hooks, and ``/metrics`` server.

The :class:`MetricsRegistry` owns its own :class:`CollectorRegistry`
rather than registering metrics on the global default. The default
pattern (``Counter("foo", ...)`` without ``registry=`` argument) puts
every metric on a process-global registry, which makes tests that
re-instantiate metrics raise ``ValueError: Duplicated timeseries``.
A per-instance registry sidesteps the issue: each test or each bridge
run gets a fresh registry, no cross-talk.

Metric naming follows Prometheus best practice (lower_snake_case,
``_total`` suffix on counters, ``_seconds`` on durations) and the
``ez1_`` prefix scopes them to the bridge so co-located services do
not collide.

Histogram buckets for the EZ1 API request duration are explicit rather
than the default ``prometheus_client`` buckets. The default range
(0.005 s to 10 s) is optimised for web apps; the EZ1 sits behind a
WLAN hop with 45-90 ms RTT, so meaningful buckets run from 25 ms to
5 s with a tail to ``+Inf`` for inverter reboots / WLAN dropouts.
"""

from __future__ import annotations

import asyncio
from typing import Final

import structlog
from aiohttp import web
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from ez1_bridge.domain.models import InverterState

_log = structlog.get_logger(__name__)

#: Histogram buckets in seconds tuned for the EZ1's local WLAN latency.
#: 25 ms / 50 ms / 100 ms covers the typical case; 250 ms / 500 ms / 1 s /
#: 2.5 s / 5 s span retries and inverter wake-up; +Inf catches anything
#: longer (WR reboot, network outage).
_API_DURATION_BUCKETS: Final[tuple[float, ...]] = (
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    float("inf"),
)


class MetricsRegistry:
    """Prometheus metrics container -- one instance per bridge run.

    All metric objects bind to ``self.registry``, which is exclusive to
    this instance. The :meth:`generate` method returns the wire-format
    bytes the ``/metrics`` server serves. Helper methods like
    :meth:`record_state` and :meth:`observe_api_request` keep update
    sites declarative and avoid scattering ``.labels(...)`` chains
    across the codebase.
    """

    def __init__(self) -> None:
        self.registry = CollectorRegistry()

        # --- Bridge liveness ---------------------------------------------
        self.bridge_up = Gauge(
            "ez1_bridge_up",
            "1 while the bridge service is running, 0 when stopped",
            registry=self.registry,
        )

        # --- Inverter state (set per poll cycle) -------------------------
        self.power_watts = Gauge(
            "ez1_power_watts",
            "Instantaneous power output in watts",
            ["device_id", "channel"],
            registry=self.registry,
        )
        self.energy_today_kwh = Gauge(
            "ez1_energy_today_kwh",
            "Energy generated since cold start, in kWh",
            ["device_id", "channel"],
            registry=self.registry,
        )
        self.energy_lifetime_kwh = Gauge(
            "ez1_energy_lifetime_kwh",
            "Lifetime cumulative energy in kWh",
            ["device_id", "channel"],
            registry=self.registry,
        )
        self.max_power_watts = Gauge(
            "ez1_max_power_watts",
            "Currently configured output limit in watts",
            ["device_id"],
            registry=self.registry,
        )
        self.inverter_on = Gauge(
            "ez1_inverter_on",
            "1 if the inverter is on, 0 if off",
            ["device_id"],
            registry=self.registry,
        )
        self.alarm = Gauge(
            "ez1_alarm",
            "1 if the named alarm bit is active, 0 otherwise",
            ["device_id", "type"],
            registry=self.registry,
        )

        # --- API instrumentation -----------------------------------------
        self.api_request_duration = Histogram(
            "ez1_api_request_duration_seconds",
            "EZ1 HTTP API request duration",
            ["endpoint"],
            buckets=_API_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.api_errors = Counter(
            "ez1_api_errors_total",
            "EZ1 HTTP API request failures",
            ["endpoint", "reason"],
            registry=self.registry,
        )

        # --- MQTT instrumentation ----------------------------------------
        self.mqtt_publish = Counter(
            "ez1_mqtt_publish_total",
            "MQTT publish operations issued by the bridge",
            ["kind"],
            registry=self.registry,
        )
        self.mqtt_reconnects = Counter(
            "ez1_mqtt_reconnects_total",
            "Number of MQTT reconnect events",
            registry=self.registry,
        )

    # --- Update helpers ---------------------------------------------------

    def set_bridge_up(self, *, up: bool) -> None:
        """Record the bridge's overall liveness."""
        self.bridge_up.set(1.0 if up else 0.0)

    def record_state(self, state: InverterState) -> None:
        """Mirror an :class:`InverterState` snapshot onto the state gauges."""
        device_id = state.device_id
        self.power_watts.labels(device_id=device_id, channel="1").set(state.power.ch1_w)
        self.power_watts.labels(device_id=device_id, channel="2").set(state.power.ch2_w)
        self.power_watts.labels(device_id=device_id, channel="total").set(state.power.total_w)
        self.energy_today_kwh.labels(device_id=device_id, channel="1").set(
            state.energy_today.ch1_kwh,
        )
        self.energy_today_kwh.labels(device_id=device_id, channel="2").set(
            state.energy_today.ch2_kwh,
        )
        self.energy_today_kwh.labels(device_id=device_id, channel="total").set(
            state.energy_today.total_kwh,
        )
        self.energy_lifetime_kwh.labels(device_id=device_id, channel="1").set(
            state.energy_lifetime.ch1_kwh,
        )
        self.energy_lifetime_kwh.labels(device_id=device_id, channel="2").set(
            state.energy_lifetime.ch2_kwh,
        )
        self.energy_lifetime_kwh.labels(device_id=device_id, channel="total").set(
            state.energy_lifetime.total_kwh,
        )
        self.max_power_watts.labels(device_id=device_id).set(state.max_power_w)
        self.inverter_on.labels(device_id=device_id).set(
            1.0 if state.status == "on" else 0.0,
        )
        self.alarm.labels(device_id=device_id, type="off_grid").set(
            1.0 if state.alarms.off_grid else 0.0,
        )
        self.alarm.labels(device_id=device_id, type="output_fault").set(
            1.0 if state.alarms.output_fault else 0.0,
        )
        self.alarm.labels(device_id=device_id, type="dc1_short").set(
            1.0 if state.alarms.dc1_short else 0.0,
        )
        self.alarm.labels(device_id=device_id, type="dc2_short").set(
            1.0 if state.alarms.dc2_short else 0.0,
        )

    def observe_api_request(self, endpoint: str, duration_seconds: float) -> None:
        """Record an EZ1 HTTP request's wall-clock duration."""
        self.api_request_duration.labels(endpoint=endpoint).observe(duration_seconds)

    def increment_api_error(self, endpoint: str, reason: str) -> None:
        """Increment the API error counter with a free-form ``reason`` label.

        Caller passes the exception class name (``ConnectError``,
        ``TimeoutException``, ``HTTPStatusError_5xx`` etc.) so dashboards
        can break down errors by failure mode.
        """
        self.api_errors.labels(endpoint=endpoint, reason=reason).inc()

    def increment_mqtt_publish(self, kind: str) -> None:
        """Count a single MQTT publish call by topic kind.

        ``kind`` follows the canonical bucket vocabulary mirrored in
        :mod:`ez1_bridge.topics`: ``state``, ``flat``, ``availability``,
        ``result``, ``discovery``, plus the ``other`` sentinel emitted
        by :meth:`MQTTPublisher.publish` when an arbitrary topic does
        not match one of the typed shapes. The sentinel is a deliberate
        bucket cap on label cardinality — without it, a future caller
        of the generic publish helper could otherwise leak free-form
        topic strings into the Prometheus index.
        """
        self.mqtt_publish.labels(kind=kind).inc()

    def increment_mqtt_reconnect(self) -> None:
        """Bump the reconnect counter -- wired to MQTTPublisher's hook."""
        self.mqtt_reconnects.inc()

    def generate(self) -> bytes:
        """Produce the Prometheus text-format payload for the ``/metrics`` endpoint."""
        return generate_latest(self.registry)


# --- /metrics aiohttp server ----------------------------------------------


async def metrics_server(
    *,
    metrics: MetricsRegistry,
    host: str,
    port: int,
    stop_event: asyncio.Event,
) -> None:
    """Run an aiohttp ``/metrics`` endpoint until ``stop_event`` fires.

    Designed to live as a sibling task in :func:`run_service`'s
    TaskGroup. Cancellation works via the ``stop_event.wait`` in the
    body; if the task is force-cancelled by ``run_service``,
    :class:`asyncio.CancelledError` propagates out of the await and
    the ``finally`` block tears the runner down.
    """

    async def handle_metrics(_: web.Request) -> web.Response:
        body = metrics.generate()
        # Prometheus' CONTENT_TYPE_LATEST already includes charset, so we
        # set it via the Content-Type header directly -- aiohttp's
        # ``content_type`` argument forbids ``charset=``.
        return web.Response(body=body, headers={"Content-Type": CONTENT_TYPE_LATEST})

    app = web.Application()
    app.router.add_get("/metrics", handle_metrics)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    _log.info("metrics_server_started", host=host, port=port)

    try:
        await stop_event.wait()
    finally:
        await runner.cleanup()
        _log.info("metrics_server_stopped")
