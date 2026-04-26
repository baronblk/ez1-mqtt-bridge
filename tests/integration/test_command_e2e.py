"""End-to-end Phase-5 integration test for the command handler.

Drives a respx-mocked EZ1 plus a real Mosquitto broker through
:func:`run_service`, publishes set-commands from an external observer,
and asserts the result topic carries the expected structured payload.

This is the test that catches "wiring against the real broker plus a
mocked inverter actually round-trips" regressions that a unit test
cannot, because it exercises the real MQTT subscribe + dispatch path
inside the running TaskGroup.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
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
    setmaxpower_verify: bool,
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
        setmaxpower_verify=setmaxpower_verify,
    )


def _arm_ez1_respx(
    api_response: Callable[[str], dict[str, Any]],
    host: str,
    *,
    device_id: str,
) -> dict[str, respx.Route]:
    """Mount fixture responses for every read endpoint and capture write routes."""
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
    set_max = respx.get(f"{base}/setMaxPower").mock(
        return_value=respx.MockResponse(
            200,
            json={"data": {"maxPower": "600"}, "message": "SUCCESS", "deviceId": device_id},
        ),
    )
    set_on_off = respx.get(f"{base}/setOnOff").mock(
        return_value=respx.MockResponse(
            200,
            json={"data": {"status": "1"}, "message": "SUCCESS", "deviceId": device_id},
        ),
    )
    return {"set_max_power": set_max, "set_on_off": set_on_off}


async def _wait_for_message_on(
    client: aiomqtt.Client,
    expected_topic: str,
    *,
    timeout: float = _E2E_TIMEOUT_SECONDS,
) -> aiomqtt.Message:
    async with asyncio.timeout(timeout):
        async for msg in client.messages:
            if str(msg.topic) == expected_topic:
                return msg
    err = f"no message on {expected_topic} within {timeout}s"
    raise TimeoutError(err)


@respx.mock
async def test_set_max_power_round_trip(
    mosquitto_broker: BrokerEndpoint,
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    """A setMaxPower command produces an ok=true result with the value."""
    device_id = f"E{uuid.uuid4().hex[:12].upper()}"
    fake_ez1_host = f"ez1-{device_id.lower()}.test"
    routes = _arm_ez1_respx(api_response, fake_ez1_host, device_id=device_id)
    settings = _make_settings(
        ez1_host=fake_ez1_host,
        mqtt_host=mosquitto_broker.host,
        mqtt_port=mosquitto_broker.port,
        setmaxpower_verify=False,
    )
    stop_event = asyncio.Event()

    async with aiomqtt.Client(
        hostname=mosquitto_broker.host,
        port=mosquitto_broker.port,
        identifier=f"cmd-observer-{device_id}",
    ) as observer:
        await observer.subscribe(f"ez1/{device_id}/result/+", qos=1)
        await asyncio.sleep(_SUBSCRIPTION_WARMUP_SECONDS)

        service_task = asyncio.create_task(run_service(settings, stop_event=stop_event))
        # Give the bridge time to start, resolve device_id, subscribe to set/+.
        await asyncio.sleep(1.5)

        await observer.publish(
            f"ez1/{device_id}/set/max_power",
            payload="600",
            qos=1,
        )

        result_topic = f"ez1/{device_id}/result/max_power"
        msg = await _wait_for_message_on(observer, result_topic, timeout=8.0)
        result = json.loads(bytes(msg.payload).decode("utf-8"))

        stop_event.set()
        await asyncio.wait_for(service_task, timeout=5.0)

    assert result["ok"] is True
    assert result["value"] == "600"
    assert "error" not in result
    assert routes["set_max_power"].call_count == 1
    assert routes["set_max_power"].calls.last.request.url.params["p"] == "600"


@respx.mock
async def test_set_on_off_round_trip(
    mosquitto_broker: BrokerEndpoint,
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    """A setOnOff command produces an ok=true result and inverts the wire bit correctly."""
    device_id = f"E{uuid.uuid4().hex[:12].upper()}"
    fake_ez1_host = f"ez1-{device_id.lower()}.test"
    routes = _arm_ez1_respx(api_response, fake_ez1_host, device_id=device_id)
    settings = _make_settings(
        ez1_host=fake_ez1_host,
        mqtt_host=mosquitto_broker.host,
        mqtt_port=mosquitto_broker.port,
        setmaxpower_verify=False,
    )
    stop_event = asyncio.Event()

    async with aiomqtt.Client(
        hostname=mosquitto_broker.host,
        port=mosquitto_broker.port,
        identifier=f"cmd-observer-{device_id}",
    ) as observer:
        await observer.subscribe(f"ez1/{device_id}/result/+", qos=1)
        await asyncio.sleep(_SUBSCRIPTION_WARMUP_SECONDS)

        service_task = asyncio.create_task(run_service(settings, stop_event=stop_event))
        await asyncio.sleep(1.5)

        await observer.publish(f"ez1/{device_id}/set/on_off", payload="off", qos=1)

        msg = await _wait_for_message_on(
            observer,
            f"ez1/{device_id}/result/on_off",
            timeout=8.0,
        )
        result = json.loads(bytes(msg.payload).decode("utf-8"))

        stop_event.set()
        await asyncio.wait_for(service_task, timeout=5.0)

    assert result["ok"] is True
    assert result["value"] == "off"
    assert routes["set_on_off"].call_count == 1
    # Inverted on the wire: "off" -> status=1
    assert routes["set_on_off"].calls.last.request.url.params["status"] == "1"


@respx.mock
async def test_set_max_power_out_of_range_publishes_structured_error(
    mosquitto_broker: BrokerEndpoint,
    api_response: Callable[[str], dict[str, Any]],
) -> None:
    """A value above the device's max_power produces ok=false + out_of_range."""
    device_id = f"E{uuid.uuid4().hex[:12].upper()}"
    fake_ez1_host = f"ez1-{device_id.lower()}.test"
    routes = _arm_ez1_respx(api_response, fake_ez1_host, device_id=device_id)
    settings = _make_settings(
        ez1_host=fake_ez1_host,
        mqtt_host=mosquitto_broker.host,
        mqtt_port=mosquitto_broker.port,
        setmaxpower_verify=False,
    )
    stop_event = asyncio.Event()

    async with aiomqtt.Client(
        hostname=mosquitto_broker.host,
        port=mosquitto_broker.port,
        identifier=f"cmd-observer-{device_id}",
    ) as observer:
        await observer.subscribe(f"ez1/{device_id}/result/+", qos=1)
        await asyncio.sleep(_SUBSCRIPTION_WARMUP_SECONDS)

        service_task = asyncio.create_task(run_service(settings, stop_event=stop_event))
        await asyncio.sleep(1.5)

        # 1000 is above the documented 800 W max_power for this device.
        await observer.publish(
            f"ez1/{device_id}/set/max_power",
            payload="1000",
            qos=1,
        )

        msg = await _wait_for_message_on(
            observer,
            f"ez1/{device_id}/result/max_power",
            timeout=8.0,
        )
        result = json.loads(bytes(msg.payload).decode("utf-8"))

        stop_event.set()
        await asyncio.wait_for(service_task, timeout=5.0)

    assert result["ok"] is False
    assert result["error"] == "out_of_range"
    assert "1000" in result["detail"]
    # Critical guard: the EZ1 set endpoint must NOT have been called.
    assert routes["set_max_power"].call_count == 0
