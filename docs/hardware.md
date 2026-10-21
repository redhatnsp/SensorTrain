# SensorTrain hardware

## Compute car

- Raspberry Pi 5
- official Raspberry Pi 5 Active Cooler
- 128 GB 2242 NVMe SSD on M.2 HAT
- Raspberry Pi Pico 2 (non-W)
- two approximately 30 x 30 x 7 mm Easycargo fans
- MOSFET fan drivers
- USB UVC camera retained for the project

The M.2 HAT stack uses GPIO pin extension and the mechanical gap is useful for airflow. The production mounting concept uses longer M2.5 hardware through the Pi/HAT stack into a reinforced removable train side wall with a printed spacer/backing plate.

The Active Cooler exhaust is aimed toward the rear of the compute car. Fans are arranged to establish through-flow across the compute hardware.

## Sensors

### BNO085

Used for motion sensing:

- acceleration
- angular velocity
- rotation-vector quaternion

### BME280

Used for ambient environmental measurements:

- temperature
- relative humidity
- barometric pressure

### DS18B20

Two temperature probes share one 1-Wire bus.

Production ROM assignments:

- `28:CA:D0:23:0D:00:00:F8` — EXHAUST
- `28:44:9D:23:0D:00:00:04` — COMPUTE

### QTR-HD-01RC

Pololu reflective wheel marker sensor, connected to GP3. It replaces the earlier Hall sensor.

## Pico pin assignments

Current production firmware uses:

- GP0 — I2C SDA
- GP1 — I2C SCL
- GP2 — 1-Wire bus
- GP3 — QTR wheel marker sensor
- GP6 — intake fan PWM
- GP7 — exhaust fan PWM

The fan PWM frequency is 100 Hz. Supported command levels are 0, 25, 50, 75, and 100 percent. A stopped fan commanded directly to 25 percent receives a 100 percent startup kick for 0.5 seconds before settling at 25 percent.

## Power

The compute car is powered from a USB-C power bank carried in the third car. The LEGO Powered Up motor system is electrically separate.

## Connectors and fabrication

The project uses locking JST-XH connectors for removable sensor/fan connections. The validated Rev C carrier uses a socketed Pico 2 (non-W), JST-XH headers, and external MOSFET fan-driver boards.

The enclosure/mechanical work is intended for a Bambu Lab P1S. M2.5 heat-set inserts and M2.5/M3 hardware are available for printed structural parts.

## Planned external status LEDs

The compute car design reserves the following Raspberry Pi GPIOs for visible indicators:

- PWR — hardware LED directly from the 3.3 V rail; no GPIO
- SSD — GPIO27, software-driven NVMe activity
- STATUS RGB — GPIO17, GPIO22, GPIO23

Approximately 1 kΩ series resistance per LED channel is the conservative starting point. GPIO0/1 are reserved for HAT ID, GPIO14/15 for serial, GPIO2/3 for I2C, and GPIO7–11 for SPI.
