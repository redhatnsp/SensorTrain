#!/usr/bin/python3

import json
import os
import re
import select
import signal
import termios
import time
import tty
import urllib.request


DEVICE = os.environ.get(
    "MOTOR_DEVICE",
    "/dev/sensortrain-motor"
)

BASE_URL = os.environ.get(
    "MOTOR_BASE_URL",
    "http://10.43.180.47:8080"
).rstrip("/")

CONTROL_URL = BASE_URL + "/api/motor/control"
STATUS_URL = BASE_URL + "/api/motor/status"

HEARTBEAT_SECONDS = 0.25
CONTROL_POLL_SECONDS = 0.5
STATUS_REQUEST_SECONDS = 1.0
STATUS_POST_SECONDS = 1.0

running = True


state = {
    "schema": "sensortrain.motor.v1",
    "serial_connected": False,
    "speed": 0,
    "ble": "UNKNOWN",
    "hub": "UNKNOWN",
    "gatt": "UNKNOWN",
    "notify": "UNKNOWN",
    # SENSORTRAIN_MOTOR_BATTERY_V1
    "battery_pct": None,
    "last_message": "",
    "last_control_id": None,
    "control_source_ok": False,
}


def stop_signal(signum, frame):
    global running
    running = False


signal.signal(signal.SIGTERM, stop_signal)
signal.signal(signal.SIGINT, stop_signal)


def send(fd, command):
    wire = (
        command.strip()
        + "\n"
    ).encode("utf-8")

    os.write(fd, wire)
    termios.tcdrain(fd)


def get_control():
    request = urllib.request.Request(
        CONTROL_URL,
        headers={
            "Cache-Control": "no-store"
        },
        method="GET",
    )

    with urllib.request.urlopen(
        request,
        timeout=2
    ) as response:

        if response.status != 200:
            raise RuntimeError(
                "unexpected control HTTP status {}".format(
                    response.status
                )
            )

        return json.loads(
            response.read().decode("utf-8")
        )


def post_status():
    body = json.dumps(
        state,
        separators=(",", ":")
    ).encode("utf-8")

    request = urllib.request.Request(
        STATUS_URL,
        data=body,
        headers={
            "Content-Type": "application/json"
        },
        method="POST",
    )

    with urllib.request.urlopen(
        request,
        timeout=2
    ) as response:

        if response.status != 204:
            raise RuntimeError(
                "unexpected status HTTP status {}".format(
                    response.status
                )
            )


def parse_status(line):

    match = re.search(
        r"STATUS\s+"
        r"SPEED=(-?\d+)\s+"
        r"BLE=(\S+)\s+"
        r"HUB=(\S+)\s+"
        r"GATT=(\S+)\s+"
        r"NOTIFY=(\S+)"
        r"(?:\s+BATTERY=(-?\d+))?",
        line
    )

    if not match:
        return False

    state["speed"] = int(
        match.group(1)
    )

    state["ble"] = match.group(2)
    state["hub"] = match.group(3)
    state["gatt"] = match.group(4)
    state["notify"] = match.group(5)

    battery = match.group(6)

    if battery is not None:
        value = int(battery)

        if 0 <= value <= 100:
            state["battery_pct"] = value
        else:
            state["battery_pct"] = None

    return True


def process_line(line):

    if not line:
        return

    if line in (
        "PING",
        "PONG",
        ">",
        "> PING"
    ):
        return

    if line.startswith("> PING"):
        return

    state["last_message"] = line

    if parse_status(line):
        return

    battery_match = re.match(
        r"HUB BATTERY\s+(\d+)%",
        line
    )

    if battery_match:
        value = int(battery_match.group(1))

        if 0 <= value <= 100:
            state["battery_pct"] = value

        return

    if "HUB CONNECTED" in line:
        state["hub"] = "CONNECTED"
        state["battery_pct"] = None

    elif "HUB DISCONNECTED" in line:
        state["hub"] = "DISCONNECTED"
        state["speed"] = 0
        state["battery_pct"] = None

    elif "LEGO GATT READY" in line:
        state["gatt"] = "READY"

    elif "LEGO NOTIFICATIONS ENABLED" in line:
        state["notify"] = "READY"

    elif "FAILSAFE:" in line:
        state["speed"] = 0

    elif line.startswith("SPEED "):
        try:
            state["speed"] = int(
                line.split()[1]
            )
        except Exception:
            pass

    elif line == "STOPPED":
        state["speed"] = 0


def open_serial():

    fd = os.open(
        DEVICE,
        os.O_RDWR
        | os.O_NOCTTY
        | os.O_NONBLOCK
    )

    tty.setraw(fd)

    attrs = termios.tcgetattr(fd)

    attrs[4] = termios.B115200
    attrs[5] = termios.B115200

    termios.tcsetattr(
        fd,
        termios.TCSANOW,
        attrs
    )

    termios.tcflush(
        fd,
        termios.TCIOFLUSH
    )

    return fd


def main():

    global running

    print(
        "SensorTrain motor bridge starting",
        flush=True
    )

    print(
        "Motor device: {}".format(
            DEVICE
        ),
        flush=True
    )

    print(
        "Control source: {}".format(
            CONTROL_URL
        ),
        flush=True
    )

    print(
        "Status destination: {}".format(
            STATUS_URL
        ),
        flush=True
    )

    fd = open_serial()

    state["serial_connected"] = True

    print(
        "ESP32 connected: {}".format(
            DEVICE
        ),
        flush=True
    )

    # ------------------------------------------
    # Safe startup.
    #
    # Always stop the train first. Then read the
    # current control ID and mark it as already
    # seen so an old command is never replayed
    # after a bridge/service restart.
    # ------------------------------------------

    send(fd, "STOP")
    time.sleep(0.2)
    send(fd, "STATUS")

    try:

        control = get_control()

        state["last_control_id"] = (
            control.get("id")
        )

        state["control_source_ok"] = True

        print(
            "Control source established; "
            "existing command suppressed",
            flush=True
        )

    except Exception as exc:

        state["control_source_ok"] = False

        print(
            "Initial control source unavailable: {}".format(
                exc
            ),
            flush=True
        )

    buffer = b""

    next_ping = time.monotonic()
    next_control = time.monotonic()
    next_status_request = time.monotonic()
    next_status_post = time.monotonic()

    control_was_ok = state[
        "control_source_ok"
    ]

    last_control_error = None
    last_status_error = None

    try:

        while running:

            now = time.monotonic()

            # ----------------------------------
            # Poll MicroShift desired state.
            # ----------------------------------

            if now >= next_control:

                next_control = (
                    now
                    + CONTROL_POLL_SECONDS
                )

                try:

                    control = get_control()

                    recovered = (
                        not control_was_ok
                    )

                    state[
                        "control_source_ok"
                    ] = True

                    control_id = control.get(
                        "id"
                    )

                    if recovered:

                        # Never replay a command
                        # that accumulated while
                        # control was unreachable.
                        state[
                            "last_control_id"
                        ] = control_id

                        send(fd, "STOP")

                        state["speed"] = 0

                        print(
                            "Control source recovered; "
                            "train remains stopped until "
                            "a new command is issued",
                            flush=True
                        )

                    elif (
                        control_id
                        and
                        control_id
                        != state[
                            "last_control_id"
                        ]
                    ):

                        if (
                            control.get("command")
                            == "speed"
                        ):

                            speed = control.get(
                                "speed"
                            )

                            if (
                                type(speed) is int
                                and
                                -100 <= speed <= 100
                            ):

                                if speed == 0:

                                    send(
                                        fd,
                                        "STOP"
                                    )

                                else:

                                    send(
                                        fd,
                                        "SPEED {}".format(
                                            speed
                                        )
                                    )

                                state[
                                    "last_control_id"
                                ] = control_id

                                print(
                                    "Motor control applied: "
                                    "id={} speed={}".format(
                                        control_id,
                                        speed
                                    ),
                                    flush=True
                                )

                    control_was_ok = True
                    last_control_error = None

                except Exception as exc:

                    state[
                        "control_source_ok"
                    ] = False

                    if control_was_ok:

                        print(
                            "Control source lost; "
                            "stopping motor and "
                            "withholding heartbeat",
                            flush=True
                        )

                        try:
                            send(fd, "STOP")
                        except Exception:
                            pass

                        state["speed"] = 0

                    message = str(exc)

                    if message != last_control_error:

                        print(
                            "Control poll failed: {}".format(
                                message
                            ),
                            flush=True
                        )

                    last_control_error = message
                    control_was_ok = False


            # ----------------------------------
            # ESP32 heartbeat.
            #
            # Only sent while the MicroShift
            # control source is healthy.
            # ----------------------------------

            if now >= next_ping:

                next_ping = (
                    now
                    + HEARTBEAT_SECONDS
                )

                if state[
                    "control_source_ok"
                ]:

                    send(fd, "PING")


            # ----------------------------------
            # Ask ESP32 for actual state.
            # ----------------------------------

            if now >= next_status_request:

                next_status_request = (
                    now
                    + STATUS_REQUEST_SECONDS
                )

                send(fd, "STATUS")


            # ----------------------------------
            # Post latest actual state.
            # ----------------------------------

            if now >= next_status_post:

                next_status_post = (
                    now
                    + STATUS_POST_SECONDS
                )

                try:

                    post_status()

                    if last_status_error is not None:

                        print(
                            "Motor status reporting recovered",
                            flush=True
                        )

                    last_status_error = None

                except Exception as exc:

                    message = str(exc)

                    if message != last_status_error:

                        print(
                            "Motor status POST failed: {}".format(
                                message
                            ),
                            flush=True
                        )

                    last_status_error = message


            # ----------------------------------
            # Read serial data.
            # ----------------------------------

            readable, _, _ = select.select(
                [fd],
                [],
                [],
                0.05
            )

            if fd in readable:

                chunk = os.read(
                    fd,
                    4096
                )

                if not chunk:
                    raise RuntimeError(
                        "ESP32 serial device disconnected"
                    )

                buffer += chunk

                while b"\n" in buffer:

                    raw, buffer = (
                        buffer.split(
                            b"\n",
                            1
                        )
                    )

                    line = raw.decode(
                        "utf-8",
                        errors="replace"
                    ).strip()

                    process_line(
                        line
                    )

    finally:

        state["serial_connected"] = False
        state["control_source_ok"] = False

        try:
            send(fd, "STOP")
            time.sleep(0.1)
        except Exception:
            pass

        try:
            os.close(fd)
        except Exception:
            pass

        print(
            "SensorTrain motor bridge stopped",
            flush=True
        )


if __name__ == "__main__":
    main()
