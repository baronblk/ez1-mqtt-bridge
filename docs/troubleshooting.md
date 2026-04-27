# Troubleshooting

Field-verified failure modes from real deployments, ordered roughly
by frequency. If your symptom is "the bridge starts cleanly but no
`state_published` events ever appear in the logs", read this page
top-to-bottom — at least three different root causes share that
exact surface symptom.

## Multi-VLAN deployments — bridge needs `network_mode: host`

**Symptom.** Bridge connects to MQTT cleanly, publishes
`availability=online`, runs the HA discovery, then every poll
cycle logs `ez1_unreachable` and the bridge keeps flipping
`availability=offline`. `curl http://<EZ1>:8050/getDeviceInfo`
from the *host* succeeds, so the inverter is alive — only the
container cannot reach it.

**Root cause.** The default [`docker-compose.yml`](../docker-compose.yml)
puts the bridge on a Docker bridge network (`networks: [bridge-net]`).
That bridge has a route to the host's *default* subnet but not to
other VLANs the host can reach via inter-VLAN routing. The Docker
network's gateway does not know about your managed-switch VLANs.

**Fix.** Switch the bridge to host-network mode:

```yaml
services:
  bridge:
    image: ghcr.io/baronblk/ez1-mqtt-bridge:0.1.2
    network_mode: host
    # When in host mode, remove these two keys — they have no effect
    # and Docker will reject the compose file:
    # ports:
    #   - "127.0.0.1:9100:9100"
    # networks:
    #   - bridge-net
    environment:
      EZ1_BRIDGE_EZ1_HOST: 192.168.3.24
      EZ1_BRIDGE_MQTT_HOST: 192.168.2.10
      EZ1_BRIDGE_METRICS_BIND: 127.0.0.1   # tighten from default 0.0.0.0
      # ... rest of the env as usual
```

**Trade-offs to know.** Host mode means port 9100 collides with
anything else the host listens on (other Prometheus exporters,
another bridge instance, etc.). Tighten `EZ1_BRIDGE_METRICS_BIND`
from the default `0.0.0.0` to `127.0.0.1` so the metrics server
is not advertised on every host interface — Prometheus scrapes
loopback fine, and you avoid the public-exposure footgun.

**When you do not need this.** Single-VLAN setups where bridge,
broker, and EZ1 all live on the same subnet — the default
bridge-network compose works as-is.

## EZ1 hardware quirks

The inverter has four field-verified behavioural quirks. Read these
once, save yourself the diagnostic loop later.

### 1. Bluetooth-app connection kills the local HTTP server

When AP EasyPower is connected to the inverter via BLE, the local
HTTP API on port 8050 stops responding to TCP connections.
`curl http://<EZ1>:8050/getDeviceInfo` hangs in connect-timeout.
Closing the app fully (force-quit on iOS, swipe-away on Android)
restores the API after a few seconds.

**Reproduces:** firmware EZ1 1.12.2t, verified live during the
2026-04-27 hardware smoke.

**Operational rule:** before debugging a "bridge can't reach the
inverter" issue, make sure no phone in the household has the
EasyPower app open in the background.

### 2. Parallel HTTP requests are dropped

The local HTTP server accepts only one TCP connection at a time.
Concurrent SYN packets from the same or different clients are
dropped at the device. The bridge already serialises its four
poll endpoints (issue #14, fixed in v0.1.1), but if *another*
script also polls the same inverter, run them sequentially or
not concurrently with the bridge.

A simple test from the host:

```bash
# 5 sequential requests, 5 s apart — all SUCCESS
for i in 1 2 3 4 5; do curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" \
  http://192.168.3.24:8050/getDeviceInfo; sleep 5; done

# 4 parallel requests — all hit connect-timeout
for ep in getOutputData getMaxPower getAlarm getOnOff; do
  curl -s --max-time 10 -o /dev/null -w "$ep: %{http_code} %{time_total}s\n" \
    "http://192.168.3.24:8050/$ep" &
done; wait
```

### 3. WLAN module is volatile

The EZ1's WiFi stack will silently drop off the network after
multi-day uptimes. The inverter is still producing power; only
the local API is unreachable. Only a power-cycle reliably
restores it.

The bridge already handles this gracefully: every cycle that ends
in `httpx.ConnectError` flips `availability=offline`, and the
poll loop retries on the next interval. Home Assistant shows
the device as Unavailable until WLAN comes back. No code change
on the bridge side helps here — escalate to the inverter vendor.

**Operational rule:** if `availability=offline` persists for more
than a poll-interval, ssh into the host and run a fresh `curl`.
If that also hangs, power-cycle the EZ1.

### 4. `e1` / `e2` reset on cold start

The energy "today" counters in `getOutputData` are really
"since last cold start". A mid-day cloud-induced inverter shutdown
can reset them. The bridge faithfully publishes whatever the
inverter reports.

**This is not a data-loss bug in the bridge.** Home Assistant's
Energy Dashboard tolerates `state_class: total_increasing` resets
correctly; the bridge sets that class on the energy sensors via
HA discovery so the dashboard handles the reset for you.

The full per-endpoint reference is at
[`_reference/apsystems-ez1-local-api.md`](_reference/apsystems-ez1-local-api.md);
the `e1`/`e2` reset semantics are documented there in detail.

## Bridge logs show silence after `ha_discovery_published`

**Symptom.** Bridge starts, logs `bridge_starting`,
`ez1_device_resolved`, `metrics_server_started`,
`command_loop_subscribed`, `ha_discovery_published` — then nothing
for minutes. The MQTT broker shows retained `availability=online`
and a fresh `state` JSON on the topic, but the bridge logs are
silent.

**Root cause.** Up to and including v0.1.1, `publish_state` and
`publish_availability` did not emit a success-level log line. The
bridge was working perfectly; the operator was blind.

**Fix.** Upgrade to v0.1.2 or later. Each cycle now logs
`state_published` with the live `power_w`, `energy_today_kwh`,
`status`, and `any_alarm`; each heartbeat logs
`availability_published` with the `online: bool`. Issue #19.

## Healthcheck reports `unhealthy` despite a working bridge

**Symptom.** `docker ps` shows the bridge as `unhealthy` even
though `/metrics` returns 200 when probed manually from the host
or another container.

**Root cause.** Up to and including v0.1.1, the Dockerfile
HEALTHCHECK probed `127.0.0.1:9100` regardless of the configured
`EZ1_BRIDGE_METRICS_PORT`. If you remapped the metrics port to
something other than 9100 (port collision, multi-bridge setup),
the probe targeted a port nobody listened on.

**Fix.** Upgrade to v0.1.2 or later — the HEALTHCHECK now reads
`EZ1_BRIDGE_METRICS_PORT` from the environment. Issue #18.

## See also

* [`docs/architecture.md`](architecture.md) — repository layout
  and the four-coroutine TaskGroup orchestration.
* [`docs/api-reference.md`](api-reference.md) — EZ1 endpoint
  summary, firmware compatibility, and the original reset-on-cold-start
  reference.
* [`docs/home-assistant.md`](home-assistant.md) — HA integration,
  automation examples, and the discovery refresh cadence.
* [`docs/_reference/apsystems-ez1-local-api.md`](_reference/apsystems-ez1-local-api.md)
  — canonical EZ1 local-API documentation with verified payloads.
