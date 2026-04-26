"""MQTT command dispatcher — forwards write requests to the EZ1 inverter.

Subscribes to ``{base}/{device_id}/set/+``, validates payloads against the
device's ``minPower``/``maxPower`` bounds, calls the EZ1 API, and publishes
the outcome to ``{base}/{device_id}/result/+``.

Implementation lands in Phase 5.

Backlog (Phase 5)
-----------------
* **Read-back verification after write.** After a ``setMaxPower`` write,
  optionally re-poll ``getMaxPower`` after ~2 s and compare. Make this
  configurable via ``Settings.setmaxpower_verify`` (default ``True``); some
  users prefer fire-and-forget for latency-sensitive automations. On a
  mismatch, publish to the result topic with
  ``{"ok": false, "error": "verify_mismatch", "expected": <int>, "actual": <int>}``
  so Home Assistant immediately surfaces a discarded write.
"""
