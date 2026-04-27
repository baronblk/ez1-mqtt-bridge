# APsystems EZ1 Local API — Reference

> Quelle: APsystems EZ1 Local API User Manual, V1.1 (2023-11-07).
> Aufbereitung als strukturierte Markdown-Referenz für `ez1-mqtt-bridge`.
> Ergänzt um in Echt-Tests (2026-04-26) verifizierte Payloads und beobachtete Edge-Cases.

---

## Aktivierung

Lokaler Modus muss in der **AP EasyPower App** über eine **Bluetooth-Direktverbindung** aktiviert werden:

1. Aus Cloud-Konto abmelden (App → „Mich" → „Abmelden").
2. Direktverbindung per BT (Gerätename `EZ1_<deviceId>`).
3. Settings → „Lokaler Modus" → Schalter aktivieren.

**Voraussetzungen:** Firmware ≥ 1.7.0 (verifiziert auf `1.12.2t`). HTTP-Server lauscht danach auf der WLAN-IP des Geräts auf TCP 8050. Bluetooth wird zur Laufzeit nicht benötigt.

**Sicherheit:** Keine Authentifizierung. Wer im LAN routbar zum Gerät ist, kann lesen UND schreiben (Max-Power, On/Off). Netzsegmentierung ist Pflicht.

---

## Common Response Envelope

Alle Endpoints liefern das gleiche Wrapping:

```json
{
  "data":     { /* endpoint-spezifisch */ },
  "message":  "SUCCESS" | "FAILED",
  "deviceId": "E17010000783"
}
```

| Feld       | Typ    | Bedeutung                                              |
|------------|--------|--------------------------------------------------------|
| `data`     | object | Endpoint-spezifischer Payload                          |
| `message`  | string | `SUCCESS` bei OK, sonst `FAILED`                       |
| `deviceId` | string | Geräte-ID, redundant zum Inhalt von `data` bei `getDeviceInfo` |

**Behandlung in der Bridge:** `message != "SUCCESS"` → Zyklus überspringen, Counter `ez1_api_errors_total{reason="non_success"}` inkrementieren, kein Crash.

---

## 1. `GET /getDeviceInfo`

Geräte-Stammdaten. Einmalig beim Start lesen, dann alle 24 h refreshen für HA-Discovery.

### Response-Schema

| Feld       | Typ    | Einheit | Beschreibung                                       |
|------------|--------|---------|----------------------------------------------------|
| `deviceId` | string | —       | Eindeutige Geräte-ID, beginnt mit `E…`             |
| `devVer`   | string | —       | Firmware-String, Format `EZ1 X.Y.Z[suffix]`        |
| `ssid`     | string | —       | Aktuell verbundene WLAN-SSID                       |
| `ipAddr`   | string | —       | Aktuelle IP des Geräts im LAN                      |
| `minPower` | string | W       | Untere Grenze für `setMaxPower` (typ. `30`)        |
| `maxPower` | string | W       | Obere Grenze für `setMaxPower` (typ. `800`)        |

### Verifizierter Echt-Payload

```json
{
  "data": {
    "deviceId": "E17010000783",
    "devVer":   "EZ1 1.12.2t",
    "ssid":     "If you read this, you suck!",
    "ipAddr":   "192.168.3.24",
    "minPower": "30",
    "maxPower": "800"
  },
  "message":  "SUCCESS",
  "deviceId": "E17010000783"
}
```

### Edge-Cases

- `minPower`/`maxPower` kommen als **Strings**, nicht als Integers → Pydantic-Coerce nötig.
- Das `t`-Suffix in `devVer` markiert Test-/Trial-Firmware.

---

## 2. `GET /getOutputData`

Live-Leistungs- und Energiewerte. Hauptendpoint für Polling.

### Response-Schema

| Feld  | Typ   | Einheit | Beschreibung                                      |
|-------|-------|---------|---------------------------------------------------|
| `p1`  | float | W       | Aktuelle Leistung Kanal 1 (MPPT 1)                |
| `p2`  | float | W       | Aktuelle Leistung Kanal 2 (MPPT 2)                |
| `e1`  | float | kWh     | Energieerzeugung seit Geräte-Startup, Kanal 1     |
| `e2`  | float | kWh     | Energieerzeugung seit Geräte-Startup, Kanal 2     |
| `te1` | float | kWh     | Lifetime-Energieerzeugung Kanal 1 (kumulativ)     |
| `te2` | float | kWh     | Lifetime-Energieerzeugung Kanal 2 (kumulativ)     |

### Verifizierter Echt-Payload

```json
{
  "data": {
    "p1": 139,
    "e1": 0.28731,
    "te1": 87.43068,
    "p2": 65,
    "e2": 0.42653,
    "te2": 111.24305
  },
  "message":  "SUCCESS",
  "deviceId": "E17010000783"
}
```

### Edge-Cases & Mapping-Hinweise

- **`e*` ist *seit Geräte-Startup*, nicht *seit Mitternacht*.** Ein Reset kann mitten am Tag passieren, wenn der WR durch Wolken/Nacht offline ging. Für HA `state_class: total_increasing` ist das aber tolerabel — HA erkennt Resets.
- **`te*` ist monoton steigend** über die gesamte Gerätelebenszeit. Ideal für `state_class: total_increasing`.
- **Werte können 0 sein** (Nacht/Verschattung), aber niemals negativ.
- Die zwei MPPT-Kanäle sind unabhängig — eine Asymmetrie zwischen `p1` und `p2` ist normal und diagnostisch wertvoll (Modul-Defekt, Verschattung).
- Aggregation `total = ch1 + ch2` macht die Bridge selbst, der WR liefert sie nicht.

---

## 3. `GET /getMaxPower`

Aktuelles Drosselungs-Limit auslesen.

### Response-Schema

| Feld       | Typ    | Einheit | Beschreibung                              |
|------------|--------|---------|-------------------------------------------|
| `maxPower` | string | W       | Aktuell konfiguriertes Output-Limit       |

### Verifizierter Echt-Payload

```json
{
  "data": { "maxPower": "800" },
  "message": "SUCCESS",
  "deviceId": "E17010000783"
}
```

---

## 4. `GET /setMaxPower?p={value}`

Drosselungs-Limit setzen.

### Query-Parameter

| Param | Required | Typ    | Bereich                              | Beschreibung           |
|-------|----------|--------|--------------------------------------|------------------------|
| `p`   | ✓        | string | `minPower …  maxPower` aus DeviceInfo | Neues Limit in Watt   |

### Response

Echo des gesetzten Werts:

```json
{
  "data": { "maxPower": "600" },
  "message": "SUCCESS",
  "deviceId": "E17010000783"
}
```

### Validierungsregeln (Bridge-seitig zu enforcen)

- Vor Aufruf gegen `minPower`/`maxPower` aus DeviceInfo prüfen.
- Werte außerhalb des Bereichs vor dem HTTP-Call ablehnen → Result-Topic mit `ok: false, error: "out_of_range"`.
- **Wichtig:** Methode ist `GET`, nicht `POST`. Nicht idempotent im REST-Sinne, aber so spezifiziert.

### Use-Cases

- Dynamisches Drosseln auf 600 W bei rechtlicher 600-W-Grenze.
- Tageszeitabhängige Drosselung über HA-Automation.

---

## 5. `GET /getAlarm`

Diagnose-Flags. Vier voneinander unabhängige Bits.

### Response-Schema

| Feld    | Typ    | Werte | Bedeutung                                      |
|---------|--------|-------|------------------------------------------------|
| `og`    | string | `0`/`1` | **Off Grid** — AC-Verbindung fehlt/instabil  |
| `oe`    | string | `0`/`1` | **Output Fault** — AC-seitiger Fehler        |
| `isce1` | string | `0`/`1` | **DC1 Short Circuit** — Kanal 1 Kurzschluss  |
| `isce2` | string | `0`/`1` | **DC2 Short Circuit** — Kanal 2 Kurzschluss  |

`0` = normal, `1` = Alarm aktiv.

### Verifizierter Echt-Payload

```json
{
  "data": {
    "og":    "0",
    "isce1": "0",
    "isce2": "0",
    "oe":    "0"
  },
  "message":  "SUCCESS",
  "deviceId": "E17010000783"
}
```

### Mapping zu HA-`binary_sensor` mit `device_class: problem`

| API-Key | HA-Topic-Key         | `device_class` |
|---------|----------------------|----------------|
| `og`    | `alarm_off_grid`     | `problem`      |
| `oe`    | `alarm_output_fault` | `problem`      |
| `isce1` | `alarm_dc1_short`    | `problem`      |
| `isce2` | `alarm_dc2_short`    | `problem`      |

### Diagnose-Hinweise (aus offizieller Doku)

- **Off grid** → AC-Verbindung des Wechselrichters prüfen.
- **Output fault** → AC-Verbindung prüfen.
- **isce1/isce2** → DC-Verbindung des betroffenen Kanals prüfen, ggf. Verlängerungsleitung tauschen oder Modul auf den anderen Kanal umstecken zur Fehlereinkreisung.

---

## 6. `GET /getOnOff`

On/Off-Status auslesen.

### Response-Schema

| Feld     | Typ    | Werte | Bedeutung   |
|----------|--------|-------|-------------|
| `status` | string | `0`   | **On**      |
|          |        | `1`   | **Off**     |

> ⚠️ **Achtung — Semantik invers zur Intuition:** `0` = an, `1` = aus.

### Verifizierter Echt-Payload

```json
{
  "data": { "status": "0" },
  "message": "SUCCESS",
  "deviceId": "E17010000783"
}
```

---

## 7. `GET /setOnOff?status={value}`

WR ein-/ausschalten (stoppt Output, lokale API bleibt erreichbar).

### Query-Parameter

| Param    | Required | Werte | Bedeutung              |
|----------|----------|-------|------------------------|
| `status` | ✓        | `0`   | Einschalten            |
|          |          | `1`   | Ausschalten (no Output)|

### Response

Echo des gesetzten Werts (selbes Schema wie `getOnOff`).

### Wichtige Eigenschaft

> **Im Off-Zustand bleibt die lokale API verfügbar.** Bridge kann den WR also remote ausschalten und ihn anschließend per API wieder einschalten — keine Offline-Falle.

### Bridge-seitiges Mapping

Anwender soll auf MQTT `"on"`/`"off"` schreiben können (lesbar), Bridge mappt intern auf `0`/`1`.

---

## Polling-Empfehlungen

| Aspekt                     | Empfehlung           | Begründung                                       |
|----------------------------|----------------------|--------------------------------------------------|
| Polling-Intervall          | 20 s                 | WR aktualisiert intern ~5 s, schneller bringt nichts |
| Endpoints pro Zyklus       | 4 parallel           | `getOutputData`, `getAlarm`, `getOnOff`, `getMaxPower` |
| `getDeviceInfo`            | 1× Start + alle 24 h | Stammdaten, ändern sich selten                   |
| Request-Timeout            | 5 s                  | WR-WLAN kann hohe Latenz haben (45–90 ms RTT typisch) |
| Backoff bei Fehlern        | Exp 2× ab 1 s, Cap 300 s | Vermeidet Hammern bei Nacht-Offline           |

---

## Nicht-dokumentiertes Verhalten (empirisch, 2026-04-26)

| Beobachtung | Konsequenz für die Bridge |
|-------------|---------------------------|
| Bei Dunkelheit / fehlender PV-Spannung schaltet der WR komplett aus → HTTP-Connection-Refused, kein TCP-RST | Connection-Errors als `info` loggen, `availability=offline` setzen, weiter pollen |
| WLAN-Latenz schwankt 45–90 ms (selbst im LAN) | Timeout nicht zu knapp wählen, Histogramm in Prom-Metrics |
| `e1`/`e2` resetten beim WR-Cold-Start (nicht bei Mitternacht) | HA-`total_increasing` toleriert das, kein Custom-Handling nötig |
| Schreibvorgänge wirken sofort, aber `getMaxPower` braucht 1–2 s, bis er den neuen Wert reflektiert | Nach Schreibzugriff: 2 s warten, dann Read-back zur Verifikation |
| Cloud-Anbindung läuft parallel zum lokalen Modus weiter | Wer Cloud-Telemetrie unterbinden will, muss Outbound-Traffic per Firewall blocken |

---

## Quick-Reference Cheat Sheet

```bash
HOST=192.168.3.24
BASE="http://${HOST}:8050"

# Read
curl -sS "${BASE}/getDeviceInfo"  | jq
curl -sS "${BASE}/getOutputData"  | jq
curl -sS "${BASE}/getMaxPower"    | jq
curl -sS "${BASE}/getAlarm"       | jq
curl -sS "${BASE}/getOnOff"       | jq

# Write (Achtung: ändert Gerätekonfig!)
curl -sS "${BASE}/setMaxPower?p=600"
curl -sS "${BASE}/setOnOff?status=1"   # Aus
curl -sS "${BASE}/setOnOff?status=0"   # An
```
