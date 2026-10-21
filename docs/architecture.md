# SensorTrain architecture

SensorTrain separates physical I/O from application hosting so that real-time-ish sensor and fan handling remains on the Pico 2 (non-W) while Linux/MicroShift handles transport, presentation, and platform health.

## Data path

```text
BNO085 ─┐
BME280 ─┼─> Raspberry Pi Pico 2 (non-W) ── USB serial / NDJSON ──> host bridge
DS18B20 ┤                                                   |
QTR-HD-01RC ┘                                                   | HTTP
Fans <────────────── control ACK / commands <───────────────┘
                                                            |
                                                            v
                                                   sensortrain-web
                                                    on MicroShift
                                                            |
                                                            v
                                                    browser dashboard
```

## Pico 2 (non-W)

The Pico runs CircuitPython and owns the physical interfaces:

- BNO085 and BME280 on shared I2C
- two DS18B20 probes on one 1-Wire bus
- QTR-HD-01RC wheel marker sensor
- two MOSFET-driven fans

Telemetry is emitted as newline-delimited JSON using schema `sensortrain.telemetry.v1`. Fan control commands are accepted over the same serial connection and acknowledged with `sensortrain.control.ack.v1`.

Fan output is constrained to 0, 25, 50, 75, or 100 percent. Starting a stopped fan at 25 percent applies a 100 percent startup kick for 0.5 seconds before dropping to 25 percent.

## Host bridge

`bridge/sensortrain-bridge.py` runs as a hardened systemd service on the Raspberry Pi host. It:

- opens the stable `/dev/sensortrain-pico` udev symlink
- forwards Pico telemetry to the web service over HTTP
- polls desired fan state from the web service
- sends fan commands to the Pico
- waits for Pico acknowledgements and retries unacknowledged commands
- sends an independent bridge heartbeat every two seconds
- reconnects automatically after Pico disconnect/reconnect

The bridge heartbeat intentionally continues even when the Pico is disconnected so the dashboard can distinguish bridge health from Pico health.

## MicroShift web application

`web/server.py` hosts the REST API and serves the dashboard. The v1.0 application stores current telemetry, desired fan state, and rolling history in memory.

Important endpoints:

- `GET /api/telemetry`
- `GET /api/control`
- `POST /api/control`
- `GET /api/history?minutes=1|5|10|20|30`
- `GET /api/system`
- `POST /api/bridge-heartbeat`
- `GET /healthz`

## Health semantics

The three dashboard health indicators are independent:

- **PICO** — latest telemetry is no more than eight seconds old.
- **BRIDGE** — latest bridge heartbeat is no more than six seconds old.
- **CLUSTER** — the application can successfully query the Kubernetes API `/readyz` endpoint.

This distinction allows the dashboard to show, for example, a healthy bridge and cluster while the Pico is physically unplugged.

## History model

History is intentionally non-persistent in v1.0. A bounded in-memory deque retains approximately 45–50 minutes at the current telemetry cadence. The dashboard offers 1, 5, 10, 20, and 30 minute views.

Pod replacement therefore clears graph history and desired fan state. This behavior was explicitly accepted for the v1.0 release.

## Current motor-control architecture

A separate ESP32-C3 connects over USB to the Pi motor bridge.
It controls the designated LEGO Powered Up hub over Bluetooth LE.

The ESP32-C3 implements a one-second heartbeat failsafe,
automatic BLE reconnection, and restart at zero motor speed.

Additional host services collect Pi telemetry and process
authorized host-shutdown requests.

The running application includes main, tablet, and admin pages.
A second motor hub for a powered caboose is planned, not deployed.
