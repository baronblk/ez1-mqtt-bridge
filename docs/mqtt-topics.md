# MQTT Topic Reference

> **Status:** placeholder — populated in Phase 9.

Will contain:

- Complete topic table: state (`{base}/{device_id}/state`), flat per-metric
  topics, command topics (`set/+`), result topics (`result/+`), availability,
  and Home Assistant discovery.
- JSON schemas for the structured state payload and the result payloads.
- Retain / QoS / LWT semantics per topic.
- Examples using `mosquitto_sub` and `mosquitto_pub`.

The canonical EZ1 endpoint reference is in
[`_reference/apsystems-ez1-local-api.md`](_reference/apsystems-ez1-local-api.md).
