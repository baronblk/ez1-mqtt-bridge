"""End-to-end Phase-6 integration test for the metrics endpoint.

Brings up :func:`run_service` against a respx-mocked EZ1 plus a real
Mosquitto, scrapes ``/metrics`` from outside the bridge with
:class:`httpx.AsyncClient`, and asserts that the canonical Prometheus
metric families show up populated -- the integration check that the
metric pipeline is wired front to back, from instrumentation hooks
through the registry into the aiohttp server.
"""

from __future__ import annotations

import asyncio
import socket
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx
from prometheus_client.parser import text_string_to_metric_families

from ez1_bridge.config import Settings
from ez1_bridge.main import run_service

from .conftest import BrokerEndpoint

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _make_settings(
    *,
    ez1_host: str,
    mqtt_host: str,
    mqtt_port: int,
    metrics_port: int,
) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        ez1_host=ez1_host,
        ez1_port=8050,
        mqtt_host=mqtt_host,
        mqtt_port=mqtt_port,
        mqtt_base_topic="ez1",
        mqtt_discovery_prefix="homeassistant",
        poll_interval=2,
        request_timeout=2,
        setmaxpower_verify=False,
        metrics_bind="127.0.0.1",
        metrics_port=metrics_port,
    )


def _arm_ez1_respx(
    api_response: Callable[[str], dict[str, Any]],
    host: str,
    *,
    device_id: str,
) -> None:
    base = f"http://{host}:8050"
    device_info = api_response("get_device_info").copy()
    device_info["data"] = {**device_info["data"], "deviceId": device_id}
    device_info["deviceId"] = device_id
    output_data = api_response("get_output_data").copy()
    output_data["deviceId"] = device_id
    max_power = api_response("get_max_power").copy()
    max_power["deviceId"] = device_id
    alarm = api_response("get_alarm").copy()
    alarm["deviceId"] = device_id
    on_off = api_response("get_on_off").copy()
    on_off["deviceId"] = device_id

    for endpoint, body in (
        ("getDeviceInfo", device_info),
        ("getOutputData", output_data),
        ("getMaxPower", max_power),
        ("getAlarm", alarm),
        ("getOnOff", on_off),
    ):
        respx.get(f"{base}/{endpoint}").mock(
            return_value=respx.MockResponse(200, json=body),
        )


@respx.mock
async def test_metrics_endpoint_serves_populated_registry(
    mosquitto_broker: BrokerEndpoint,
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    """After one poll cycle, /metrics returns valid Prometheus text with the full FR-005 surface.

    The respx mock is configured to pass-through the localhost scrape so
    httpx reaches the real aiohttp server, while every EZ1 URL stays
    intercepted -- a refactor that issues a real HTTP call to the
    inverter would surface as a connection error rather than silently
    succeeding via a stale fixture.
    """
    device_id = f"E{uuid.uuid4().hex[:12].upper()}"
    fake_ez1_host = f"ez1-{device_id.lower()}.test"
    metrics_port = _free_port()
    # Allow real HTTP traffic to the local metrics endpoint; everything
    # else is mocked. The passthrough route must be registered before
    # any GET hits the URL pattern.
    respx.get(host="127.0.0.1", port=metrics_port).pass_through()
    _arm_ez1_respx(api_response, fake_ez1_host, device_id=device_id)
    settings = _make_settings(
        ez1_host=fake_ez1_host,
        mqtt_host=mosquitto_broker.host,
        mqtt_port=mosquitto_broker.port,
        metrics_port=metrics_port,
    )
    stop_event = asyncio.Event()

    service_task = asyncio.create_task(run_service(settings, stop_event=stop_event))

    try:
        # Wait long enough for run_service to start, finish device-info
        # resolution, do one poll cycle, and have the metrics_server
        # serving requests.
        await asyncio.sleep(2.0)

        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{metrics_port}") as http:
            response = await http.get("/metrics")

        assert response.status_code == 200
        body = response.text
        families = {f.name for f in text_string_to_metric_families(body)}

        # Every FR-005 metric family is present.
        for name in (
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
        ):
            assert any(f.startswith(name) for f in families), (
                f"missing metric family {name!r} in {sorted(families)}"
            )

        # Spot-check live values: bridge_up=1, power_total matches the
        # fixture's p1+p2 = 139+65 = 204 W, max_power = 800.
        assert "ez1_bridge_up 1.0" in body
        assert f'ez1_power_watts{{channel="total",device_id="{device_id}"}} 204.0' in body
        assert f'ez1_max_power_watts{{device_id="{device_id}"}} 800.0' in body

        # API instrumentation: at least one observation on getOutputData.
        assert 'ez1_api_request_duration_seconds_count{endpoint="getOutputData"}' in body

        # MQTT publish counter: at least state + flat + availability + discovery
        # have been bumped in this run.
        assert 'ez1_mqtt_publish_total{kind="state"}' in body
        assert 'ez1_mqtt_publish_total{kind="availability"}' in body
        assert 'ez1_mqtt_publish_total{kind="discovery"}' in body

    finally:
        stop_event.set()
        await asyncio.wait_for(service_task, timeout=5.0)
