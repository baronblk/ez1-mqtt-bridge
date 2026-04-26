"""Home Assistant MQTT discovery payload builder and publisher.

Publishes 11 ``sensor.*`` and 4 ``binary_sensor.*`` configs with retain=true
under ``{discovery_prefix}/{component}/{device_id}/{key}/config``.

Implementation lands in Phase 4.
"""
