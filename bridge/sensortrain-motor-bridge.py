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

    # SENSORTRAIN_134I_HEADLIGHT_TELEMETRY
    "headlight_portb_ready": False,
    "headlight_commanded_pct": None,
    "headlight_last_write": "UNKNOWN",
    "headlight_status_at": None,
    "last_control_id": None,

    # SENSORTRAIN_134I_LIGHT_CONTROL_STATE
    "last_headlight_id": None,
    "headlight_requested_pct": None,
    "headlight_request_result": "NONE",
    "control_source_ok": False,

    # SENSORTRAIN_V15C_BRIDGE_RECOVERY_V1
    "admin_recovery_id": None,
    "admin_recovery_phase": "IDLE",
    "admin_recovery_result": "NONE",

    # SENSORTRAIN_V12_DUAL_MOTOR_BRIDGE_V1
    "pair_ready": False,
    "dual_status_fresh": False,
    "fault_latched": None,
    "dual_drive_active": False,
    "locomotive_power": 0,
    "caboose_power": 0,
    "drive_l_pending": False,
    "drive_c_pending": False,
    "hubs": {
        "locomotive": {
            "connected": False,
            "gatt": "NOT_READY",
            "notify": "NOT_READY",
            "sign": None,
            "motor0": False,
            "battery_pct": None,
        },
        "caboose": {
            "connected": False,
            "gatt": "NOT_READY",
            "notify": "NOT_READY",
            "sign": None,
            "motor0": False,
            "battery_pct": None,
        },
    },
}

dual_pair_received_at = None


# SENSORTRAIN_134I_LIGHT_PARSER
def parse_headlight_line(line):

    if line.startswith("LOCOMOTIVE:") and             "DISCONNECTED" in line:
        state["headlight_portb_ready"] = False
        state["headlight_commanded_pct"] = None
        state["headlight_status_at"] = None
        return False

    if line.startswith("LIGHT PORTB="):
        ready = line == "LIGHT PORTB=READY"

        state["headlight_portb_ready"] = ready

        if not ready:
            state["headlight_commanded_pct"] = None
            state["headlight_status_at"] = None

        return True

    match = re.fullmatch(
        r"LIGHTSTATUS PORTB=(READY|MISSING) "
        r"COMMANDED=(\d+)",
        line
    )

    if match:
        ready = match.group(1) == "READY"
        level = int(match.group(2))

        state["headlight_portb_ready"] = ready
        state["headlight_status_at"] = time.time()

        state["headlight_commanded_pct"] = (
            level if ready and level in (0, 50, 100)
            else None
        )
        return True

    match = re.fullmatch(
        r"LIGHT SUBMITTED LEVEL=(\d+)",
        line
    )

    if match:
        level = int(match.group(1))

        if level in (0, 50, 100):
            state["headlight_commanded_pct"] = level
            state["headlight_last_write"] = "SUBMITTED"

        return True

    if line.startswith("LIGHT QUEUED LEVEL="):
        state["headlight_last_write"] = "QUEUED"
        return True

    if line == "LIGHT BLE_WRITE_OK":
        state["headlight_last_write"] = "ACKNOWLEDGED"
        return True

    if line == "LIGHT BLE_WRITE_FAILED" or             line.startswith("ERROR LIGHT_"):
        state["headlight_last_write"] = "FAILED"
        return True

    return False





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



# SENSORTRAIN_134I_HEADLIGHT_BRIDGE_CONTROL

def _st_original_headlight_send_allowed(status, control, now):
    """Authorize live lighting only on healthy links and idle motor writes."""
    if not isinstance(status, dict) or not isinstance(control, dict):
        return False

    if (
        control.get("command") != "speed"
        or type(control.get("speed")) is not int
        or not -100 <= control["speed"] <= 100
    ):
        return False

    for key in (
        "serial_connected", "control_source_ok", "dual_status_fresh",
        "pair_ready", "headlight_portb_ready",
    ):
        if status.get(key) is not True:
            return False

    if (
        status.get("fault_latched") is not False
        or status.get("drive_l_pending") is not False
        or status.get("drive_c_pending") is not False
    ):
        return False

    if type(status.get("dual_drive_active")) is not bool:
        return False

    for key in ("speed", "locomotive_power", "caboose_power"):
        if type(status.get(key)) is not int or not -100 <= status[key] <= 100:
            return False

    stamp = status.get("headlight_status_at")
    if (
        type(stamp) not in (int, float)
        or not 0 <= now - stamp <= 3.0
    ):
        return False

    hubs = status.get("hubs")
    if not isinstance(hubs, dict):
        return False
    for role in ("locomotive", "caboose"):
        hub = hubs.get(role)
        if not isinstance(hub, dict):
            return False
        if (
            hub.get("connected") is not True
            or hub.get("gatt") != "READY"
            or hub.get("notify") != "READY"
            or hub.get("motor0") is not True
        ):
            return False

    return True


def headlight_send_allowed(status, control, now):
    # Diagnostic wrapper: original authorization unchanged.
    accepted = _st_original_headlight_send_allowed(
        status, control, now
    )

    if not accepted:
        s = status if isinstance(status, dict) else {}
        c = control if isinstance(control, dict) else {}

        keys = (
            "serial_connected", "control_source_ok",
            "dual_status_fresh", "pair_ready",
            "headlight_portb_ready", "fault_latched",
            "dual_drive_active", "drive_l_pending",
            "drive_c_pending", "speed",
            "locomotive_power", "caboose_power",
        )

        stamp = s.get("headlight_status_at")
        age = (
            round(now - stamp, 3)
            if type(stamp) in (int, float)
            else "MISSING"
        )

        snapshot = {
            "control_command": c.get("command"),
            "control_speed": c.get("speed"),
            "headlight_age_s": age,
            "flags": {k: s.get(k) for k in keys},
            "hubs": s.get("hubs"),
        }

        print(
            "HEADLIGHT_GATE_DIAGNOSTIC "
            + json.dumps(snapshot, sort_keys=True),
            flush=True
        )

    return accepted



# SENSORTRAIN_134I_45_LOCAL_FRESHNESS
def sensortrain_refresh_local_dual_freshness():
    stamp = state.get("_st_dual_ready_mono")
    now = time.monotonic()

    fresh = (
        type(stamp) in (int, float)
        and 0 <= now - stamp <= 2.5
        and state.get("pair_ready") is True
    )

    state["dual_status_fresh"] = fresh
    return fresh


def consume_headlight_request(fd, control, recovered):
    """Consume each light ID once, retrying brief BLE-motor contention."""
    sensortrain_refresh_local_dual_freshness()
    light = control.get("headlight")
    if not isinstance(light, dict):
        return "ABSENT"
    command_id = light.get("id")
    if type(command_id) is not int or command_id <= 0:
        return "ABSENT"

    if recovered:
        state["last_headlight_id"] = command_id
        state["headlight_request_result"] = "SUPPRESSED_RECOVERY"
        state.pop("_st_light_retry_id", None)
        return "SUPPRESSED_RECOVERY"
    if command_id == state.get("last_headlight_id"):
        return "DUPLICATE"

    level = light.get("level")
    if (
        light.get("command") != "light"
        or type(level) is not int
        or level not in (0, 50, 100)
    ):
        state["last_headlight_id"] = command_id
        state["headlight_request_result"] = "REJECTED_INVALID"
        return "REJECTED_INVALID"

    now_m = time.monotonic()
    if state.get("_st_light_retry_id") != command_id:
        state["_st_light_retry_id"] = command_id
        state["_st_light_retry_deadline"] = now_m + 4.0

    now = time.time()
    allowed = headlight_send_allowed(state, control, now)
    if not allowed:
        # Only transient BLE motor-write activity / dual-status gaps
        # may be retried. Never retry a fault, disconnect, or bad control.
        hypothetical = dict(state)
        hypothetical["drive_l_pending"] = False
        hypothetical["drive_c_pending"] = False
        hypothetical["dual_status_fresh"] = True
        retryable = (
            (
                state.get("drive_l_pending") is True
                or state.get("drive_c_pending") is True
                or state.get("dual_status_fresh") is False
            )
            and headlight_send_allowed(hypothetical, control, now)
        )
        if retryable and now_m < state["_st_light_retry_deadline"]:
            state["headlight_request_result"] = "WAITING_MOTOR_BLE"
            return "WAITING_MOTOR_BLE"

        state["last_headlight_id"] = command_id
        state["headlight_request_result"] = (
            "REJECTED_BUSY_TIMEOUT" if retryable else "REJECTED_UNSAFE"
        )
        state.pop("_st_light_retry_id", None)
        return state["headlight_request_result"]

    # Mark consumed BEFORE transmission. Recovery never replays lights.
    state["last_headlight_id"] = command_id
    state.pop("_st_light_retry_id", None)
    send(fd, "LIGHT {}".format(level))
    state["headlight_requested_pct"] = level
    state["headlight_last_write"] = "REQUESTED"
    state["headlight_request_result"] = "SENT"
    print("Headlight request sent: {}%".format(level), flush=True)
    return "SENT"


# SENSORTRAIN_V15C_BRIDGE_RECOVERY_V1
# Admin requests are ID-tagged, sent once, and never resume prior speed.
def _admin_recovery_healthy():
    sensortrain_refresh_local_dual_freshness()
    return (
        state.get("serial_connected") is True
        and state.get("control_source_ok") is True
        and state.get("fault_latched") is False
        and state.get("pair_ready") is True
        and state.get("dual_status_fresh") is True
        and state.get("speed") == 0
        and state.get("locomotive_power") == 0
        and state.get("caboose_power") == 0
    )


def process_admin_recovery(fd, control):
    """Dispatch one zero-speed firmware restart for a pending Admin request."""
    req = control.get("recovery") if isinstance(control, dict) else None
    if not isinstance(req, dict) or req.get("locked") is not True:
        return
    rid = req.get("id")
    if type(rid) is not int or rid <= 0:
        return
    if rid != state.get("admin_recovery_id"):
        state["admin_recovery_id"] = rid
        state["admin_recovery_phase"] = "WAITING"
        state["admin_recovery_result"] = "PENDING"
        state["_admin_rec_started"] = time.monotonic()
        state["_admin_rec_sent"] = False
        state["_admin_rec_saw_restart"] = False
        print("Admin recovery request received id={}".format(rid), flush=True)

    if state["admin_recovery_phase"] in ("DONE", "FAILED"):
        return

    # A reconnect/restart may have already cleared the latch. This also
    # covers a bridge service restart during an ESP32 reboot.
    if _admin_recovery_healthy():
        state["admin_recovery_phase"] = "DONE"
        state["admin_recovery_result"] = "FAULT_CLEARED"
        print("Admin recovery confirmed fault-free id={}".format(rid), flush=True)
        return

    elapsed = time.monotonic() - state["_admin_rec_started"]
    if elapsed >= 45.0:
        state["admin_recovery_phase"] = "FAILED"
        state["admin_recovery_result"] = "TIMEOUT_WAITING_FOR_FAULT_CLEAR"
        print("Admin recovery timed out id={}".format(rid), flush=True)
        return

    # Once the web server changes REQUESTED to RUNNING, a bridge restart
    # must not replay the firmware-reset command.
    if req.get("phase") != "REQUESTED":
        return

    if state["_admin_rec_sent"]:
        return

    # Do not issue a reset while the host or controller reports motion.
    if not (state.get("serial_connected") is True
            and state.get("control_source_ok") is True
            and state.get("fault_latched") is True
            and state.get("speed") == 0
            and state.get("locomotive_power") == 0
            and state.get("caboose_power") == 0
            and state.get("drive_l_pending") is False
            and state.get("drive_c_pending") is False):
        return

    # Mark as sent BEFORE writing to prevent retransmission on a delayed
    # reply; a new Admin click creates a new request ID.
    state["_admin_rec_sent"] = True
    state["admin_recovery_phase"] = "SENT"
    state["admin_recovery_result"] = "PENDING"
    send(fd, "ADMINRECOVER RESET")
    print("Admin recovery command sent id={}".format(rid), flush=True)


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
    data = dict(state)

    data["dual_status_fresh"] = (
        dual_pair_received_at is not None
        and
        time.monotonic() - dual_pair_received_at <= 3.0
    )

    body = json.dumps(
        data,
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



def parse_dual_status(line):
    global dual_pair_received_at

    m = re.fullmatch(
        r"DUALSTATUS (LOCOMOTIVE|CABOOSE) "
        r"CONNECTED=(YES|NO) "
        r"GATT=(READY|NOT_READY) "
        r"NOTIFY=(READY|NOT_READY) "
        r"SIGN=(-?1|0) BATTERY=(-?\d+)",
        line
    )

    if m:
        hub = state["hubs"][m.group(1).lower()]

        hub["connected"] = m.group(2) == "YES"
        hub["gatt"] = m.group(3)
        hub["notify"] = m.group(4)
        hub["sign"] = int(m.group(5))

        value = int(m.group(6))
        hub["battery_pct"] = (
            value if 0 <= value <= 100 else None
        )

        if not hub["connected"]:
            hub["motor0"] = False
            state["pair_ready"] = False
            state["dual_drive_active"] = False
            state["speed"] = 0

        return True

    m = re.fullmatch(
        r"DUALSTATUS PAIR_READY=(YES|NO) "
        r"MOTION=\S+",
        line
    )

    if m:
        state["pair_ready"] = m.group(1) == "YES"
        dual_pair_received_at = time.monotonic()
        return True

    m = re.fullmatch(
        r"CALSTATUS (LOCOMOTIVE|CABOOSE) "
        r"LINK=(READY|NOT_READY) "
        r"MOTOR0=(YES|NO) BATTERY=(-?\d+)",
        line
    )

    if m:
        hub = state["hubs"][m.group(1).lower()]

        hub["motor0"] = (
            m.group(2) == "READY" and
            m.group(3) == "YES"
        )

        if not hub["motor0"]:
            state["pair_ready"] = False

        value = int(m.group(4))
        hub["battery_pct"] = (
            value if 0 <= value <= 100 else None
        )

        return True

    m = re.fullmatch(
        r"SAFETYSTATUS LATCHED=(YES|NO) "
        r"MOTION=\S+",
        line
    )

    if m:
        state["fault_latched"] = m.group(1) == "YES"

        if state["fault_latched"]:
            state["dual_drive_active"] = False
            state["speed"] = 0

        return True

    m = re.fullmatch(
        r"DUALDRIVESTATUS ACTIVE=(YES|NO) "
        r"SPEED=(-?\d+) FAULT=(YES|NO) "
        r"LOCO_POWER=(-?\d+) "
        r"CABOOSE_POWER=(-?\d+) "
        r"L_PENDING=(YES|NO) "
        r"C_PENDING=(YES|NO)",
        line
    )

    if m:
        state["dual_drive_active"] = (
            m.group(1) == "YES"
        )

        state["speed"] = int(m.group(2))
        state["fault_latched"] = m.group(3) == "YES"
        state["locomotive_power"] = int(m.group(4))
        state["caboose_power"] = int(m.group(5))
        state["drive_l_pending"] = m.group(6) == "YES"
        state["drive_c_pending"] = m.group(7) == "YES"

        if state["fault_latched"]:
            state["dual_drive_active"] = False
            state["speed"] = 0

        return True

    if line.startswith("SAFETY FAULT LATCHED:"):
        state["fault_latched"] = True
        state["dual_drive_active"] = False
        state["speed"] = 0
        return True

    for role in ("LOCOMOTIVE", "CABOOSE"):
        if line.startswith(role + " DISCONNECTED"):
            hub = state["hubs"][role.lower()]
            hub["connected"] = False
            hub["gatt"] = "NOT_READY"
            hub["notify"] = "NOT_READY"
            hub["motor0"] = False
            hub["battery_pct"] = None
            state["pair_ready"] = False
            state["dual_drive_active"] = False
            state["speed"] = 0
            return True

    return False


def process_line(line):
    global dual_pair_received_at

    if not line:
        return

    # SENSORTRAIN_PORTB_DISCOVERY_V1
    if line.startswith("ATTACHED-I/O: PORT=1 "):
        print("PORTB_DISCOVERY " + line, flush=True)

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

    # SENSORTRAIN_V15C_BRIDGE_RECOVERY_V1 — firmware reply handling.
    if line == "RECOVERY RESTARTING":
        state["_admin_rec_saw_restart"] = True
        state["admin_recovery_phase"] = "RESTARTING"
        state["admin_recovery_result"] = "PENDING"
        # Invalidate pre-reboot hub data. A new, independent pair-ready
        # sample must arrive before the Admin API may call recovery done.
        dual_pair_received_at = None
        state["_st_dual_ready_mono"] = None
        state["pair_ready"] = False
        state["dual_status_fresh"] = False
        state["fault_latched"] = None
        state["dual_drive_active"] = False
        state["headlight_portb_ready"] = False
        state["speed"] = 0
        state["locomotive_power"] = 0
        state["caboose_power"] = 0
        for hub in state["hubs"].values():
            hub["connected"] = False
            hub["gatt"] = "NOT_READY"
            hub["notify"] = "NOT_READY"
            hub["motor0"] = False
        print("ESP32 acknowledged Admin recovery reboot", flush=True)
        return
    if line == "RECOVERY ALREADY_CLEAR":
        state["admin_recovery_phase"] = "WAITING"
        state["admin_recovery_result"] = "WAITING_FOR_HEALTHY_TELEMETRY"
        return
    if line.startswith("RECOVERY REJECTED") or (
            line.startswith("ERROR UNKNOWN_COMMAND:")
            and "ADMINRECOVER RESET" in line):
        state["admin_recovery_phase"] = "FAILED"
        state["admin_recovery_result"] = "REJECTED_BY_ESP32"
        print("Admin recovery failed: " + line, flush=True)
        return

    if parse_headlight_line(line):
        return

    # SENSORTRAIN_134I_45_PAIR_TIMESTAMP
    if line.startswith("DUALSTATUS PAIR_READY="):
        if line == "DUALSTATUS PAIR_READY=YES MOTION=LOCKED":
            state["_st_dual_ready_mono"] = time.monotonic()
        else:
            state["_st_dual_ready_mono"] = None

    if parse_dual_status(line):
        return

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

        initial_light = control.get("headlight")

        if isinstance(initial_light, dict):
            state["last_headlight_id"] = (
                initial_light.get("id")
            )

        print(
            "Existing headlight command suppressed",
            flush=True
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

                    # SENSORTRAIN_134I_LIGHT_DISPATCH
                    consume_headlight_request(
                        fd,
                        control,
                        recovered
                    )

                    # SENSORTRAIN_V15C_BRIDGE_RECOVERY_V1
                    process_admin_recovery(fd, control)

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
                send(fd, "DUALSTATUS")
                send(fd, "CALSTATUS")
                send(fd, "SAFETYSTATUS")
                send(fd, "DUALDRIVESTATUS")
                send(fd, "LIGHTSTATUS")


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
