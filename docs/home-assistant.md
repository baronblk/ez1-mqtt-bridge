# Home Assistant Integration

End-to-end guide: get the bridge running, watch Home Assistant
auto-discover the device, build automations against the published
state and the writable command topics.

## Prerequisites

* Home Assistant Core / OS / Container with the **MQTT integration**
  configured against the same Mosquitto the bridge uses.
* A reachable APsystems EZ1-M with **Local Mode** enabled (see
  [`_reference/apsystems-ez1-local-api.md`](_reference/apsystems-ez1-local-api.md#aktivierung)
  for the exact AP EasyPower Bluetooth ritual).
* The bridge running -- `docker compose up -d` per the
  [container deployment section in the README](../README.md#container-deployment).

## What you get

When the bridge starts and the inverter responds to `getDeviceInfo`,
Home Assistant's MQTT integration auto-creates **one device card with
fifteen entities**:

| Entity | Type            | Unit  | Source field             |
|--------|-----------------|-------|--------------------------|
| Power Channel 1 | sensor    | W     | `power.ch1_w`            |
| Power Channel 2 | sensor    | W     | `power.ch2_w`            |
| Power Total     | sensor    | W     | `power.total_w`          |
| Energy Today Channel 1 | sensor | kWh | `energy_today.ch1_kwh` |
| Energy Today Channel 2 | sensor | kWh | `energy_today.ch2_kwh` |
| Energy Today Total     | sensor | kWh | `energy_today.total_kwh` |
| Energy Lifetime Channel 1 | sensor | kWh | `energy_lifetime.ch1_kwh` |
| Energy Lifetime Channel 2 | sensor | kWh | `energy_lifetime.ch2_kwh` |
| Energy Lifetime Total     | sensor | kWh | `energy_lifetime.total_kwh` |
| Max Power           | sensor (diagnostic) | W | `max_power_w`        |
| Status              | sensor (diagnostic) | -- | `status`             |
| Alarm Off Grid      | binary_sensor (problem) | -- | `alarms.off_grid` |
| Alarm Output Fault  | binary_sensor (problem) | -- | `alarms.output_fault` |
| Alarm DC1 Short     | binary_sensor (problem) | -- | `alarms.dc1_short` |
| Alarm DC2 Short     | binary_sensor (problem) | -- | `alarms.dc2_short` |

The `device_class` annotations (`power`, `energy`, `problem`) tell
Home Assistant how to format values, which icons to use, and which
dashboard cards to suggest.

## Verify the integration after first start

1. Wait ~30 s after `docker compose up -d` for the first poll cycle
   to complete and discovery to publish.
2. In Home Assistant, navigate to *Settings → Devices & Services →
   MQTT* and look for an `APsystems EZ1 {device_id}` entry.
3. Click the device card. All 15 entities should appear with
   non-stale values (`state_class: total_increasing` for energy
   counters, `measurement` for instantaneous power).
4. Check **Availability**: the card-level "Available" indicator
   reads from the bridge's availability topic. If the bridge is
   stopped or the inverter is night-offline, every entity goes
   *Unavailable* simultaneously rather than reporting stale values.

If the device does not appear:

* `mosquitto_sub -t "homeassistant/+/{device_id}/+/config" -v` --
  is discovery actually being published?
* `docker compose logs bridge | grep ha_discovery_published` --
  did the bridge log a successful discovery cycle?
* Toggle the MQTT integration (re-load it) -- HA caches discovery
  topics on a per-restart basis.

## Automation examples

The examples assume `device_id = "E17010000783"` and `base_topic = "ez1"`
(the project defaults). Replace with your own values in `.env`.

### Throttle output to 600 W during midday hours

Reduces export to the grid when local consumption is low; useful in
markets with negative daytime feed-in tariffs or for staying within
a 600 W grid-connection limit.

```yaml
automation:
  - alias: "EZ1 midday throttle"
    description: "Limit output to 600 W between 11:00 and 14:00"
    trigger:
      - platform: time
        at: "11:00:00"
    action:
      - service: mqtt.publish
        data:
          topic: "ez1/E17010000783/set/max_power"
          payload: "600"
          qos: 1

  - alias: "EZ1 midday throttle release"
    description: "Restore 800 W after midday window"
    trigger:
      - platform: time
        at: "14:00:00"
    action:
      - service: mqtt.publish
        data:
          topic: "ez1/E17010000783/set/max_power"
          payload: "800"
          qos: 1
```

### React to a verify_mismatch result

If the inverter silently rejected a `setMaxPower` write (the verify
read-back returns a different value), surface a notification rather
than letting the discrepancy hide.

```yaml
automation:
  - alias: "EZ1 setMaxPower verify failure"
    trigger:
      - platform: mqtt
        topic: "ez1/E17010000783/result/max_power"
    condition:
      - "{{ trigger.payload_json.ok == false and trigger.payload_json.error == 'verify_mismatch' }}"
    action:
      - service: notify.mobile_app
        data:
          title: "EZ1 inverter rejected throttle"
          message: >-
            Tried to set {{ trigger.payload_json.expected }} W,
            inverter reports {{ trigger.payload_json.actual }} W.
```

### Power-cap based on house consumption

Combine the bridge with a smart-meter integration so the inverter
limit follows household demand. (Example assumes a `sensor.house_load_w`
exists.)

```yaml
automation:
  - alias: "EZ1 dynamic cap follows house load"
    trigger:
      - platform: state
        entity_id: sensor.house_load_w
    action:
      - service: mqtt.publish
        data:
          topic: "ez1/E17010000783/set/max_power"
          payload: >-
            {{ [800, [30, states('sensor.house_load_w') | int(0)] | max] | min }}
          qos: 1
    mode: queued
    max: 5
```

The Jinja clamps the load to `[30, 800]` (the EZ1's documented
power range) and uses `mode: queued` so a fast burst of changes
doesn't trample older requests.

### Shut the inverter off on alarm

If any of the four alarm bits flips to `on`, shut the output down
and notify. Useful for the DC short-circuit alarms which point at
panel-side faults that you don't want to ignore.

```yaml
automation:
  - alias: "EZ1 shut down on alarm"
    trigger:
      - platform: state
        entity_id:
          - binary_sensor.alarm_dc1_short
          - binary_sensor.alarm_dc2_short
        to: "on"
    action:
      - service: mqtt.publish
        data:
          topic: "ez1/E17010000783/set/on_off"
          payload: "off"
          qos: 1
      - service: notify.mobile_app
        data:
          title: "EZ1 alarm fired"
          message: >-
            {{ trigger.entity_id }} = on. Inverter has been turned
            off; investigate the affected DC channel.
```

The `set/on_off` accepts `on`/`off`/`1`/`0`; the bridge handles the
EZ1's inverted wire format (`status="0"` = on) so the automation
stays human-readable.

## Energy dashboard wiring

Home Assistant's **Energy Dashboard** picks up `energy_*` sensors
automatically when they have `device_class: energy` and
`state_class: total_increasing`, both of which the bridge sets via
discovery. Walk through:

1. *Settings → Dashboards → Energy*.
2. Under *Solar Panels*, *Add solar production*.
3. Pick `sensor.energy_lifetime_total` (or `sensor.energy_today_total`
   if you prefer the per-day flavour, but be aware HA computes daily
   roll-ups itself; using `lifetime_total` plays nicer with the
   built-in delta calculation).

Energy Dashboard handles the inverter's per-cold-start reset of
`energy_today` correctly because `state_class: total_increasing`
tolerates resets to zero.

## Troubleshooting

| Symptom                                  | Likely cause                                                       | Fix |
|------------------------------------------|--------------------------------------------------------------------|-----|
| Device doesn't appear in HA             | Discovery prefix mismatch                                          | Check `EZ1_BRIDGE_MQTT_DISCOVERY_PREFIX` matches HA's MQTT discovery prefix (default `homeassistant`) |
| Entities stuck on "Unavailable"          | Bridge offline or LWT triggered                                    | `docker compose logs bridge`; verify availability topic (`mosquitto_sub -t ez1/+/availability -v`) |
| `set/max_power` returns `out_of_range`   | Value outside `[30, 800]`                                          | Range comes from `getDeviceInfo`; clamp the automation's input |
| `set/on_off` returns `invalid_payload`   | Payload neither `on`/`off` nor `1`/`0`                             | Ensure the automation publishes a string, not an int |
| Energy Dashboard shows zeros at midnight | Using `energy_today_total` which the inverter resets at cold start | Switch to `energy_lifetime_total` (HA computes daily delta) |
| Discovery doesn't refresh after firmware upgrade | 24 h discovery refresh cadence                              | Restart bridge to force immediate re-publish, or wait |

For deeper debugging, the bridge exposes Prometheus metrics on
`:9100/metrics` (see `docs/architecture.md` for the metric set).
`ez1_api_errors_total` and `ez1_mqtt_publish_total` are usually
the first two metrics to inspect when something looks wrong.
