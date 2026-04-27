"""Home Assistant MQTT discovery payload builder.

Pure-function module: a single :func:`build_discovery_messages` takes a
:class:`DeviceInfo` plus the topic-root settings and returns a list of
:class:`DiscoveryMessage` objects ready to hand to the publisher. No
state lives between calls -- the function is called once on the first
successful poll and again every 24 h so a firmware upgrade or SSID
change updates Home Assistant cleanly.

The 11 sensors and 4 binary_sensors live as data in two module-level
specification tables, not as 15 individual functions or a long if/elif
chain. Adding a new HA field (e.g. ``suggested_display_precision``)
costs one column in the tuple, not a refactor. Reading the file should
feel like reading a config sheet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from ez1_bridge import topics
from ez1_bridge.domain.models import DeviceInfo

#: Manufacturer string published in the HA ``device`` block. Hard-coded
#: rather than passed through from settings -- there is exactly one
#: vendor this bridge supports, and surfacing that string anywhere else
#: invites drift.
_MANUFACTURER: Final[str] = "APsystems"
_MODEL: Final[str] = "EZ1"


@dataclass(frozen=True, slots=True)
class DiscoveryMessage:
    """A single MQTT discovery message ready for the publisher.

    ``topic`` is the full discovery topic (with prefix and component);
    ``payload`` is the JSON-serialisable config dict; ``retain`` is
    always ``True`` for HA discovery (set explicitly so callers do not
    have to remember).
    """

    topic: str
    payload: dict[str, Any]
    retain: bool = True


@dataclass(frozen=True, slots=True)
class _SensorSpec:
    """One row of the sensor specification table."""

    key: str
    name: str
    value_template: str
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    icon: str | None = None
    entity_category: str | None = None


@dataclass(frozen=True, slots=True)
class _BinarySensorSpec:
    """One row of the binary-sensor specification table."""

    key: str
    name: str
    value_template: str
    device_class: str = "problem"


# --- Sensor table (11 entries) ------------------------------------------

_SENSOR_SPECS: Final[tuple[_SensorSpec, ...]] = (
    _SensorSpec(
        key="power_ch1",
        name="Power Channel 1",
        value_template="{{ value_json.power.ch1_w }}",
        unit="W",
        device_class="power",
        state_class="measurement",
    ),
    _SensorSpec(
        key="power_ch2",
        name="Power Channel 2",
        value_template="{{ value_json.power.ch2_w }}",
        unit="W",
        device_class="power",
        state_class="measurement",
    ),
    _SensorSpec(
        key="power_total",
        name="Power Total",
        value_template="{{ value_json.power.total_w }}",
        unit="W",
        device_class="power",
        state_class="measurement",
    ),
    _SensorSpec(
        key="energy_today_ch1",
        name="Energy Today Channel 1",
        value_template="{{ value_json.energy_today.ch1_kwh }}",
        unit="kWh",
        device_class="energy",
        state_class="total_increasing",
    ),
    _SensorSpec(
        key="energy_today_ch2",
        name="Energy Today Channel 2",
        value_template="{{ value_json.energy_today.ch2_kwh }}",
        unit="kWh",
        device_class="energy",
        state_class="total_increasing",
    ),
    _SensorSpec(
        key="energy_today_total",
        name="Energy Today Total",
        value_template="{{ value_json.energy_today.total_kwh }}",
        unit="kWh",
        device_class="energy",
        state_class="total_increasing",
    ),
    _SensorSpec(
        key="energy_lifetime_ch1",
        name="Energy Lifetime Channel 1",
        value_template="{{ value_json.energy_lifetime.ch1_kwh }}",
        unit="kWh",
        device_class="energy",
        state_class="total_increasing",
    ),
    _SensorSpec(
        key="energy_lifetime_ch2",
        name="Energy Lifetime Channel 2",
        value_template="{{ value_json.energy_lifetime.ch2_kwh }}",
        unit="kWh",
        device_class="energy",
        state_class="total_increasing",
    ),
    _SensorSpec(
        key="energy_lifetime_total",
        name="Energy Lifetime Total",
        value_template="{{ value_json.energy_lifetime.total_kwh }}",
        unit="kWh",
        device_class="energy",
        state_class="total_increasing",
    ),
    _SensorSpec(
        key="max_power",
        name="Max Power",
        value_template="{{ value_json.max_power_w }}",
        unit="W",
        device_class="power",
        state_class="measurement",
        entity_category="diagnostic",
    ),
    _SensorSpec(
        key="status",
        name="Status",
        value_template="{{ value_json.status }}",
        icon="mdi:power",
        entity_category="diagnostic",
    ),
)

# --- Binary-sensor table (4 entries) ------------------------------------

_BINARY_SENSOR_SPECS: Final[tuple[_BinarySensorSpec, ...]] = (
    _BinarySensorSpec(
        key="alarm_off_grid",
        name="Alarm Off Grid",
        value_template="{{ 'ON' if value_json.alarms.off_grid else 'OFF' }}",
    ),
    _BinarySensorSpec(
        key="alarm_output_fault",
        name="Alarm Output Fault",
        value_template="{{ 'ON' if value_json.alarms.output_fault else 'OFF' }}",
    ),
    _BinarySensorSpec(
        key="alarm_dc1_short",
        name="Alarm DC1 Short Circuit",
        value_template="{{ 'ON' if value_json.alarms.dc1_short else 'OFF' }}",
    ),
    _BinarySensorSpec(
        key="alarm_dc2_short",
        name="Alarm DC2 Short Circuit",
        value_template="{{ 'ON' if value_json.alarms.dc2_short else 'OFF' }}",
    ),
)


# --- Builders -----------------------------------------------------------


def _device_block(info: DeviceInfo) -> dict[str, Any]:
    """Common ``device`` block referenced by every entity payload."""
    return {
        "identifiers": [info.device_id],
        "manufacturer": _MANUFACTURER,
        "model": _MODEL,
        "sw_version": info.firmware_version,
        "name": f"{_MANUFACTURER} {_MODEL} {info.device_id}",
    }


def _availability_block(base_topic: str, device_id: str) -> dict[str, str]:
    """Shared availability block (``online`` / ``offline``) for every entity."""
    return {
        "availability_topic": topics.availability(base_topic, device_id),
        "payload_available": topics.AVAILABILITY_ONLINE,
        "payload_not_available": topics.AVAILABILITY_OFFLINE,
    }


def _entity_payload(
    *,
    info: DeviceInfo,
    base_topic: str,
    state_topic: str,
    key: str,
    name: str,
    value_template: str,
    extras: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the discovery payload for a single entity."""
    unique_id = f"ez1_{info.device_id}_{key}"
    payload: dict[str, Any] = {
        "unique_id": unique_id,
        "object_id": unique_id,
        "name": name,
        "state_topic": state_topic,
        "value_template": value_template,
        "device": _device_block(info),
        **_availability_block(base_topic, info.device_id),
        **extras,
    }
    # Filter out None values from the extras (avoid emitting nulls in JSON).
    return {k: v for k, v in payload.items() if v is not None}


def build_discovery_messages(
    info: DeviceInfo,
    *,
    base_topic: str,
    discovery_prefix: str,
) -> list[DiscoveryMessage]:
    """Return the 15 :class:`DiscoveryMessage` objects for one inverter.

    11 ``sensor`` entries + 4 ``binary_sensor`` entries. Order is stable
    (sensor table first, then binary-sensor table, both in declaration
    order) so snapshot tests keep working across runs.
    """
    state_topic = topics.state(base_topic, info.device_id)
    messages: list[DiscoveryMessage] = []

    for spec in _SENSOR_SPECS:
        extras: dict[str, Any] = {}
        if spec.unit is not None:
            extras["unit_of_measurement"] = spec.unit
        if spec.device_class is not None:
            extras["device_class"] = spec.device_class
        if spec.state_class is not None:
            extras["state_class"] = spec.state_class
        if spec.icon is not None:
            extras["icon"] = spec.icon
        if spec.entity_category is not None:
            extras["entity_category"] = spec.entity_category

        payload = _entity_payload(
            info=info,
            base_topic=base_topic,
            state_topic=state_topic,
            key=spec.key,
            name=spec.name,
            value_template=spec.value_template,
            extras=extras,
        )
        messages.append(
            DiscoveryMessage(
                topic=topics.discovery(discovery_prefix, "sensor", info.device_id, spec.key),
                payload=payload,
                retain=topics.RETAIN["discovery"],
            ),
        )

    for bspec in _BINARY_SENSOR_SPECS:
        payload = _entity_payload(
            info=info,
            base_topic=base_topic,
            state_topic=state_topic,
            key=bspec.key,
            name=bspec.name,
            value_template=bspec.value_template,
            extras={"device_class": bspec.device_class},
        )
        messages.append(
            DiscoveryMessage(
                topic=topics.discovery(
                    discovery_prefix,
                    "binary_sensor",
                    info.device_id,
                    bspec.key,
                ),
                payload=payload,
                retain=topics.RETAIN["discovery"],
            ),
        )

    return messages
