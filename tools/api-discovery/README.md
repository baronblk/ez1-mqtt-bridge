# EZ1-M Local API Discovery

Read-only, rate-limited reverse-engineering of the APsystems EZ1-M
inverter's local HTTP server on port 8050. Goal: confirm whether the
seven documented endpoints are the complete public surface, or whether
a wordlist-driven probe surfaces undocumented paths worth wiring into
the bridge in v0.2.x.

**Result of the 2026-04-27 run against E17010000783 (firmware EZ1 1.12.2t):
the documented seven endpoints are the complete surface. No hidden read
paths, no diagnostic endpoints, no dashboard, no debug widening.** The
findings below also surface two non-functional but operationally useful
facts (the validation-error envelope and the bare-bones HTTP stack).

## Tools in this directory

| File | Purpose |
|------|---------|
| [`probe_endpoints.py`](probe_endpoints.py) | Phase 1 — wordlist probe over 68 plausible endpoints, GET only, with collision-aware retry |
| [`probe_methods.py`](probe_methods.py) | Phase 2 — header / method probes against known endpoints (OPTIONS, HEAD, Accept variants, X-Debug) |
| `findings-<UTC>.json` | Per-run output of `probe_endpoints.py` |
| `methods-<UTC>.json`  | Per-run output of `probe_methods.py` |

Both scripts read the inverter address from CLI flags (`--host`, `--port`)
and default to the homelab values used during this run.

## Safety rules baked into the tools

The EZ1 has multiple field-verified fragility quirks (issue #14 +
[`docs/troubleshooting.md`](../../docs/troubleshooting.md)). The tools
enforce:

* **GET only** in Phase 1; only `GET`, `HEAD`, `OPTIONS` (all idempotent)
  in Phase 2. No `POST`/`PUT`/`DELETE`/`PATCH` anywhere.
* **`set*` endpoints are existence-probed only** — the URL is hit
  with no query string. Never with guessed parameters. Calling
  `/setNetwork?ssid=...` is the kind of mistake that locks the
  inverter onto the wrong WLAN; the methodology rules it out at
  the tool level, not just the operator level.
* **Rate limit ≥ 1 s** between probes (configurable up; the run
  below used 1.0 s for Phase 1 and 1.5 s for Phase 2).
* **Per-probe timeout 5 s** with a single retry on transport-level
  unreachability. Two consecutive timeouts mark the probe
  `inconclusive` (could be endpoint-absent OR a one-shot collision
  with the bridge polling the same inverter — they are
  indistinguishable from the probe side).
* **Watchdog**: 5 consecutive `unreachable` outcomes abort the run
  with a clear message. The inverter is most likely WLAN-dropped at
  that point, and continuing would only delay the inevitable
  power-cycle.

## What was probed

68 endpoints in Phase 1, in six classes:

| Class | Count | Notes |
|-------|------:|-------|
| Documented baseline (`/getDeviceInfo`, `/getOutputData`, `/getMaxPower`, `/getAlarm`, `/getOnOff`) | 5 | The seven endpoints minus the two `set*` ones, which sit in their own bucket below |
| Speculative read endpoints (`/getStatus`, `/getInverterDetail`, `/getNetworkInfo`, `/getEnergyHistory`, `/getMpptStatus`, …) | 27 | Patterns lifted from related solar-inverter vendors and ESP32-firmware conventions |
| Diagnostic / service patterns (`/status`, `/info`, `/health`, `/api/v1/*`, `/api/v2/*`, …) | 15 | Common conventions for embedded HTTP servers |
| Destructive patterns, existence-only (`/reboot`, `/restart`, `/reset`, `/shutdown`, `/factoryReset`, `/clearData`) | 6 | Existence is recorded; tools never call them with a body or query |
| Dashboard / UI patterns (`/`, `/index.html`, `/admin`, `/config`, `/settings`, …) | 7 | In case the inverter ships a hidden web UI |
| `set*` endpoints, existence-only (`/setMaxPower`, `/setOnOff`, `/setNetwork`, `/setWifi`, `/setMqtt`, `/setTime`, `/setNtp`, `/setCloud`) | 8 | Includes the two known ones as a methodology check |

Phase 2 ran 8 probes against known endpoints: OPTIONS on three paths,
HEAD on one, GET with `Accept: application/xml`, GET with
`Accept: text/plain`, and GET with `X-Debug: 1` on two endpoints.

## Findings

### F1 — The documented surface is the complete surface

Status `200 SUCCESS` came back from exactly seven endpoints:

```
/getDeviceInfo, /getOutputData, /getMaxPower, /getAlarm, /getOnOff,
/setMaxPower, /setOnOff
```

The other 61 endpoints in the wordlist returned `404` with the body
`"Nothing matches the given URI"`. Six probes timed out twice on the
first run; manual re-probing with 3 s spacing showed all six are
genuine 404s — the timeouts were collisions with the bridge polling
the same inverter.

**No diagnostic, debug, dashboard, or `/api/*` routes exist.**
The inverter does not expose its identity beyond what the seven
documented endpoints already publish.

### F2 — `200 OK` does NOT mean success on `set*` endpoints

The two known `set*` endpoints respond with **HTTP 200** even when
called without parameters — but the JSON body explicitly says
`"message": "FAILED"`:

```json
GET /setMaxPower
HTTP/1.1 200 OK
{"data":{"maxPower":"null"},"message":"FAILED","deviceId":"E17010000783"}

GET /setOnOff
HTTP/1.1 200 OK
{"data":{"status":"null"},"message":"FAILED","deviceId":"E17010000783"}
```

Verified post-probe: `getMaxPower` still reads `800` and
`getOnOff` still reads `"0"` (= on). The empty calls do **not**
mutate state — the firmware's validation layer rejects the missing
parameter and reports the rejection in the JSON envelope rather
than via an HTTP error code.

**Operational consequence for clients:** never trust the HTTP status
alone. Always check `body.message == "SUCCESS"` before treating a
write as effective. The bridge already does this in
[`src/ez1_bridge/domain/normalizer.py`](../../src/ez1_bridge/domain/normalizer.py)
via the `_SUCCESS` constant and the per-endpoint envelope check;
this finding formalises why that check is mandatory rather than
belt-and-braces.

### F3 — Only GET is supported; OPTIONS does not list methods

`OPTIONS /getDeviceInfo` and `HEAD /getDeviceInfo` both return
`HTTP 405 Method Not Allowed` with the body
`"Specified method is invalid for this resource"`. Notably, **no
`Allow:` header** is sent — the inverter does not advertise its
supported method set the way RFC 7231 expects. The information
"only GET works" is verifiable empirically but not via the HTTP
contract itself.

### F4 — No content negotiation; no `X-Debug` widening

`GET /getDeviceInfo` with `Accept: application/xml` and with
`Accept: text/plain` both return the same JSON envelope, with
the `Content-Type: application/json` response header unchanged.
The inverter ignores the `Accept` header — there is no XML or
plaintext variant lurking behind content negotiation.

`GET /getDeviceInfo` and `GET /getOutputData` with the
`X-Debug: 1` header (a long shot, but a common embedded-firmware
convention) return identical bodies to the unmodified GET. No
debug-mode widening is wired into the firmware's HTTP layer.

### F5 — Web stack is bare-bones; no `Server:` / `Date:` / `Connection:` headers

A standard `curl -i` against `/getDeviceInfo` produces:

```http
HTTP/1.1 200 OK
Content-Type: application/json
Content-Length: 199

{"data":{"deviceId":"E17010000783","devVer":"EZ1 1.12.2t",...
```

That is the complete header set. **No `Server:` header** identifying
a web framework, no `Date:`, no `Connection:` directive, no
`Cache-Control:`. The bodies on 404 (`"Nothing matches the given URI"`)
and 405 (`"Specified method is invalid for this resource"`) match
CherryPy's default error templates, but the missing standard headers
argue against an off-the-shelf Python framework. Most likely a
hand-rolled HTTP layer on top of an ESP32 lwIP stack, with the
error-message strings copied from a CherryPy-style reference.

### F6 — The single-connection limitation is observable in collision-rate

8.8 % of Phase 1 probes (6 / 68) timed out on the first attempt,
re-probed cleanly later with a 3 s pause. The Phase 2 run had
2 / 8 unreachable on similar grounds. This matches the observed
behaviour from issue #14 — the inverter accepts only one TCP
connection at a time, and a probe arriving while the bridge held
its connection got SYN-dropped.

The probe tool's collision-aware retry handles this automatically;
operationally, anyone running another client against the same
inverter (HA add-on, custom scripts, etc.) needs to serialise their
calls or accept similar drop-rates.

## Phase 3 — not run, awaiting decision

The original brief proposes Phase 3 (path-traversal of the
`/api/v1/*` shape, parameter exploration on `/getEnergyDay` etc.)
**only after** Phase 1 surfaces undocumented endpoints. Phase 1
surfaced none. There is no `/api/`, no `/api/v1/`, no
`/getEnergyDay`. Phase 3 has nothing to traverse.

The remaining unexplored research directions are firmware-level,
not protocol-level:

* **BLE-protocol reverse-engineering of AP EasyPower.** The phone
  app talks BLE GATT to the inverter and exposes a richer surface
  (energy histories, network reconfiguration, time sync). Reaching
  it needs a BLE sniffer (HCI dump from a rooted Android phone or a
  hardware sniffer like nRF52840 dongle), not HTTP. Out of scope
  for this Mac-driven HTTP probe.
* **Firmware extraction.** Open the inverter, identify the ESP32
  module, use `esptool.py` to dump the flash, and disassemble.
  Hardware-invasive and almost certainly voids any warranty.
* **Cloud-protocol reverse-engineering.** The inverter talks to
  AP's cloud over MQTT/HTTPS for the cloud dashboard; capturing
  that traffic might reveal further endpoints in the cloud-only
  protocol. Not local-API and therefore not interesting for this
  bridge's positioning.

If any of those become interesting, file an issue and discuss
methodology before starting.

## Recommended next steps

* **Land these tools as a research artefact.** Other operators with
  access to an EZ1 can re-run the wordlist (and possibly extend it
  via an `--extra-wordlist` flag) without rediscovering the safety
  rules.
* **Cross-link from `docs/troubleshooting.md` and `docs/api-reference.md`.**
  Findings F2, F3, F4, F5, F6 each formalise something the bridge
  already implements correctly (or would otherwise have to assume);
  the cross-link gives a future contributor "why does the normalizer
  check `message == SUCCESS`?" a one-click answer.
* **No bridge code change needed.** None of the findings reveal a
  contract the bridge is currently violating. The seven-endpoint
  surface is exactly what the documented adapter assumes.

## Reproducing this run

```bash
# Phase 1 — wordlist (~68 probes, ~80 s with default rate limit)
uv run python tools/api-discovery/probe_endpoints.py \
    --host 192.168.3.24 --port 8050 --rate-limit-seconds 1.0

# Phase 2 — methods/headers (~8 probes, ~12 s)
uv run python tools/api-discovery/probe_methods.py \
    --host 192.168.3.24 --port 8050 --rate-limit-seconds 1.5
```

Both produce timestamped JSON outputs in this directory. The
findings JSON is the primary artefact; this README is the human
synthesis. Re-runs against different firmware (or a different
unit) should preserve or refute each finding above and append
to this document with a dated section.
