# SensorTrain v1.5 source snapshot

Captured: 2026-11-06. Candidate source snapshot for branch `sensortrain-v1.5`.

## Components

- ESP32-C3: dual LEGO Powered Up hubs, moving headlights, and `ADMINRECOVER RESET` serial command. Firmware was built/flashed with ESP-IDF 6.1.
- Raspberry Pi 5: RHEL 9.8, MicroShift 4.22.7; live `sensortrain-app` `server.py` (Dashboard, Control Panel, Admin), host telemetry and motor bridges, and operational service/rule files.
- Pico 2: CircuitPython telemetry with ENS161 and VEML7700 environmental sensors, plus BNO085, BME280, DS18B20, wheel measurement and fan control.
- Admin: HTTPS authentication, no web password rotation, Clear Fault recovery command dispatch and state reconciliation.

## Scope and verification

- This commit is **source**, not a firmware image, backup of the live Kubernetes cluster, or automated redeployment.
- No runtime credentials, passwords, Secret objects, TLS certificates/private keys, kubeconfigs, SSH keys, environment files, or stored telemetry are intentionally included.
- The latest observed system was healthy: motor source fresh, both BLE links ready, fault latch clear, outputs zero; the Admin page showed no latched fault.
- **A true latched-fault end-to-end Admin Clear Fault test remains outstanding.** Healthy IDLE state was verified.
- Pico provenance at capture: `CIRCUITPY-readonly-mount`. If marked `not-live-confirmed`, the source comes from a checksum-verified v0.5.0 installation stage rather than a direct read of the board.
- Service manifests/deployment configuration in the repository may require separate review before reproducible installation; do not apply blindly to a running train.

To propose merging into `main`, open a pull request from `sensortrain-v1.5` after reviewing the source diff and ensuring no private information was included.
