# EZ1 Local API Reference

The canonical, hand-authored EZ1 local-API reference is at
[`_reference/apsystems-ez1-local-api.md`](_reference/apsystems-ez1-local-api.md).
That file is the source of truth for endpoint shapes, verified
real-world payloads, edge cases (inverted on/off semantics,
string-encoded watt values), and the polling recommendations the
bridge follows.

## Where to use which document

| You want to...                                    | Read                                                   |
|---------------------------------------------------|--------------------------------------------------------|
| Know what the inverter answers and how            | [`_reference/apsystems-ez1-local-api.md`](_reference/apsystems-ez1-local-api.md) |
| Know what topics the bridge produces or consumes  | [`mqtt-topics.md`](mqtt-topics.md) |
| Wire the bridge into Home Assistant               | [`home-assistant.md`](home-assistant.md) |
| Understand the repo layout, CI, branch protection | [`architecture.md`](architecture.md) |

## Endpoint summary

The bridge talks to seven EZ1 endpoints. All HTTP `GET`, all return
the same `{"data": {...}, "message": "SUCCESS|FAILED", "deviceId": "..."}`
envelope.

| Method | Endpoint           | Direction | Use in the bridge                                       |
|--------|--------------------|-----------|----------------------------------------------------------|
| GET    | `/getDeviceInfo`   | read      | Resolved at startup + every 24 h to refresh HA discovery |
| GET    | `/getOutputData`   | read      | Polled every cycle (default 20 s) for instantaneous power and energy |
| GET    | `/getMaxPower`     | read      | Polled every cycle; used by the verify read-back after `setMaxPower` writes |
| GET    | `/setMaxPower?p=N` | write     | Issued by the command handler in response to `set/max_power` MQTT messages |
| GET    | `/getAlarm`        | read      | Polled every cycle for the four diagnostic bits |
| GET    | `/getOnOff`        | read      | Polled every cycle for the operational on/off state (note inverted wire format) |
| GET    | `/setOnOff?status=N` | write   | Issued in response to `set/on_off` MQTT messages |

The implementation lives in
[`src/ez1_bridge/adapters/ez1_http.py`](../src/ez1_bridge/adapters/ez1_http.py)
with retry classification (timeouts and 5xx retry; `ConnectError` and
4xx fail fast) and Prometheus instrumentation per call.

## Firmware compatibility

The reference document was authored against firmware **EZ1 1.12.2t**.
The bridge has not been tested against earlier or later firmware
versions. If you run a different firmware:

1. Capture the actual payloads with `curl http://<EZ1>:8050/<endpoint>`
   for each of the seven endpoints.
2. Diff against the verified payloads in
   [`_reference/apsystems-ez1-local-api.md`](_reference/apsystems-ez1-local-api.md).
3. Open an issue with the diff plus your firmware string from
   `getDeviceInfo.devVer` if the response shape changed -- the
   normalizer's defensive parsers will surface drift as
   `ValueError` instead of silently coercing.

## Empirical edge cases (recap)

A short tour of the surprises the reference document captures in
detail. The bridge handles all four; this list is here so a
contributor reading runtime logs knows which behaviour is
documented vs. unexpected.

* **Inverted on/off semantics.** API `status="0"` means **on**,
  `"1"` means **off**. The bridge centralises this in a single
  `_STATUS_MAP` constant in
  [`src/ez1_bridge/domain/normalizer.py`](../src/ez1_bridge/domain/normalizer.py)
  so a refactor cannot drift the direction.
* **`minPower` / `maxPower` arrive as strings.** The bridge parses
  them through `_to_int_watt` which rejects `"800W"`, `"800.0"`,
  hex, and other firmware-drift strings.
* **`e1` / `e2` reset on cold start.** Energy "today" counters are
  really "since last cold start"; mid-day cloud-induced inverter
  shutdowns can reset them. Home Assistant's
  `state_class: total_increasing` handles the resets correctly.
* **Local API stays reachable when the inverter is `off`.** A
  `set_on_off(on=False)` does not lock you out -- the bridge can
  always re-issue `set_on_off(on=True)` to bring output back.
