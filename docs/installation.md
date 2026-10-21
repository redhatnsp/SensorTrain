# SensorTrain installation

This document describes the v1.0 application and bridge installation on an already-working SensorTrain Raspberry Pi 5 platform. Platform/kernel construction is documented separately in `platform/raspberry-pi-rhel-microshift.md`.

## Prerequisites

- Raspberry Pi 5 running the validated RHEL/MicroShift platform
- MicroShift running and `oc` configured
- Raspberry Pi Pico 2 (non-W) running CircuitPython 10.2.1
- repository checked out on the Pi

## 1. Install Pico firmware

Mount the `CIRCUITPY` filesystem and copy the production firmware:

```bash
sudo mkdir -p /mnt/circuitpy
sudo mount /dev/disk/by-label/CIRCUITPY /mnt/circuitpy
sudo cp pico/code.py /mnt/circuitpy/code.py
sync
sudo umount /mnt/circuitpy
```

The Pico firmware depends on CircuitPython libraries for the BME280, BNO08X, DS18B20/OneWire, and bus-device support. Install matching CircuitPython 10.x library-bundle versions on the Pico rather than copying arbitrary current libraries.

## 2. Install the stable Pico udev rule

```bash
sudo cp bridge/99-sensortrain-pico.rules \
  /etc/udev/rules.d/99-sensortrain-pico.rules

sudo udevadm control --reload-rules
sudo udevadm trigger
```

Verify:

```bash
ls -l /dev/sensortrain-pico
```

The bridge account must be a member of `dialout`.

## 3. Install the host bridge

```bash
sudo install -m 0755 bridge/sensortrain-bridge.py \
  /usr/local/libexec/sensortrain-bridge.py

sudo cp bridge/sensortrain-bridge.service \
  /etc/systemd/system/sensortrain-bridge.service
```

Create `/etc/sysconfig/sensortrain-bridge` from `bridge/sensortrain-bridge.env.example` after the MicroShift Service is deployed and its ClusterIP is known.

Reload systemd:

```bash
sudo systemctl daemon-reload
```

## 4. Deploy the MicroShift application

Follow `deploy/microshift/README.md` to create the namespace, ConfigMap, Deployment, Service, and Route.

Retrieve the Service ClusterIP:

```bash
oc get svc sensortrain-web -n sensortrain \
  -o jsonpath='{.spec.clusterIP}{"\n"}'
```

Create `/etc/sysconfig/sensortrain-bridge`:

```text
PICO_DEVICE=/dev/sensortrain-pico
TELEMETRY_URL=http://SERVICE_CLUSTER_IP:8080/api/telemetry
CONTROL_URL=http://SERVICE_CLUSTER_IP:8080/api/control
```

Then enable the bridge:

```bash
sudo systemctl enable --now sensortrain-bridge
```

## 5. Verify data flow

Check the bridge:

```bash
systemctl status sensortrain-bridge --no-pager
journalctl -u sensortrain-bridge -n 50 --no-pager
```

Expected log events include a Pico connection, telemetry forwarding, and bridge heartbeat establishment.

Check the application:

```bash
oc get pods -n sensortrain
oc get route sensortrain -n sensortrain
```

The original September deployment used the `sensortrain.test` Route. The current train is accessed locally at `http://sensortrain.local`. Verify the active network and route configuration before reusing the historical manifest.

## 6. Validate controls

Open the dashboard and verify live telemetry. Exercise intake and exhaust fan controls at 0, 25, 50, 75, and 100 percent. Confirm that a fan starting from rest at 25 percent receives the brief startup kick and then settles at the commanded level.

## Updating the application

After editing `web/server.py`, refresh the ConfigMap and restart the Deployment:

```bash
oc create configmap sensortrain-app \
  -n sensortrain \
  --from-file=server.py=web/server.py \
  --dry-run=client -o yaml | oc apply -f -

oc rollout restart deployment/sensortrain-web -n sensortrain
oc rollout status deployment/sensortrain-web -n sensortrain
```

The dashboard HTML is embedded/served by the current v1.0 server implementation. `web/dashboard.html` is retained as the standalone dashboard source/reference used during development.

## Expected restart behavior

The v1.0 server keeps telemetry history and desired fan state only in memory. Restarting/replacing the web pod clears both. This is intentional and not a recovery failure.

## Current GA additions

The current platform also runs the ESP32-C3 motor controller,
the motor bridge, host telemetry, and shutdown-agent services.

Install these from the reviewed files under `esp32-c3/` and
`bridge/`. Their runtime configuration must be checked against
the target hardware before enabling them.

The current Pico 2 uses USB communications, not Pico Wi-Fi.
Red Hat Flight Control enrollment credentials are configured
outside this public repository.
