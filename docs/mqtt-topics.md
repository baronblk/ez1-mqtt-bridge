# MQTT Topic Reference

Every topic the bridge produces or consumes, the retain flag it sets,
and the payload shape on the wire. Three sections: *published* (the
bridge → broker), *subscribed* (broker → bridge), and *Home Assistant
discovery* (the bridge announces entities).

The placeholders `{base}` and `{device_id}` are configurable; the
defaults are `ez1` and the value of `getDeviceInfo.deviceId`
(e.g. `E17010000783`). The discovery prefix `{discovery_prefix}`
defaults to `homeassistant`.

## Published topics

The bridge emits these. Retain semantics flow from
`src/ez1_bridge/topics.py::RETAIN`; a regression test guards that the
publisher reads the map directly rather than hard-coding flags.

| Topic                                         | Retain | QoS | Payload | Updated |
|-----------------------------------------------|:------:|:---:|---------|---------|
| `{base}/{device_id}/availability`             |  ✅    |  1  | `online` / `offline` | On connect (online), every 30 s heartbeat, LWT (offline) |
| `{base}/{device_id}/state`                    |  ✅    |  1  | JSON (see schema below) | Every poll cycle (default 20 s) |
| `{base}/{device_id}/power/ch1_w`              |  ✅    |  1  | float as string, e.g. `"139.0"` | Every poll cycle |
| `{base}/{device_id}/power/ch2_w`              |  ✅    |  1  | float as string | Every poll cycle |
| `{base}/{device_id}/power/total_w`            |  ✅    |  1  | float as string | Every poll cycle |
| `{base}/{device_id}/energy_today/ch1_kwh`     |  ✅    |  1  | float as string | Every poll cycle |
| `{base}/{device_id}/energy_today/ch2_kwh`     |  ✅    |  1  | float as string | Every poll cycle |
| `{base}/{device_id}/energy_today/total_kwh`   |  ✅    |  1  | float as string | Every poll cycle |
| `{base}/{device_id}/energy_lifetime/ch1_kwh`  |  ✅    |  1  | float as string | Every poll cycle |
| `{base}/{device_id}/energy_lifetime/ch2_kwh`  |  ✅    |  1  | float as string | Every poll cycle |
| `{base}/{device_id}/energy_lifetime/total_kwh`|  ✅    |  1  | float as string | Every poll cycle |
| `{base}/{device_id}/max_power_w/value`        |  ✅    |  1  | int as string, e.g. `"800"` | Every poll cycle |
| `{base}/{device_id}/status/value`             |  ✅    |  1  | `on` / `off` | Every poll cycle |
| `{base}/{device_id}/alarm/off_grid`           |  ✅    |  1  | `true` / `false` | Every poll cycle |
| `{base}/{device_id}/alarm/output_fault`       |  ✅    |  1  | `true` / `false` | Every poll cycle |
| `{base}/{device_id}/alarm/dc1_short`          |  ✅    |  1  | `true` / `false` | Every poll cycle |
| `{base}/{device_id}/alarm/dc2_short`          |  ✅    |  1  | `true` / `false` | Every poll cycle |
| `{base}/{device_id}/alarm/any_active`         |  ✅    |  1  | `true` / `false` | Every poll cycle |
| `{base}/{device_id}/result/max_power`         |  ❌    |  1  | JSON command result (see below) | After every `setMaxPower` write |
| `{base}/{device_id}/result/on_off`            |  ❌    |  1  | JSON command result | After every `setOnOff` write |

Result topics are **not** retained — they are events, not state. A
late subscriber should never see a stale write outcome.

### `state` payload schema

```json
{
  "ts": "2026-04-26T18:00:00+00:00",
  "device_id": "E17010000783",
  "power": {
    "ch1_w": 139.0,
    "ch2_w": 65.0,
    "total_w": 204.0
  },
  "energy_today": {
    "ch1_kwh": 0.28731,
    "ch2_kwh": 0.42653,
    "total_kwh": 0.71384
  },
  "energy_lifetime": {
    "ch1_kwh": 87.43068,
    "ch2_kwh": 111.24305,
    "total_kwh": 198.67373
  },
  "max_power_w": 800,
  "status": "on",
  "alarms": {
    "off_grid": false,
    "output_fault": false,
    "dc1_short": false,
    "dc2_short": false,
    "any_active": false
  }
}
```

Field types are stable per `src/ez1_bridge/domain/models.py`:
power and energy values are floats, `max_power_w` is an int,
`status` is a `Literal["on", "off"]`, alarm bits are bools.

### `result/*` payload schema

Success:

```json
{
  "ok": true,
  "ts": "2026-04-26T18:00:00+00:00",
  "value": "600"
}
```

Failure variants — `error` is one of `invalid_payload`, `out_of_range`,
`transport_error`, `verify_mismatch`. Stable identifiers are intended
for Home Assistant automations to match against.

```json
{"ok": false, "ts": "...", "error": "invalid_payload",  "detail": "expected integer watts, got 'abc'"}
{"ok": false, "ts": "...", "error": "out_of_range",     "detail": "value 1000 outside [30, 800]"}
{"ok": false, "ts": "...", "error": "transport_error",  "detail": "ConnectError: ..."}
{"ok": false, "ts": "...", "error": "verify_mismatch",  "detail": "expected 600, actual 800",
                                                          "expected": 600, "actual": 800}
```

## Subscribed topics

The bridge listens on these. Retain is irrelevant on the subscribe
side — the bridge consumes the message and emits a `result/*` event
in response.

| Topic                                  | Payload                          | Validation                                   | Result topic                          |
|----------------------------------------|----------------------------------|----------------------------------------------|---------------------------------------|
| `{base}/{device_id}/set/max_power`     | integer watts as string, `"600"` | parsed via `int()`; rejected for empties, units, decimals, non-numeric | `{base}/{device_id}/result/max_power` |
| `{base}/{device_id}/set/max_power`     | range check                      | must be within `[minPower, maxPower]` from `getDeviceInfo` | `result/max_power` with `error=out_of_range` |
| `{base}/{device_id}/set/on_off`        | `"on"` or `"off"` (case-insensitive); also `"1"`/`"0"` accepted | `parse_on_off_payload`; rejects anything else with `error=invalid_payload` | `{base}/{device_id}/result/on_off` |

A single subscription pattern `{base}/{device_id}/set/+` covers both
commands. Unknown command names (e.g. `set/foobar`) are logged at
WARN level and dropped silently — no result topic is emitted.

### Verify read-back

When `EZ1_BRIDGE_SETMAXPOWER_VERIFY=true` (default), the bridge waits
~2 s after a `setMaxPower` write, re-reads `getMaxPower`, and emits a
`verify_mismatch` result if the inverter ignored the write. Disable
this in latency-sensitive automations by setting it to `false`; the
bridge is then fire-and-forget and a silently rejected write becomes
invisible to the result topic.

## Home Assistant discovery topics

The bridge auto-publishes 15 entity-config topics under the discovery
prefix on the first successful poll cycle and re-publishes every 24 h
(or whenever the `DeviceInfo` changes after a firmware upgrade).

| Component       | Object ID / unique_id                  | State topic              | Notes                              |
|-----------------|----------------------------------------|--------------------------|------------------------------------|
| `sensor`        | `ez1_{device_id}_power_ch1`            | `{base}/{device_id}/state` | unit `W`, `device_class=power`     |
| `sensor`        | `ez1_{device_id}_power_ch2`            | `{base}/{device_id}/state` | unit `W`                           |
| `sensor`        | `ez1_{device_id}_power_total`          | `{base}/{device_id}/state` | unit `W`                           |
| `sensor`        | `ez1_{device_id}_energy_today_ch1`     | `{base}/{device_id}/state` | unit `kWh`, `state_class=total_increasing` |
| `sensor`        | `ez1_{device_id}_energy_today_ch2`     | `{base}/{device_id}/state` | unit `kWh`                         |
| `sensor`        | `ez1_{device_id}_energy_today_total`   | `{base}/{device_id}/state` | unit `kWh`                         |
| `sensor`        | `ez1_{device_id}_energy_lifetime_ch1`  | `{base}/{device_id}/state` | unit `kWh`                         |
| `sensor`        | `ez1_{device_id}_energy_lifetime_ch2`  | `{base}/{device_id}/state` | unit `kWh`                         |
| `sensor`        | `ez1_{device_id}_energy_lifetime_total`| `{base}/{device_id}/state` | unit `kWh`                         |
| `sensor`        | `ez1_{device_id}_max_power`            | `{base}/{device_id}/state` | unit `W`, `entity_category=diagnostic` |
| `sensor`        | `ez1_{device_id}_status`               | `{base}/{device_id}/state` | textual `on`/`off`, `entity_category=diagnostic` |
| `binary_sensor` | `ez1_{device_id}_alarm_off_grid`       | `{base}/{device_id}/state` | `device_class=problem`             |
| `binary_sensor` | `ez1_{device_id}_alarm_output_fault`   | `{base}/{device_id}/state` | `device_class=problem`             |
| `binary_sensor` | `ez1_{device_id}_alarm_dc1_short`      | `{base}/{device_id}/state` | `device_class=problem`             |
| `binary_sensor` | `ez1_{device_id}_alarm_dc2_short`      | `{base}/{device_id}/state` | `device_class=problem`             |

The discovery config topic for each entry is
`{discovery_prefix}/{component}/{device_id}/{key}/config`,
all retained.

Every payload includes a shared `device` block so Home Assistant
groups all 15 entities under one device card:

```json
{
  "identifiers": ["E17010000783"],
  "manufacturer": "APsystems",
  "model": "EZ1",
  "sw_version": "EZ1 1.12.2t",
  "name": "APsystems EZ1 E17010000783"
}
```

The shared `availability_topic` (`{base}/{device_id}/availability`)
plus `payload_available: "online"` / `payload_not_available: "offline"`
makes the entire device card unavailable in the UI when the bridge is
down or the inverter is night-offline.

## Diagnostic helpers

```bash
# Watch every topic the bridge produces
mosquitto_sub -h "$MQTT_HOST" -u "$MQTT_USER" -P "$MQTT_PASSWORD" \
  -t "ez1/#" -v

# Watch only the structured state
mosquitto_sub -h "$MQTT_HOST" -u "$MQTT_USER" -P "$MQTT_PASSWORD" \
  -t "ez1/E17010000783/state" -v

# Trigger a max_power write and observe the result
mosquitto_pub -h "$MQTT_HOST" -u "$MQTT_USER" -P "$MQTT_PASSWORD" \
  -t "ez1/E17010000783/set/max_power" -m "600" -q 1
mosquitto_sub -h "$MQTT_HOST" -u "$MQTT_USER" -P "$MQTT_PASSWORD" \
  -t "ez1/E17010000783/result/max_power" -C 1 -v

# Inspect the discovery payloads HA picked up
mosquitto_sub -h "$MQTT_HOST" -u "$MQTT_USER" -P "$MQTT_PASSWORD" \
  -t "homeassistant/+/E17010000783/+/config" -v
```

The canonical EZ1 endpoint reference (verified payloads, edge cases)
is in [`_reference/apsystems-ez1-local-api.md`](_reference/apsystems-ez1-local-api.md).
