# SensorTrain v1.1.0

Release prepared: October 20, 2026

## Overview

Updated source baseline of the working SensorTrain
LEGO sensor and computing platform.

## Hardware

- Raspberry Pi 5
- Raspberry Pi Pico 2 (non-W)
- ESP32-C3 BLE motor controller
- BNO085 IMU
- BME280 environmental sensor
- Two DS18B20 temperature probes
- QTR-HD-01RC wheel marker sensor
- Intake and exhaust cooling fans

The Pico 2 replaces the original Pico 2 W.
The QTR sensor replaces the original Hall sensor.

## Platform

- Red Hat Enterprise Linux 9.8 aarch64
- MicroShift 4.22.7
- Red Hat Edge Manager / Flight Control 1.3.1
- Standalone OpenWRT network

## Software

- Updated CircuitPython sensor firmware
- ESP32-C3 LEGO Powered Up motor firmware
- Pi sensor and motor bridges
- Host telemetry and shutdown-agent services
- Updated MicroShift dashboard
- Tablet and Admin control interfaces
- Travel instrumentation and animated fans
- CPU temperature-dependent gauge coloring

## Safety

The motor controller implements an independent
one-second heartbeat failsafe.

BLE reconnection does not restore motor speed.

## Verification

- Source capture integrity verified
- Python syntax validation passed
- Git formatting check passed
- Initial staged-file secret scan passed
- ESP-IDF 6.1 clean configuration passed
- ESP32-C3 clean compilation passed

## Future development

A second motor and LEGO Powered Up hub in a powered
caboose are planned but not included in this release.

## Version history

The September 2026 v1.0.0 tag remains unchanged.
