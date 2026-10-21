# SensorTrain ESP32-C3 Motor Controller

Firmware for controlling the SensorTrain locomotive's
LEGO Powered Up hub over Bluetooth Low Energy.

## Requirements

- ESP32-C3 development board
- ESP-IDF 6.1
- USB connection to the Raspberry Pi 5

## Building

Activate the ESP-IDF 6.1 environment, change into this
directory, and run:

    idf.py build

The sdkconfig.defaults file provides the required ESP32-C3,
NimBLE, and USB Serial/JTAG configuration.

A clean configuration and firmware build were verified
using ESP-IDF 6.1 on October 20, 2026.

## Runtime integration

The Raspberry Pi communicates with the ESP32-C3 over USB.

The host motor bridge uses /dev/sensortrain-motor.

The ESP32-C3 connects to the designated LEGO Powered Up
hub and supports speed commands, STOP, and automatic
BLE reconnection.

A one-second heartbeat failsafe stops the motor when
communication from the host is lost.

Reconnection never restores a previous motor speed.

## Future development

The current firmware controls one LEGO Powered Up hub.

Coordinated control of a second hub for the planned
powered caboose is future development.
