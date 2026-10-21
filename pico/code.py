import time
import json
import sys
import math
import supervisor
import board
import pwmio
import busio
import digitalio

from adafruit_bno08x.i2c import BNO08X_I2C
from adafruit_bno08x import (
    BNO_REPORT_ACCELEROMETER,
    BNO_REPORT_GYROSCOPE,
    BNO_REPORT_ROTATION_VECTOR,
)
from adafruit_bme280 import basic as adafruit_bme280
from adafruit_onewire.bus import OneWireBus
import adafruit_ds18x20


# ==================================================
# SensorTrain Pico 2 telemetry firmware
# ==================================================

FIRMWARE_VERSION = "0.4.1"


# Known DS18B20 ROM identities
COMPUTE_ROM = "28:44:9D:23:0D:00:00:04"
EXHAUST_ROM = "28:CA:D0:23:0D:00:00:F8"


# Wheel geometry
WHEEL_DIAMETER_M = 0.023
WHEEL_CIRCUMFERENCE_M = math.pi * WHEEL_DIAMETER_M


boot_errors = []


def rom_string(device):
    return ":".join("{:02X}".format(byte) for byte in device.rom)


def rounded(value, digits=3):
    if value is None:
        return None
    return round(value, digits)


# --------------------------------------------------
# I2C
# GP0 = SDA
# GP1 = SCL
# --------------------------------------------------

i2c = busio.I2C(board.GP1, board.GP0)


bno = None
try:
    bno = BNO08X_I2C(i2c)
    bno.enable_feature(BNO_REPORT_ACCELEROMETER)
    bno.enable_feature(BNO_REPORT_GYROSCOPE)
    bno.enable_feature(BNO_REPORT_ROTATION_VECTOR)
except Exception as exc:
    boot_errors.append("bno085_init:" + str(exc))


bme = None
try:
    bme = adafruit_bme280.Adafruit_BME280_I2C(
        i2c,
        address=0x77
    )
except Exception as exc:
    boot_errors.append("bme280_init:" + str(exc))


# --------------------------------------------------
# DS18B20
# GP2
#
# Temperature conversions are started asynchronously so
# the wheel sensor can continue being serviced while the
# DS18B20s are converting.
# --------------------------------------------------

ow_bus = OneWireBus(board.GP2)

ds_sensors = {
    "compute": None,
    "exhaust": None,
}

try:
    ds_devices = ow_bus.scan()

    for device in ds_devices:
        rom = rom_string(device)

        if rom == COMPUTE_ROM:
            ds_sensors["compute"] = adafruit_ds18x20.DS18X20(
                ow_bus, device
            )

        elif rom == EXHAUST_ROM:
            ds_sensors["exhaust"] = adafruit_ds18x20.DS18X20(
                ow_bus, device
            )

    if ds_sensors["compute"] is None:
        boot_errors.append("ds18b20_compute_not_found")

    if ds_sensors["exhaust"] is None:
        boot_errors.append("ds18b20_exhaust_not_found")

except Exception as exc:
    boot_errors.append("ds18b20_init:" + str(exc))


DS_UPDATE_SECONDS = 2.0

compute_c = None
exhaust_c = None

ds_conversion_active = False
ds_conversion_ready_at = 0.0
ds_next_start_at = 0.0
ds_last_error = None


def start_ds_conversion(now):
    global ds_conversion_active
    global ds_conversion_ready_at
    global ds_next_start_at
    global ds_last_error

    max_delay = 0.0
    started = False

    try:
        for sensor in ds_sensors.values():
            if sensor is None:
                continue

            delay = sensor.start_temperature_read()
            if delay > max_delay:
                max_delay = delay
            started = True

        if started:
            ds_conversion_active = True
            ds_conversion_ready_at = now + max_delay
        else:
            ds_next_start_at = now + DS_UPDATE_SECONDS

        ds_last_error = None

    except Exception as exc:
        ds_conversion_active = False
        ds_next_start_at = now + DS_UPDATE_SECONDS
        ds_last_error = "ds18b20_convert:" + str(exc)


def service_ds_temperatures(now):
    global compute_c
    global exhaust_c
    global ds_conversion_active
    global ds_next_start_at
    global ds_last_error

    if not ds_conversion_active:
        if now >= ds_next_start_at:
            start_ds_conversion(now)
        return

    if now < ds_conversion_ready_at:
        return

    errors = []

    if ds_sensors["compute"] is not None:
        try:
            compute_c = rounded(
                ds_sensors["compute"].read_temperature(), 2
            )
        except Exception as exc:
            errors.append("compute:" + str(exc))

    if ds_sensors["exhaust"] is not None:
        try:
            exhaust_c = rounded(
                ds_sensors["exhaust"].read_temperature(), 2
            )
        except Exception as exc:
            errors.append("exhaust:" + str(exc))

    ds_conversion_active = False
    ds_next_start_at = now + DS_UPDATE_SECONDS

    if errors:
        ds_last_error = "ds18b20_read:" + ",".join(errors)
    else:
        ds_last_error = None


# --------------------------------------------------
# QTR-HD-01RC wheel reflectance sensor
# GP3 = OUT
# CTRL is intentionally left unconnected on Rev C.
#
# Measured on this installation:
# reflective aluminum marker: ~61-150 us
# black/no target: 5000 us timeout
#
# Hysteresis:
# marker detected below 500 us
# marker cleared at/above 1000 us
# --------------------------------------------------

ir = digitalio.DigitalInOut(board.GP3)

IR_CHARGE_SECONDS = 0.00002
IR_DETECT_US = 500
IR_CLEAR_US = 1000
IR_FULL_TIMEOUT_US = 5000
IR_MIN_EVENT_SECONDS = 0.030
WHEEL_STOP_TIMEOUT_SECONDS = 3.0

wheel_marker_active = False
wheel_count = 0
wheel_last_event_at = None
wheel_period_s = None
wheel_rpm = 0.0
wheel_speed_mps = 0.0
wheel_last_raw_us = IR_FULL_TIMEOUT_US


def read_ir_rc(timeout_us):
    ir.switch_to_output(value=True)
    time.sleep(IR_CHARGE_SECONDS)

    ir.switch_to_input()

    start_ns = time.monotonic_ns()

    while ir.value:
        elapsed_us = (time.monotonic_ns() - start_ns) // 1000

        if elapsed_us >= timeout_us:
            return timeout_us

    return (time.monotonic_ns() - start_ns) // 1000


def register_wheel_event(now):
    global wheel_count
    global wheel_last_event_at
    global wheel_period_s
    global wheel_rpm
    global wheel_speed_mps

    if wheel_last_event_at is not None:
        interval = now - wheel_last_event_at

        if interval < IR_MIN_EVENT_SECONDS:
            return

        wheel_period_s = interval
        wheel_rpm = 60.0 / interval
        wheel_speed_mps = WHEEL_CIRCUMFERENCE_M / interval

    wheel_last_event_at = now
    wheel_count += 1


def service_wheel_sensor():
    global wheel_marker_active

    if wheel_marker_active:
        value_us = read_ir_rc(IR_CLEAR_US)

        if value_us >= IR_CLEAR_US:
            wheel_marker_active = False

    else:
        value_us = read_ir_rc(IR_DETECT_US)

        if value_us < IR_DETECT_US:
            wheel_marker_active = True
            register_wheel_event(time.monotonic())


def service_wheel_stopped(now):
    global wheel_rpm
    global wheel_speed_mps
    global wheel_period_s

    if wheel_last_event_at is None:
        return

    if now - wheel_last_event_at >= WHEEL_STOP_TIMEOUT_SECONDS:
        wheel_rpm = 0.0
        wheel_speed_mps = 0.0
        wheel_period_s = None


# --------------------------------------------------
# Fans
# GP6 = intake
# GP7 = exhaust
#
# 100 Hz PWM characterized with discrete levels:
# 0 / 25 / 50 / 75 / 100 percent.
#
# Both fans require a startup kick when going
# directly from stopped to 25 percent.
# --------------------------------------------------

FAN_PWM_FREQUENCY = 100
FAN_STARTUP_KICK_SECONDS = 0.5
VALID_FAN_LEVELS = (0, 25, 50, 75, 100)


def fan_duty(percent):
    return round(65535 * percent / 100)


intake_fan = pwmio.PWMOut(
    board.GP6,
    frequency=FAN_PWM_FREQUENCY,
    duty_cycle=0
)

exhaust_fan = pwmio.PWMOut(
    board.GP7,
    frequency=FAN_PWM_FREQUENCY,
    duty_cycle=0
)

intake_fan_pct = 0
exhaust_fan_pct = 0


def fan_startup_kick(fan):
    fan.duty_cycle = fan_duty(100)

    end_at = time.monotonic() + FAN_STARTUP_KICK_SECONDS

    while time.monotonic() < end_at:
        service_wheel_sensor()
        service_wheel_stopped(time.monotonic())


def set_fan_level(
    fan,
    current_pct,
    requested_pct
):
    if type(requested_pct) is not int:
        raise ValueError(
            "fan percentage must be an integer"
        )

    if requested_pct not in VALID_FAN_LEVELS:
        raise ValueError(
            "fan percentage must be one of "
            "0, 25, 50, 75, 100"
        )

    # 25% sustains rotation but does not reliably
    # start either fan from a dead stop.
    if current_pct == 0 and requested_pct == 25:
        fan_startup_kick(fan)

    fan.duty_cycle = fan_duty(
        requested_pct
    )

    return requested_pct


# --------------------------------------------------
# USB serial command handling
#
# Commands are newline-delimited JSON, for example:
#
# {"command":"fans","intake":true,"exhaust":false}
# {"command":"fans","intake_pct":25,"exhaust_pct":50}
#
# --------------------------------------------------

command_buffer = ""


def apply_command(command):
    global intake_fan_pct
    global exhaust_fan_pct

    if command.get("command") != "fans":
        raise ValueError(
            "unsupported command"
        )

    # Preferred percentage-based control.

    if "intake_pct" in command:
        intake_fan_pct = set_fan_level(
            intake_fan,
            intake_fan_pct,
            command["intake_pct"]
        )

    # Legacy boolean compatibility for current bridge.

    elif "intake" in command:
        if type(command["intake"]) is not bool:
            raise ValueError(
                "intake must be boolean"
            )

        requested = (
            100 if command["intake"] else 0
        )

        intake_fan_pct = set_fan_level(
            intake_fan,
            intake_fan_pct,
            requested
        )

    if "exhaust_pct" in command:
        exhaust_fan_pct = set_fan_level(
            exhaust_fan,
            exhaust_fan_pct,
            command["exhaust_pct"]
        )

    elif "exhaust" in command:
        if type(command["exhaust"]) is not bool:
            raise ValueError(
                "exhaust must be boolean"
            )

        requested = (
            100 if command["exhaust"] else 0
        )

        exhaust_fan_pct = set_fan_level(
            exhaust_fan,
            exhaust_fan_pct,
            requested
        )


def process_serial_commands():
    global command_buffer

    try:
        while supervisor.runtime.serial_bytes_available:
            char = sys.stdin.read(1)

            if not char:
                return

            if char in ("\n", "\r"):
                if not command_buffer:
                    continue

                try:
                    command = json.loads(command_buffer)
                    apply_command(command)

                    print(json.dumps({
                        "schema": "sensortrain.control.ack.v1",
                        "status": "ok",
                        "command": command,
                        "fans": {
                            "intake": intake_fan_pct > 0,
                            "exhaust": exhaust_fan_pct > 0,
                            "intake_pct": intake_fan_pct,
                            "exhaust_pct": exhaust_fan_pct,
                        }
                    }))

                except Exception as exc:
                    print(json.dumps({
                        "schema": "sensortrain.control.ack.v1",
                        "status": "error",
                        "error": str(exc)
                    }))

                command_buffer = ""

            else:
                if len(command_buffer) < 512:
                    command_buffer += char
                else:
                    command_buffer = ""

    except Exception:
        pass


# --------------------------------------------------
# Telemetry
# --------------------------------------------------

seq = 0
TELEMETRY_SECONDS = 1.0
next_telemetry_at = time.monotonic()


def emit_telemetry():
    global seq
    global wheel_last_raw_us

    errors = list(boot_errors)

    if ds_last_error is not None:
        errors.append(ds_last_error)

    # ---------- Environment ----------

    ambient_c = None
    humidity_pct = None
    pressure_hpa = None

    if bme is not None:
        try:
            ambient_c = rounded(bme.temperature, 2)
            humidity_pct = rounded(bme.relative_humidity, 1)
            pressure_hpa = rounded(bme.pressure, 2)
        except Exception as exc:
            errors.append("bme280_read:" + str(exc))

    # ---------- Motion ----------

    accel_x = None
    accel_y = None
    accel_z = None

    gyro_x = None
    gyro_y = None
    gyro_z = None

    quat_i = None
    quat_j = None
    quat_k = None
    quat_real = None

    if bno is not None:
        try:
            accel = bno.acceleration
            accel_x = rounded(accel[0])
            accel_y = rounded(accel[1])
            accel_z = rounded(accel[2])
        except Exception as exc:
            errors.append("bno085_accel:" + str(exc))

        try:
            gyro = bno.gyro
            gyro_x = rounded(gyro[0])
            gyro_y = rounded(gyro[1])
            gyro_z = rounded(gyro[2])
        except Exception as exc:
            errors.append("bno085_gyro:" + str(exc))

        try:
            quat = bno.quaternion
            quat_i = rounded(quat[0])
            quat_j = rounded(quat[1])
            quat_k = rounded(quat[2])
            quat_real = rounded(quat[3])
        except Exception as exc:
            errors.append("bno085_quaternion:" + str(exc))

    # ---------- Wheel / IR ----------

    try:
        wheel_last_raw_us = read_ir_rc(IR_FULL_TIMEOUT_US)
    except Exception as exc:
        errors.append("wheel_ir_read:" + str(exc))

    telemetry = {
        "schema": "sensortrain.telemetry.v1",
        "firmware": FIRMWARE_VERSION,
        "seq": seq,
        "uptime_s": rounded(time.monotonic(), 1),

        "status": "ok" if not errors else "degraded",

        "environment": {
            "temperature_c": ambient_c,
            "humidity_pct": humidity_pct,
            "pressure_hpa": pressure_hpa,
        },

        "temperatures": {
            "compute_c": compute_c,
            "exhaust_c": exhaust_c,
        },

        "motion": {
            "accel_x": accel_x,
            "accel_y": accel_y,
            "accel_z": accel_z,

            "gyro_x": gyro_x,
            "gyro_y": gyro_y,
            "gyro_z": gyro_z,

            "quat_i": quat_i,
            "quat_j": quat_j,
            "quat_k": quat_k,
            "quat_real": quat_real,
        },

        "wheel": {
            "marker_active": wheel_marker_active,
            "rc_us": wheel_last_raw_us,
            "count": wheel_count,
            "period_s": rounded(wheel_period_s, 4),
            "rpm": rounded(wheel_rpm, 1),
            "speed_mps": rounded(wheel_speed_mps, 3),
            "distance_m": rounded(
                wheel_count * WHEEL_CIRCUMFERENCE_M, 3
            ),
        },

        "fans": {
            "intake_pct": intake_fan_pct,
            "exhaust_pct": exhaust_fan_pct,
        },

        "errors": errors,
    }

    print(json.dumps(telemetry))

    seq += 1


# --------------------------------------------------
# Main service loop
#
# No one-second sleep here: the QTR RC sensor must be
# sampled continuously so wheel events are not missed.
# --------------------------------------------------

while True:
    process_serial_commands()

    service_wheel_sensor()

    now = time.monotonic()

    service_wheel_stopped(now)
    service_ds_temperatures(now)

    if now >= next_telemetry_at:
        emit_telemetry()

        next_telemetry_at += TELEMETRY_SECONDS

        # If a long operation ever puts us more than one interval
        # behind, resume from "now" instead of flooding catch-up frames.
        if next_telemetry_at <= now:
            next_telemetry_at = now + TELEMETRY_SECONDS
