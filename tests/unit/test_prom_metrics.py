"""Tests for :mod:`ez1_bridge.adapters.prom_metrics`."""

from __future__ import annotations

import asyncio
import socket
from datetime import UTC, datetime

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families

from ez1_bridge.adapters.prom_metrics import (
    MetricsRegistry,
    metrics_server,
)
from ez1_bridge.domain.models import (
    AlarmFlags,
    EnergyReading,
    InverterState,
    PowerReading,
)

# --- Fresh-registry isolation -----------------------------------------


def test_two_registries_do_not_share_state() -> None:
    """The whole point of a per-instance CollectorRegistry."""
    a = MetricsRegistry()
    b = MetricsRegistry()
    a.increment_mqtt_reconnect()
    a.increment_mqtt_reconnect()

    a_metrics = a.generate().decode("utf-8")
    b_metrics = b.generate().decode("utf-8")

    assert "ez1_mqtt_reconnects_total 2.0" in a_metrics
    assert "ez1_mqtt_reconnects_total 0.0" in b_metrics


# --- Liveness ----------------------------------------------------------


def test_set_bridge_up_toggles_gauge() -> None:
    metrics = MetricsRegistry()
    metrics.set_bridge_up(up=True)
    assert "ez1_bridge_up 1.0" in metrics.generate().decode("utf-8")
    metrics.set_bridge_up(up=False)
    assert "ez1_bridge_up 0.0" in metrics.generate().decode("utf-8")


# --- record_state ------------------------------------------------------


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
        alarms=AlarmFlags(off_grid=False, output_fault=True, dc1_short=False, dc2_short=False),
    )


def test_record_state_sets_per_channel_power(sample_state: InverterState) -> None:
    metrics = MetricsRegistry()
    metrics.record_state(sample_state)
    text = metrics.generate().decode("utf-8")

    assert 'ez1_power_watts{channel="1",device_id="E17010000783"} 139.0' in text
    assert 'ez1_power_watts{channel="2",device_id="E17010000783"} 65.0' in text
    assert 'ez1_power_watts{channel="total",device_id="E17010000783"} 204.0' in text


def test_record_state_sets_energy_today_and_lifetime(sample_state: InverterState) -> None:
    metrics = MetricsRegistry()
    metrics.record_state(sample_state)
    text = metrics.generate().decode("utf-8")

    assert 'ez1_energy_today_kwh{channel="1",device_id="E17010000783"} 0.28731' in text
    assert 'ez1_energy_today_kwh{channel="total",device_id="E17010000783"} 0.71384' in text
    assert 'ez1_energy_lifetime_kwh{channel="1",device_id="E17010000783"} 87.43068' in text


def test_record_state_inverter_on_off_status(sample_state: InverterState) -> None:
    metrics = MetricsRegistry()
    metrics.record_state(sample_state)
    text = metrics.generate().decode("utf-8")
    assert 'ez1_inverter_on{device_id="E17010000783"} 1.0' in text


def test_record_state_alarm_bits(sample_state: InverterState) -> None:
    """Each alarm bit lands on its own labelled gauge value."""
    metrics = MetricsRegistry()
    metrics.record_state(sample_state)
    text = metrics.generate().decode("utf-8")

    assert 'ez1_alarm{device_id="E17010000783",type="off_grid"} 0.0' in text
    assert 'ez1_alarm{device_id="E17010000783",type="output_fault"} 1.0' in text
    assert 'ez1_alarm{device_id="E17010000783",type="dc1_short"} 0.0' in text
    assert 'ez1_alarm{device_id="E17010000783",type="dc2_short"} 0.0' in text


def test_record_state_max_power(sample_state: InverterState) -> None:
    metrics = MetricsRegistry()
    metrics.record_state(sample_state)
    text = metrics.generate().decode("utf-8")
    assert 'ez1_max_power_watts{device_id="E17010000783"} 800.0' in text


# --- API instrumentation ----------------------------------------------


def test_observe_api_request_records_histogram_bucket() -> None:
    metrics = MetricsRegistry()
    metrics.observe_api_request("getOutputData", 0.075)  # falls into the 0.1 bucket
    text = metrics.generate().decode("utf-8")

    assert 'ez1_api_request_duration_seconds_bucket{endpoint="getOutputData",le="0.1"} 1.0' in text
    assert 'ez1_api_request_duration_seconds_bucket{endpoint="getOutputData",le="0.05"} 0.0' in text


def test_increment_api_error_records_reason() -> None:
    metrics = MetricsRegistry()
    metrics.increment_api_error("getOutputData", "ConnectError")
    metrics.increment_api_error("getOutputData", "ConnectError")
    metrics.increment_api_error("getOutputData", "TimeoutException")
    text = metrics.generate().decode("utf-8")

    assert 'ez1_api_errors_total{endpoint="getOutputData",reason="ConnectError"} 2.0' in text
    assert 'ez1_api_errors_total{endpoint="getOutputData",reason="TimeoutException"} 1.0' in text


# --- MQTT instrumentation ---------------------------------------------


def test_increment_mqtt_publish_by_kind() -> None:
    metrics = MetricsRegistry()
    metrics.increment_mqtt_publish("state")
    metrics.increment_mqtt_publish("flat")
    metrics.increment_mqtt_publish("flat")
    text = metrics.generate().decode("utf-8")

    assert 'ez1_mqtt_publish_total{kind="state"} 1.0' in text
    assert 'ez1_mqtt_publish_total{kind="flat"} 2.0' in text


def test_increment_mqtt_reconnect_counter() -> None:
    metrics = MetricsRegistry()
    metrics.increment_mqtt_reconnect()
    metrics.increment_mqtt_reconnect()
    metrics.increment_mqtt_reconnect()
    assert "ez1_mqtt_reconnects_total 3.0" in metrics.generate().decode("utf-8")


# --- generate() / Prometheus text format -------------------------------


def test_generate_returns_valid_prometheus_text(sample_state: InverterState) -> None:
    metrics = MetricsRegistry()
    metrics.set_bridge_up(up=True)
    metrics.record_state(sample_state)
    metrics.observe_api_request("getOutputData", 0.05)
    metrics.increment_api_error("getDeviceInfo", "ConnectError")
    metrics.increment_mqtt_publish("state")
    metrics.increment_mqtt_reconnect()

    text = metrics.generate().decode("utf-8")
    families = list(text_string_to_metric_families(text))
    names = {f.name for f in families}

    expected = {
        "ez1_bridge_up",
        "ez1_power_watts",
        "ez1_energy_today_kwh",
        "ez1_energy_lifetime_kwh",
        "ez1_max_power_watts",
        "ez1_inverter_on",
        "ez1_alarm",
        "ez1_api_request_duration_seconds",
        "ez1_api_errors",
        "ez1_mqtt_publish",
        "ez1_mqtt_reconnects",
    }
    # Some metric families are reported with the suffix stripped by the parser;
    # check coverage without being strict on exact suffix handling.
    for name in expected:
        prefixes = {n for n in names if n.startswith(name)}
        assert prefixes, f"expected family for {name!r} in output"


# --- /metrics aiohttp server ------------------------------------------


@pytest.fixture
def metrics() -> MetricsRegistry:
    m = MetricsRegistry()
    m.set_bridge_up(up=True)
    return m


def _free_port() -> int:
    """Reserve and immediately release a free localhost TCP port for the server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_metrics_server_serves_prometheus_text_format(
    metrics: MetricsRegistry,
) -> None:
    """Spin up the server, GET /metrics, verify Prometheus text format."""
    stop_event = asyncio.Event()
    port = _free_port()
    server_task = asyncio.create_task(
        metrics_server(
            metrics=metrics,
            host="127.0.0.1",
            port=port,
            stop_event=stop_event,
        ),
    )

    # Wait briefly for the runner to bind, then GET.
    try:
        await asyncio.sleep(0.1)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            response = await http.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        body = response.text
        assert "ez1_bridge_up 1.0" in body
        list(text_string_to_metric_families(body))
    finally:
        stop_event.set()
        await asyncio.wait_for(server_task, timeout=3.0)


async def test_metrics_server_returns_404_for_other_paths(
    metrics: MetricsRegistry,
) -> None:
    stop_event = asyncio.Event()
    port = _free_port()
    server_task = asyncio.create_task(
        metrics_server(
            metrics=metrics,
            host="127.0.0.1",
            port=port,
            stop_event=stop_event,
        ),
    )
    try:
        await asyncio.sleep(0.1)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            response = await http.get("/healthz")
        assert response.status_code == 404
    finally:
        stop_event.set()
        await asyncio.wait_for(server_task, timeout=3.0)


async def test_metrics_server_exits_on_stop_event(
    metrics: MetricsRegistry,
) -> None:
    """The coroutine must return promptly once stop_event fires."""
    stop_event = asyncio.Event()
    port = _free_port()
    server_task = asyncio.create_task(
        metrics_server(
            metrics=metrics,
            host="127.0.0.1",
            port=port,
            stop_event=stop_event,
        ),
    )
    await asyncio.sleep(0.1)
    stop_event.set()
    await asyncio.wait_for(server_task, timeout=2.0)
    assert server_task.done()
