# SensorTrain

SensorTrain is a Raspberry Pi 5 / Raspberry Pi Pico 2 (non-W) sensor and compute platform designed to operate inside a LEGO train.

The Raspberry Pi 5 runs Red Hat Enterprise Linux 9.8 with the Red Hat build of MicroShift 4.22 on a custom Raspberry Pi 6.18 kernel. A Pico 2 (non-W) handles physical sensor acquisition and fan control, with telemetry bridged into a MicroShift-hosted web dashboard.

## Hardware

### Compute
- Raspberry Pi 5 - 8GB (https://www.microcenter.com/product/673711/raspberry-pi-5-8gb)
- 52Pi M.2 NVMe / PoE+ Hat (https://www.microcenter.com/product/688932/52pi-m2-nvme-m-key-poe-hat-for-raspberry-pi-5)
- Official Raspberry Pi 5 Active Cooler (https://www.microcenter.com/product/671930/raspberry-pi-5-active-cooler)
- 128 GB 2242 NVMe SSD (https://a.co/d/0gFHPqWI)
- Raspberry Pi Pico 2 (https://www.microcenter.com/product/683268/raspberry-pi-pico-2)

### Sensors
- Adafruit BME280 environmental sensor (https://www.adafruit.com/product/2652)
    - Temperature, Humidity, Barometric Pressure
- Adafruit BNO085 Orientation IMU (https://www.adafruit.com/product/4754)
    - Acceleration and Acceleration Vector
    - Angular Velocity and Gyroscope
    - 3 Axis Magentometer
    - Linear Acceleration Vector
    - Gravity Vector
    - Rotation Vector
- 2 x Adafruit DS18B20 temperature sensors (https://www.adafruit.com/product/374)
    - 12 bit Precision Temperature
- Adafruit VEML7700 Lux Sensor (https://www.adafruit.com/product/4162)
    - 16 bit Precision Lux
- Adafruit ENS161 MOX Gas Sensor (https://www.adafruit.com/product/6431)
    - Air Quality Index
    - eCO2
    - TVOC
- Pololu QTR-HD-01RC Reflectance Sensor (https://www.pololu.com/product/4101)
    - IR LED Phototransistor

### Cooling
- 2 x 30 mm fans
- 2x Adafruit MOSFET fan drivers (https://www.adafruit.com/product/5648)
- PWM levels: 0%, 25%, 50%, 75%, 100%
- 0.5-second full-power startup kick when starting at 25%

## Software architecture

```text
Pico 2 (non-W)
    |
    | USB serial / NDJSON
    v
sensortrain-bridge
    |
    | HTTP
    v
MicroShift / sensortrain-web
    |
    v
Browser dashboard
```

The dashboard provides:

- live environmental and temperature gauges
- acceleration, gyroscope, and orientation telemetry
- wheel marker and travel information
- independent intake/exhaust fan control
- simultaneous rolling history graphs
- 1, 5, 10, 20, and 30 minute history windows
- independent PICO, BRIDGE, and CLUSTER health indicators

History is intentionally stored in RAM and resets when the web application restarts.

## Documentation

- [Architecture](docs/architecture.md)
- [Hardware](docs/hardware.md)
- [Installation](docs/installation.md)
- [v1.0 acceptance testing](docs/acceptance-testing.md)
- [MicroShift deployment](deploy/microshift/README.md)
- [Raspberry Pi / RHEL / MicroShift platform notes](platform/raspberry-pi-rhel-microshift.md)

## Repository layout

- `pico/` - Pico 2 CircuitPython firmware
- `bridge/` - Raspberry Pi host serial/HTTP bridge and host integration
- `web/` - MicroShift dashboard application
- `deploy/` - deployment manifests and deployment notes
- `docs/` - architecture, hardware, installation, and acceptance documentation
- `platform/` - Raspberry Pi / RHEL / MicroShift platform notes
- `releases/` - release records

## Historical release

SensorTrain v1.0

- Pico firmware: 0.3.0
- CircuitPython: 10.2.1
- RHEL: 9.8
- MicroShift: 4.22
- Kubernetes: 1.35

The `v1.0.0` tag is the frozen accepted baseline. Development and documentation continue on `main`.

## Support note

The RHEL userspace and MicroShift packages used by this project are genuine Red Hat software. Raspberry Pi 5 is not a supported Red Hat hardware target for this configuration, and the project relies on a custom Raspberry Pi kernel and boot arrangement.

## Current GA development

The current hardware uses the Pico 2 (non-W), which replaced the
original Pico 2 W. It communicates with the Pi 5 over USB.

The QTR-HD-01RC wheel marker sensor replaced the Hall sensor.

The ESP32-C3 controls the locomotive's LEGO Powered Up hub over BLE.
Its firmware is maintained in `esp32-c3/`, while the motor bridge
and host services are maintained in `bridge/`.

The application also provides `/tablet` and `/admin` interfaces.
Red Hat Edge Manager manages the Pi through Flight Control 1.3.1.

The powered caboose and second motor hub are future work.
The original v1.0.0 Git tag remains unchanged.
