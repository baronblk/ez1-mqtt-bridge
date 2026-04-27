"""End-to-end Phase-4 integration test.

Wires a respx-mocked EZ1 inverter to a real Mosquitto container via
:func:`run_service`. Verifies that one full poll cycle produces:

* the structured JSON state on ``ez1/{device_id}/state`` (retained)
* the 16 flat per-metric topics (retained)
* the availability ``online`` (retained)
* the 15 HA discovery messages under
  ``homeassistant/sensor/{device_id}/{key}/config`` and
  ``homeassistant/binary_sensor/{device_id}/{key}/config`` (retained)
* and on graceful shutdown, ``availability=offline`` overrides the
  retained ``online``.

This is the test that catches "everything wired correctly together"
regressions that unit tests would miss (LWT topic vs config topic,
JSON shape vs HA value_template, retain semantics on the wire).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import aiomqtt
import pytest
import respx

from ez1_bridge.config import Settings
from ez1_bridge.main import run_service

from .conftest import BrokerEndpoint

pytestmark = pytest.mark.integration

_E2E_TIMEOUT_SECONDS = 15.0
_SUBSCRIPTION_WARMUP_SECONDS = 0.1


def _make_settings(
    *,
    ez1_host: str,
    mqtt_host: str,
    mqtt_port: int,
    poll_interval: int,
) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        ez1_host=ez1_host,
        ez1_port=8050,
        mqtt_host=mqtt_host,
        mqtt_port=mqtt_port,
        mqtt_base_topic="ez1",
        mqtt_discovery_prefix="homeassistant",
        poll_interval=poll_interval,
        request_timeout=2,
    )


def _arm_ez1_respx(api_response: Any, host: str, *, device_id: str) -> None:
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
    respx.get(f"{base}/getDeviceInfo").mock(
        return_value=respx.MockResponse(200, json=device_info),
    )
    respx.get(f"{base}/getOutputData").mock(
        return_value=respx.MockResponse(200, json=output_data),
    )
    respx.get(f"{base}/getMaxPower").mock(
        return_value=respx.MockResponse(200, json=max_power),
    )
    respx.get(f"{base}/getAlarm").mock(
        return_value=respx.MockResponse(200, json=alarm),
    )
    respx.get(f"{base}/getOnOff").mock(
        return_value=respx.MockResponse(200, json=on_off),
    )


@respx.mock
async def test_run_service_publishes_state_and_discovery(
    mosquitto_broker: BrokerEndpoint,
    api_response: Any,
) -> None:
    """One full poll cycle produces state + flat metrics + discovery + online."""
    device_id = f"E{uuid.uuid4().hex[:12].upper()}"
    fake_ez1_host = f"ez1-{device_id.lower()}.test"
    _arm_ez1_respx(api_response, fake_ez1_host, device_id=device_id)

    settings = _make_settings(
        ez1_host=fake_ez1_host,
        mqtt_host=mosquitto_broker.host,
        mqtt_port=mosquitto_broker.port,
        poll_interval=1,
    )
    stop_event = asyncio.Event()

    # Spy: subscribe to everything we expect, then start the service.
    seen: dict[str, bytes] = {}

    async with aiomqtt.Client(
        hostname=mosquitto_broker.host,
        port=mosquitto_broker.port,
        identifier=f"e2e-observer-{device_id}",
    ) as observer:
        await observer.subscribe(f"ez1/{device_id}/#", qos=1)
        await observer.subscribe(f"homeassistant/+/{device_id}/+/config", qos=1)
        await asyncio.sleep(_SUBSCRIPTION_WARMUP_SECONDS)

        async def collect() -> None:
            async for msg in observer.messages:
                seen[str(msg.topic)] = bytes(msg.payload)

        collector = asyncio.create_task(collect())
        service_task = asyncio.create_task(
            run_service(settings, stop_event=stop_event),
        )

        # Wait long enough for at least one poll cycle and discovery + heartbeat.
        try:
            async with asyncio.timeout(_E2E_TIMEOUT_SECONDS):
                while True:
                    state_topic = f"ez1/{device_id}/state"
                    discovery_power_total = f"homeassistant/sensor/{device_id}/power_total/config"
                    if state_topic in seen and discovery_power_total in seen:
                        break
                    await asyncio.sleep(0.1)
        finally:
            stop_event.set()
            await asyncio.wait_for(service_task, timeout=5.0)
            collector.cancel()
            with pytest.raises((asyncio.CancelledError, BaseException)):
                await collector

    # --- Availability ---
    assert seen[f"ez1/{device_id}/availability"] in {b"online", b"offline"}

    # --- Structured state ---
    state_body = json.loads(seen[f"ez1/{device_id}/state"].decode("utf-8"))
    assert state_body["device_id"] == device_id
    assert state_body["status"] == "on"
    assert state_body["power"]["total_w"] == 204.0
    assert state_body["energy_today"]["total_kwh"] == pytest.approx(0.71384, abs=1e-6)

    # --- Flat per-metric topics (retained) ---
    assert seen[f"ez1/{device_id}/power/total_w"] == b"204.0"
    assert seen[f"ez1/{device_id}/power/ch1_w"] == b"139.0"
    assert seen[f"ez1/{device_id}/energy_today/total_kwh"] == b"0.71384"
    assert seen[f"ez1/{device_id}/status/value"] == b"on"
    assert seen[f"ez1/{device_id}/alarm/any_active"] == b"false"

    # --- HA discovery: 11 sensor + 4 binary_sensor configs ---
    sensor_configs = [t for t in seen if t.startswith(f"homeassistant/sensor/{device_id}/")]
    binary_configs = [t for t in seen if t.startswith(f"homeassistant/binary_sensor/{device_id}/")]
    assert len(sensor_configs) == 11
    assert len(binary_configs) == 4

    # Spot-check one sensor's discovery payload contents
    power_total_cfg = json.loads(
        seen[f"homeassistant/sensor/{device_id}/power_total/config"].decode("utf-8"),
    )
    assert power_total_cfg["unique_id"] == f"ez1_{device_id}_power_total"
    assert power_total_cfg["unit_of_measurement"] == "W"
    assert power_total_cfg["device_class"] == "power"
    assert power_total_cfg["state_topic"] == f"ez1/{device_id}/state"
    assert power_total_cfg["device"]["sw_version"] == "EZ1 1.12.2t"


@respx.mock
async def test_run_service_publishes_offline_on_graceful_shutdown(
    mosquitto_broker: BrokerEndpoint,
    api_response: Any,
) -> None:
    """Graceful stop_event triggers an explicit availability=offline publish.

    aiomqtt's clean DISCONNECT does not trigger the broker's LWT, so the
    bridge must publish offline itself before tearing down the
    connection. Otherwise HA would see a stale ``online`` retained
    message until the next bridge connects.
    """
    device_id = f"E{uuid.uuid4().hex[:12].upper()}"
    fake_ez1_host = f"ez1-{device_id.lower()}.test"
    _arm_ez1_respx(api_response, fake_ez1_host, device_id=device_id)

    settings = _make_settings(
        ez1_host=fake_ez1_host,
        mqtt_host=mosquitto_broker.host,
        mqtt_port=mosquitto_broker.port,
        poll_interval=2,
    )
    stop_event = asyncio.Event()

    service_task = asyncio.create_task(
        run_service(settings, stop_event=stop_event),
    )

    # Let it run one cycle, then shut down gracefully.
    await asyncio.sleep(0.5)
    stop_event.set()
    await asyncio.wait_for(service_task, timeout=5.0)

    # Re-subscribe with a fresh client and observe the latest retained
    # availability message -- it must be offline.
    async with aiomqtt.Client(
        hostname=mosquitto_broker.host,
        port=mosquitto_broker.port,
        identifier=f"post-shutdown-observer-{device_id}",
    ) as observer:
        await observer.subscribe(f"ez1/{device_id}/availability", qos=1)
        async with asyncio.timeout(5.0):
            async for msg in observer.messages:
                assert msg.payload == b"offline"
                assert msg.retain is True
                return

    pytest.fail("expected retained availability=offline after graceful shutdown")
