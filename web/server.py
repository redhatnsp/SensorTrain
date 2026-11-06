import json
import ssl
import urllib.request
import time
from collections import deque
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, HTTPServer

latest = None
received_at = None

# Roughly 45-50 minutes at the current telemetry
# cadence, providing headroom above the 30-minute
# graph window.
history = deque(maxlen=1200)

# Independent system-health state.
bridge_heartbeat_at = None

# SENSORTRAIN_UI_V2
# Temperature reported by the Raspberry Pi host.
host_temperature = {
    "pi_cpu_c": None,
    "received_at": None
}

HOST_TEMP_FRESH_SECONDS = 6.0


cluster_check_at = 0.0
cluster_ready = False
cluster_error = None

PICO_FRESH_SECONDS = 8.0
BRIDGE_FRESH_SECONDS = 6.0
CLUSTER_CACHE_SECONDS = 3.0


# SENSORTRAIN_134I_ADMIN_AUTH_V2

# SENSORTRAIN_V15C_RECOVERY_RECONCILE_V1
# Called from existing control/status GETs; does not issue hardware commands.
def sensortrain_admin_reconcile_recovery():
    import time as _rec_time
    now = _rec_time.time()
    with recovery_lock:
        req = recovery_request
        if req.get("locked") is not True:
            return
        rid = req.get("id")
        requested_at = req.get("requested_at")
        if type(rid) is not int:
            return
        if type(requested_at) not in (float, int):
            requested_at = now

        status = latest_motor_status
        fresh = (isinstance(status, dict)
                 and type(motor_status_at) in (float, int)
                 and 0 <= now - motor_status_at <= 3.0)
        if fresh and status.get("admin_recovery_id") == rid:
            phase = status.get("admin_recovery_phase")
            if phase == "FAILED":
                req.update({"phase": "FAILED", "locked": False,
                            "result": str(status.get("admin_recovery_result") or "ESP32_RESET_FAILED")})
                return
            if (phase == "DONE"
                and status.get("serial_connected") is True
                and status.get("control_source_ok") is True
                and status.get("dual_status_fresh") is True
                and status.get("pair_ready") is True
                and status.get("fault_latched") is False
                and status.get("speed") == 0
                and status.get("locomotive_power") == 0
                and status.get("caboose_power") == 0):
                req.update({"phase": "CLEARED", "locked": False,
                            "result": "FAULT_CLEARED"})
                return
            if phase in ("SENT", "RESTARTING"):
                req["phase"] = "RUNNING"
        if now - requested_at > 60.0:
            req.update({"phase": "FAILED", "locked": False,
                        "result": "TIMEOUT_WAITING_FOR_ESP32"})


def sensortrain_admin_gate(handler):
    import os
    import hmac
    import base64
    import binascii
    import ssl

    def reject(status, message, challenge=False):
        body = message.encode("utf-8")

        handler.send_response(status)
        handler.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )
        handler.send_header(
            "Cache-Control",
            "no-store"
        )
        handler.send_header(
            "Content-Length",
            str(len(body))
        )

        if challenge:
            handler.send_header(
                "WWW-Authenticate",
                'Basic realm="SensorTrain Admin"'
            )

        handler.end_headers()
        handler.wfile.write(body)
        return False

    password = os.environ.get(
        "SENSORTRAIN_ADMIN_PASSWORD", ""
    )

    tls_ready = (
        os.environ.get(
            "SENSORTRAIN_ADMIN_TLS_READY"
        ) == "1"
    )

    if len(password) < 8 or not tls_ready:
        return reject(
            503,
            "Administrator access not configured"
        )

    # Admin requires TLS on the actual application socket.
    # Forwarded protocol headers are never proof of TLS.
    try:
        tls_verified = (
            isinstance(handler.connection, ssl.SSLSocket)
            and bool(handler.connection.version())
        )
    except (OSError, ValueError):
        tls_verified = False

    if not tls_verified:
        return reject(403, "HTTPS required")

    header = handler.headers.get(
        "Authorization", ""
    )

    user = ""
    supplied = ""

    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(
                header[6:].strip(),
                validate=True
            ).decode("utf-8")

            user, supplied = decoded.split(":", 1)

        except (
            ValueError,
            UnicodeError,
            binascii.Error
        ):
            pass

    if not (
        hmac.compare_digest(user, "admin")
        and hmac.compare_digest(
            supplied, password
        )
    ):
        return reject(
            401,
            "Administrator authentication required",
            challenge=True
        )

    return True


latest_control = {
    "id": 0,
    "command": None
}


latest_motor_control = {
    "id": 0,
    "command": None
}


# SENSORTRAIN_134I_HEADLIGHT_API_V2

latest_headlight_control = {
    "id": 0,
    "command": None,
    "level": None,
}


def headlight_request_allowed(
        status, motor_control, status_at, now):
    """Allow lighting while moving; never bypass transport/fault gates.

    BLE write arbitration is performed again at the bridge and ESP32.
    """
    if recovery_request["locked"]:
        return False

    if not isinstance(status, dict) or not isinstance(motor_control, dict):
        return False

    # Require a valid known motor command, but NOT speed zero.
    if (
        motor_control.get("command") != "speed"
        or type(motor_control.get("speed")) is not int
        or not -100 <= motor_control["speed"] <= 100
    ):
        return False

    if (
        type(status_at) not in (int, float)
        or not 0 <= now - status_at <= 2.5
    ):
        return False

    for key in (
        "serial_connected", "control_source_ok", "dual_status_fresh",
        "pair_ready", "headlight_portb_ready",
    ):
        if status.get(key) is not True:
            return False

    if status.get("fault_latched") is not False:
        return False

    # Pending motor writes are allowed here: bridge defers the light
    # request until they finish, ESP32 arbitrates actual BLE writes.
    for key in ("drive_l_pending", "drive_c_pending", "dual_drive_active"):
        if type(status.get(key)) is not bool:
            return False

    for key in ("speed", "locomotive_power", "caboose_power"):
        if type(status.get(key)) is not int or not -100 <= status[key] <= 100:
            return False

    light_at = status.get("headlight_status_at")
    if (
        type(light_at) not in (int, float)
        or not 0 <= now - light_at <= 3.0
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


# SENSORTRAIN_134I_RECOVERY_LOCK_V2

import threading

recovery_lock = threading.RLock()

recovery_request = {
    "id": 0,
    "phase": "IDLE",
    "locked": False,
    "requested_at": None,
    "result": "NONE",
}


def sensortrain_recovery_ready(
        status, motor, status_at, now):

    if not isinstance(status, dict):
        return False

    if not isinstance(motor, dict):
        return False

    if (
        motor.get("command") != "speed"
        or type(motor.get("speed")) is not int
        or motor["speed"] != 0
    ):
        return False

    if (
        type(status_at) not in (int, float)
        or not 0 <= now - status_at <= 2.5
    ):
        return False

    recovery_at = status.get(
        "recovery_status_at"
    )

    if (
        type(recovery_at) not in (int, float)
        or not 0 <= now - recovery_at <= 3.0
    ):
        return False

    required_true = (
        "serial_connected",
        "control_source_ok",
        "dual_status_fresh",
        "pair_ready",
        "recovery_latched",
        "recovery_disconnect_seen",
        "recovery_preflight",
    )

    for name in required_true:
        if status.get(name) is not True:
            return False

    required_false = (
        "recovery_blocking_fault_seen",
        "recovery_attempted",
        "recovery_armed",
        "dual_drive_active",
        "drive_l_pending",
        "drive_c_pending",
    )

    for name in required_false:
        if status.get(name) is not False:
            return False

    if status.get("fault_latched") is not True:
        return False

    for name in (
        "speed",
        "locomotive_power",
        "caboose_power",
    ):
        if (
            type(status.get(name)) is not int
            or status[name] != 0
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


def sensortrain_same_origin(handler):
    from urllib.parse import urlsplit

    origin = handler.headers.get("Origin", "")
    host = handler.headers.get("Host", "")

    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False

    return (
        bool(host)
        and parsed.scheme == "https"
        and parsed.netloc.lower() == host.lower()
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    )


latest_motor_status = {
    "schema": "sensortrain.motor.v1",
    "serial_connected": False,
    "speed": 0,
    "ble": "UNKNOWN",
    "hub": "UNKNOWN",
    "gatt": "UNKNOWN",
    "notify": "UNKNOWN",
    "last_message": "",
    "last_control_id": None,
    "control_source_ok": False
}

motor_status_at = None


# SENSORTRAIN_ADMIN_SHUTDOWN_STATE_V1
latest_shutdown_request = {
    "id": 0,
    "requested": False,
    "requested_at": None
}

def empty_telemetry():
    return {
        "schema": "sensortrain.telemetry.v1",
        "firmware": None,
        "status": "waiting",
        "seq": None,
        "uptime_s": None,
        "environment": {
            "temperature_c": None,
            "pressure_hpa": None,
            "humidity_pct": None
        },
        "temperatures": {
            "compute_c": None,
            "exhaust_c": None,
            "pi_cpu_c": None
        },
        "motion": {
            "accel_x": None,
            "accel_y": None,
            "accel_z": None,
            "gyro_x": None,
            "gyro_y": None,
            "gyro_z": None,
            "quat_i": None,
            "quat_j": None,
            "quat_k": None,
            "quat_real": None
        },
        "wheel": {
            "marker_active": False,
            "rc_us": None,
            "count": 0,
            "period_s": None,
            "rpm": 0.0,
            "speed_mps": 0.0,
            "distance_m": 0.0
        },
        "fans": {
            "intake_pct": 0,
            "exhaust_pct": 0
        },
        "errors": []
    }

PAGE = r"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport"
        content="width=device-width, initial-scale=1">

  <title>SensorTrain</title>

  <style>
    :root {
      font-family:
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;

      color-scheme: dark;
    }

    * {
      box-sizing: border-box;
    }

    html,
    body {
      margin: 0;
      width: 100%;
      height: 100%;
    }

    body {
      background: #101418;
      color: #e8edf2;
      overflow: hidden;
    }

    button {
      font: inherit;
    }

    .page {
      width: min(1600px, 100%);
      height: 100vh;
      margin: 0 auto;
      padding: 10px 14px;

      display: grid;
      grid-template-rows:
        auto
        122px
        168px
        minmax(360px, 1fr);

      gap: 9px;
    }


    /* ============================================
       Header
       ============================================ */

    .header {
      min-height: 46px;

      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;

      padding: 4px 3px;
    }

    .brand {
      display: flex;
      align-items: baseline;
      gap: 12px;
    }

    .brand-name {
      font-size: 1.45rem;
      font-weight: 850;
      letter-spacing: -0.02em;
    }

    .brand-subtitle {
      color: #81909c;
      font-size: 0.78rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }

    .header-status {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 14px;
      flex-wrap: wrap;

      font-size: 0.8rem;
      color: #9aa8b4;
    }

    .header-value {
      color: #dce4ea;
      font-weight: 750;
    }

    .system-strip {
      display: flex;
      align-items: center;
      gap: 8px;

      padding-right: 3px;

      font-size: 0.65rem;
      font-weight: 800;
      letter-spacing: 0.05em;
    }

    .system-item {
      display: flex;
      align-items: center;
      gap: 4px;

      color: #7f8d98;
      white-space: nowrap;
    }

    .system-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;

      background: #4a555e;
      box-shadow:
        0 0 0 1px #5f6b74;
    }

    .system-dot.ok {
      background: #58c98d;
      box-shadow:
        0 0 5px rgba(
          88,
          201,
          141,
          0.65
        );
    }

    .system-dot.bad {
      background: #db6c65;
      box-shadow:
        0 0 5px rgba(
          219,
          108,
          101,
          0.55
        );
    }

    .system-dot.unknown {
      background: #58636c;
      box-shadow: none;
    }

    .status-badge {
      border-radius: 999px;
      padding: 5px 11px;

      font-size: 0.76rem;
      font-weight: 850;
      letter-spacing: 0.08em;
    }

    .status-badge.live {
      background: #163c2c;
      color: #73dfa8;
    }

    .status-badge.waiting {
      background: #34383d;
      color: #bbc4cc;
    }

    .status-badge.stale {
      background: #4c3820;
      color: #ffc66d;
    }

    .status-badge.degraded {
      background: #4b2927;
      color: #ff9188;
    }

    .error-strip {
      display: none;
      position: absolute;
      left: 14px;
      right: 14px;
      top: 54px;

      border: 1px solid #75423e;
      background: #3b2523;
      color: #ffb0a8;

      border-radius: 7px;
      padding: 6px 10px;

      font-size: 0.75rem;
      z-index: 10;
    }


    /* ============================================
       Shared panels
       ============================================ */

    .panel {
      background: #171d23;
      border: 1px solid #2b343d;
      border-radius: 11px;
      min-width: 0;
    }

    .panel-title {
      color: #9eabb7;

      font-size: 0.72rem;
      font-weight: 850;
      letter-spacing: 0.09em;
      text-transform: uppercase;
    }


    /* ============================================
       Gauges
       ============================================ */

    .gauges {
      display: grid;
      grid-template-columns: repeat(6, 1fr);
      gap: 9px;
      min-width: 0;
    }

    .gauge-card {
      appearance: none;
      width: 100%;
      min-width: 0;

      border: 1px solid #2b343d;
      border-radius: 11px;
      background: #171d23;
      color: #e8edf2;

      padding: 8px 10px 7px;

      display: grid;
      grid-template-rows: auto 1fr auto;

      cursor: pointer;
      text-align: center;
    }

    .gauge-card:hover {
      border-color: #53616d;
      background: #1a2128;
    }

    .gauge-card:focus-visible {
      outline: 2px solid #6e9cca;
      outline-offset: 2px;
    }

    .gauge-title {
      color: #9eabb7;

      font-size: 0.7rem;
      font-weight: 850;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }

    .gauge {
      position: relative;
      height: 98px;
      margin-top: -3px;
    }

    .gauge svg {
      width: 100%;
      height: 100%;
      overflow: visible;
    }

    .gauge-track {
      fill: none;
      stroke: #27313a;
      stroke-width: 11;
      stroke-linecap: round;
    }

    .gauge-value-arc {
      fill: none;
      stroke: #649bd2;
      stroke-width: 11;
      stroke-linecap: round;

      stroke-dasharray: 0 100;
      transition: stroke-dasharray 0.25s ease;
    }

    .gauge-scale-tick {
      stroke: #7b8994;
      stroke-width: 1.2;
    }

    .gauge-scale-label {
      fill: #8d9aa5;
      font-size: 6px;
      font-weight: 650;
      font-family:
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;
    }

    .gauge-scale-label.pressure {
      font-size: 5px;
    }

    .gauge-scale-tick.major {
      stroke: #aab6c0;
      stroke-width: 1.7;
    }

    .gauge-number {
      position: absolute;
      left: 0;
      right: 0;
      bottom: 1px;

      font-size: 1.55rem;
      line-height: 1;
      font-weight: 850;
      letter-spacing: -0.04em;
    }

    .gauge-unit {
      margin-left: 3px;

      color: #9aa8b4;
      font-size: 0.7rem;
      font-weight: 650;
    }


    /* ============================================
       Middle instrument row
       ============================================ */

    .instruments {
      display: grid;
      grid-template-columns:
        1.25fr
        0.63fr
        0.63fr
        1.8fr;

      gap: 9px;
      min-height: 0;
    }

    .motion-panel,
    .orientation-panel,
    .wheel-panel,
    .fan-panel {
      padding: 10px 12px;
    }

    .motion-columns {
      height: calc(100% - 22px);

      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 15px;

      align-items: center;
    }

    .motion-group-title {
      color: #75838f;
      font-size: 0.67rem;
      font-weight: 800;
      letter-spacing: 0.07em;
      text-transform: uppercase;

      margin-bottom: 5px;
    }

    .axis-row {
      display: grid;
      grid-template-columns: 20px 1fr;
      align-items: baseline;
      gap: 7px;

      min-height: 31px;
    }

    .axis-label {
      color: #70808c;
      font-size: 0.72rem;
      font-weight: 850;
    }

    .axis-value {
      font-variant-numeric: tabular-nums;
      font-size: 1.03rem;
      font-weight: 720;
    }

    .axis-unit {
      color: #65737e;
      font-size: 0.62rem;
      margin-left: 3px;
    }

    .orientation-values {
      margin-top: 7px;
    }

    .orientation-panel .axis-row {
      min-height: 31px;
    }


    /* ============================================
       Wheel
       ============================================ */

    .wheel-panel {
      display: grid;
      grid-template-rows: auto 1fr auto;
      min-width: 0;
    }

    .wheel-center {
      display: flex;
      flex-direction: column;
      justify-content: center;
      gap: 7px;
      min-width: 0;
    }

    .wheel-primary {
      display: flex;
      align-items: baseline;
      justify-content: center;
      gap: 5px;
    }

    .wheel-rpm {
      font-size: 1.45rem;
      line-height: 1;
      font-weight: 850;
      font-variant-numeric: tabular-nums;
    }

    .wheel-rpm-unit {
      color: #74828d;
      font-size: 0.62rem;
      font-weight: 800;
    }

    .wheel-stats {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 4px 8px;
    }

    .wheel-stat {
      min-width: 0;
      text-align: center;
    }

    .wheel-stat-label {
      display: block;
      color: #65737e;
      font-size: 0.55rem;
      font-weight: 800;
      letter-spacing: 0.05em;
    }

    .wheel-stat-value {
      font-size: 0.76rem;
      font-weight: 800;
      font-variant-numeric: tabular-nums;
    }

    .wheel-stat-unit {
      color: #65737e;
      font-size: 0.52rem;
      margin-left: 2px;
    }

    .wheel-marker-row {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 5px;
      min-width: 0;
    }

    .wheel-lamp {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      flex: 0 0 auto;

      background: #273039;
      border: 2px solid #384550;
    }

    .wheel-lamp.clear {
      background: #457963;
      border-color: #63967f;
    }

    .wheel-lamp.marker {
      background: #ce645c;
      border-color: #f1877f;
    }

    .wheel-marker-text {
      font-size: 0.58rem;
      font-weight: 850;
      letter-spacing: 0.05em;
    }

    .wheel-sensor {
      color: #65737e;
      font-size: 0.51rem;
    }


    /* ============================================
       Fans
       ============================================ */

    .fan-panel {
      min-width: 0;
    }

    .fan-row {
      display: grid;
      grid-template-columns: 68px 1fr 45px;
      gap: 8px;
      align-items: center;

      margin-top: 10px;
    }

    .fan-name {
      color: #9ba8b4;

      font-size: 0.69rem;
      font-weight: 850;
      letter-spacing: 0.08em;
    }

    .fan-levels {
      display: grid;
      grid-template-columns: repeat(5, 1fr);
      gap: 5px;
      min-width: 0;
    }

    .fan-button {
      appearance: none;

      min-width: 0;
      padding: 7px 3px;

      border: 1px solid #35414c;
      border-radius: 7px;

      background: #1e262d;
      color: #b7c2cb;

      font-size: 0.68rem;
      font-weight: 800;

      cursor: pointer;
    }

    .fan-button:hover:not(:disabled) {
      background: #27323b;
      border-color: #657682;
    }

    .fan-button.actual {
      background: #184b37;
      border-color: #40976e;
      color: #d9f5e7;
    }

    .fan-button:disabled {
      opacity: 0.48;
      cursor: wait;
    }

    .fan-actual {
      text-align: right;

      font-size: 0.81rem;
      font-weight: 850;
      font-variant-numeric: tabular-nums;
    }

    .fan-bottom {
      margin-top: 12px;

      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }

    .control-status {
      color: #788792;
      font-size: 0.67rem;

      overflow: hidden;
      white-space: nowrap;
      text-overflow: ellipsis;
    }

    .control-status.confirmed {
      color: #69cf99;
    }

    .control-status.error {
      color: #ff8e85;
    }

    .all-off {
      appearance: none;

      border: 1px solid #8b4c47;
      border-radius: 7px;

      background: #492725;
      color: #ffc1bb;

      padding: 7px 14px;

      font-size: 0.69rem;
      font-weight: 850;

      cursor: pointer;
    }

    .all-off.actual {
      background: #592d29;
      border-color: #cf6a61;
    }


    /* ============================================
       Always-visible history matrix
       ============================================ */

    .history-panel {
      padding: 8px 10px 9px;
      min-height: 0;

      display: grid;
      grid-template-rows: auto 1fr;
      gap: 6px;
    }

    .history-toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }

    .history-ranges {
      display: flex;
      gap: 4px;
    }

    .range-button {
      appearance: none;

      border: 1px solid #35414b;
      border-radius: 6px;

      background: #1d252c;
      color: #85939e;

      padding: 4px 7px;

      font-size: 0.63rem;
      font-weight: 800;

      cursor: pointer;
    }

    .range-button.active {
      background: #184b37;
      border-color: #3c9069;
      color: #d9f5e7;
    }

    .mini-chart-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      grid-template-rows: repeat(4, minmax(0, 1fr));

      gap: 6px;

      min-height: 0;
    }

    .mini-chart {
      min-width: 0;
      min-height: 0;

      border: 1px solid #29333c;
      border-radius: 8px;

      background: #141a20;

      padding: 5px 7px 4px;

      display: grid;
      grid-template-rows: auto 1fr;
    }

    .mini-chart.selected {
      border-color: #54738a;
    }

    .mini-chart-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;

      min-height: 17px;
    }

    .mini-chart-title {
      color: #a4b0ba;

      font-size: 0.64rem;
      font-weight: 850;
      letter-spacing: 0.07em;
      text-transform: uppercase;
    }

    .mini-legend {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 7px;
      flex-wrap: wrap;

      color: #75848f;
      font-size: 0.56rem;
    }

    .mini-legend-item {
      display: flex;
      align-items: center;
      gap: 3px;
    }

    .mini-swatch {
      width: 10px;
      height: 2px;
      border-radius: 2px;
    }

    .mini-canvas {
      display: block;
      width: 100%;
      height: 100%;
      min-height: 60px;
    }


    /* ============================================
       Compact-height desktop
       ============================================ */

    @media (max-height: 830px) and (min-width: 1000px) {
      .page {
        grid-template-rows:
          auto
          125px
          172px
          minmax(205px, 1fr);
      }

      .gauge {
        height: 76px;
      }

      .gauge-number {
        font-size: 1.35rem;
      }

      .axis-row {
        min-height: 27px;
      }

      .fan-row {
        margin-top: 7px;
      }
    }


    /* ============================================
       Narrow/small screens fall back to scrolling
       ============================================ */

    @media (max-width: 999px),
           (max-height: 690px) {

      body {
        overflow: auto;
      }

      .page {
        height: auto;
        min-height: 100%;
        grid-template-rows: auto;
      }

      .gauges {
        grid-template-columns:
          repeat(2, minmax(0, 1fr));
      }

      .instruments {
        grid-template-columns:
          1fr;
      }

      .history-panel {
        min-height: 340px;
      }

      .history-toolbar {
        grid-template-columns: 1fr;
      }
    }


    /* SENSORTRAIN_V12_DUAL_WEB_V1 */

    .st-dual-strip {
      display: grid;
      gap: 5px;
      padding: 7px;
      border: 1px solid #384d42;
      border-radius: 9px;
      background: #102019;
      min-width: 0;
    }

    .st-dual-heading {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      font-size: 0.68rem;
      font-weight: 850;
      letter-spacing: 0.045em;
      color: #b5dac3;
    }

    .st-dual-result {
      color: #9ee6b3;
      text-align: right;
    }

    .st-dual-result.fault {
      color: #ffa5a5;
    }

    .st-dual-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 6px;
    }

    .st-dual-hub {
      min-width: 0;
      padding: 6px 7px;
      background: #192a22;
      border: 1px solid #304b3a;
      border-radius: 7px;
    }

    .st-dual-hub strong {
      display: block;
      color: #d8eade;
      font-size: 0.68rem;
      letter-spacing: 0.045em;
    }

    .st-dual-hub span {
      display: block;
      margin-top: 3px;
      font-size: 0.66rem;
      line-height: 1.35;
      color: #a9c3b1;
      overflow-wrap: anywhere;
    }

    /* SENSORTRAIN_MOTOR_UI_V1 */

    .train-control-stack {
      display: grid;
      gap: 7px;
      margin-top: 6px;
    }

    .motor-status-grid {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 5px;
    }

    .motor-pill {
      min-width: 0;
      padding: 4px 5px;

      border: 1px solid #35414c;
      border-radius: 6px;

      background: #20272e;
      color: #8997a2;

      text-align: center;
      font-size: 0.61rem;
      font-weight: 800;
      letter-spacing: 0.04em;

      white-space: nowrap;
    }

    .motor-pill::before {
      content: "";
      display: inline-block;

      width: 6px;
      height: 6px;

      margin-right: 5px;

      border-radius: 50%;
      background: #59636b;
    }

    .motor-pill.ok {
      color: #c8efda;
      border-color: #316e50;
      background: #173629;
    }

    .motor-pill.ok::before {
      background: #58c98d;
      box-shadow: 0 0 5px rgba(88,201,141,0.75);
    }

    .motor-pill.bad {
      color: #e2aaa6;
      border-color: #6d3c39;
      background: #352321;
    }

    .motor-pill.bad::before {
      background: #db6c65;
      box-shadow: 0 0 5px rgba(219,108,101,0.65);
    }


    /* SensorTrain dual-hub compact indicators */
    .st15-dual-pill {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 3px;
      white-space: nowrap;
    }
    .st15-dual-pill::before { display: none; }
    .st15-dual-label {
      flex: 0 1 auto;
      min-width: 0;
      margin-right: 6px;
    }
    .st15-dual-unit {
      display: inline-flex;
      align-items: center;
      gap: 2px;
      flex: 0 0 auto;
      font-size: inherit;
      font-weight: inherit;
      letter-spacing: inherit;
      color: #c8d1d9;
    }
    .st15-dual-unit::before {
      content: "";
      display: block;
      width: 5px;
      height: 5px;
      border-radius: 50%;
      background: #85929c;
    }
    .st15-dual-unit.st15-ok::before {
      background: #58c98d;
    }
    .st15-dual-unit.st15-warn::before {
      background: #edbf59;
    }
    .st15-dual-unit.st15-bad::before {
      background: #db6c65;
    }
    html[data-st15-theme="light"] .st15-dual-unit {
      color: #425f6e;
    }

    .motor-main {
      display: grid;
      grid-template-columns: 102px 1fr;
      gap: 7px;
      align-items: stretch;
    }

    .motor-readout {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;

      min-width: 0;

      border: 1px solid #33404a;
      border-radius: 7px;
      background: #11171c;
    }

    .motor-direction {
      color: #9aa8b4;
      font-size: 0.61rem;
      font-weight: 850;
      letter-spacing: 0.07em;
    }

    .motor-speed {
      margin-top: 1px;

      font-size: 1.18rem;
      line-height: 1;
      font-weight: 850;
      font-variant-numeric: tabular-nums;
    }

    .motor-buttons {
      display: grid;
      grid-template-columns: 1fr 1.15fr 1fr;
      gap: 5px;
    }

    .motor-button {
      appearance: none;

      border: 1px solid #41505c;
      border-radius: 7px;

      background: #202a32;
      color: #d6dee5;

      min-width: 0;
      padding: 7px 5px;

      cursor: pointer;

      font-size: 0.68rem;
      font-weight: 850;
      letter-spacing: 0.04em;
    }

    .motor-button:hover:not(:disabled) {
      background: #2b3741;
      border-color: #6b7f8e;
    }

    .motor-button.stop {
      border-color: #874943;
      background: #4c2926;
      color: #ffd0cb;

      font-size: 0.75rem;
    }

    .motor-button.stop:hover:not(:disabled) {
      background: #63332f;
      border-color: #b45c54;
    }

    .motor-button:disabled {
      opacity: 0.38;
      cursor: not-allowed;
    }

    .motor-command-status {
      min-height: 12px;

      color: #778590;
      font-size: 0.58rem;
      text-align: right;
    }

    .fan-compact {
      display: grid;
      grid-template-columns:
        46px 1fr 33px
        52px 1fr 33px
        66px;

      gap: 5px;
      align-items: center;
    }

    .fan-compact-name {
      color: #87949f;

      font-size: 0.61rem;
      font-weight: 850;
      letter-spacing: 0.04em;
    }

    .fan-compact-buttons {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 4px;
    }

    .fan-compact .fan-button {
      padding: 5px 3px;
      font-size: 0.61rem;
    }

    .fan-compact .fan-actual {
      font-size: 0.58rem;
      text-align: center;
      color: #8997a2;
    }

    .fan-compact .all-off {
      padding: 5px 3px;
      font-size: 0.58rem;
      white-space: nowrap;
    }

    .fan-command-status {
      min-height: 11px;

      color: #778590;
      font-size: 0.55rem;
      text-align: right;
    }


    /* SENSORTRAIN_UI_V2 */

    .motor-power-row {
      display: grid;
      grid-template-columns: 48px 1fr 42px;
      gap: 7px;
      align-items: center;

      padding: 1px 2px 0;
    }

    .motor-power-label {
      color: #84929d;
      font-size: 0.60rem;
      font-weight: 850;
      letter-spacing: 0.06em;
    }

    .motor-power-value {
      color: #d9e2e9;
      font-size: 0.68rem;
      font-weight: 850;
      text-align: right;
      font-variant-numeric: tabular-nums;
    }

    .motor-power-slider {
      width: 100%;
      cursor: pointer;
    }

    .fan-status-card {
      display: grid;
      grid-template-rows: auto 1fr;
    }

    .fan-light-area {
      height: 100%;

      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 18px;

      align-items: center;
      justify-items: center;

      padding: 15px 24px;
    }

    .fan-light-item {
      display: grid;
      justify-items: center;
      gap: 8px;
    }

    .fan-light-label {
      color: #8e9ba6;
      font-size: 0.66rem;
      font-weight: 850;
      letter-spacing: 0.08em;
    }

    .fan-light {
      width: 30px;
      height: 30px;

      border-radius: 50%;

      background: #b74b45;
      border: 2px solid #e17770;

      box-shadow:
        0 0 10px rgba(225,119,112,0.55);

      transition:
        background 0.20s ease,
        border-color 0.20s ease,
        box-shadow 0.20s ease;
    }

    .fan-light.on {
      background: #40ad72;
      border-color: #70d69b;

      box-shadow:
        0 0 12px rgba(88,201,141,0.75);
    }

    .fan-light-state {
      color: #b8c3cc;
      font-size: 0.70rem;
      font-weight: 850;
    }



    /* SENSORTRAIN_FAN_GRAPHICS_V1 */

    .fan-light-area {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      align-items: center;
      height: calc(100% - 26px);
    }

    .fan-light-item {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      min-width: 0;
    }

    .fan-light-label {
      color: #75838f;
      font-size: 0.62rem;
      font-weight: 800;
      letter-spacing: 0.07em;
      margin-bottom: 4px;
    }

    .fan-graphic {
      width: 72px;
      height: 72px;
      display: block;
    }

    .fan-housing {
      fill: #151d22;
      stroke: #47555e;
      stroke-width: 5;
    }

    .fan-inner-ring {
      fill: none;
      stroke: #2d3940;
      stroke-width: 2;
    }

    .fan-blade {
      fill: #91a2ab;
    }

    .fan-hub {
      fill: #3a474e;
      stroke: #83939b;
      stroke-width: 2;
    }

    .fan-rotor {
      transform-origin: 50px 50px;
      transform-box: view-box;
    }

    .fan-rotor.spinning {
      animation-name: sensortrainFanSpin;
      animation-timing-function: linear;
      animation-iteration-count: infinite;
    }

    @keyframes sensortrainFanSpin {
      from {
        transform: rotate(0deg);
      }

      to {
        transform: rotate(360deg);
      }
    }

    .fan-light-state {
      margin-top: 4px;
      color: #75838f;
      font-size: 0.68rem;
      font-weight: 800;
      font-variant-numeric: tabular-nums;
    }

    .fan-light-state.running {
      color: #63d69d;
    }

</style>

  <style>
    /* SENSORTRAIN_HUB_BATTERY_UI_V1 */

    .st-hub-battery {
      display: inline-flex;
      align-items: center;
      gap: 7px;

      min-height: 28px;
      padding: 5px 9px;

      border: 1px solid #46535e;
      border-radius: 999px;

      background: #151c22;
      color: #b9c4cc;

      font-size: 0.68rem;
      font-weight: 800;
      letter-spacing: 0.045em;

      white-space: nowrap;
    }

    .st-hub-battery.floating {
      position: fixed;
      top: 48px;
      right: 14px;
      z-index: 9999;

      box-shadow: 0 4px 18px
        rgba(0, 0, 0, 0.38);
    }

    .st-battery-shell {
      position: relative;

      width: 27px;
      height: 14px;

      border: 2px solid #687680;
      border-radius: 3px;

      padding: 2px;

      box-sizing: border-box;
    }

    .st-battery-shell::after {
      content: "";

      position: absolute;
      top: 3px;
      right: -5px;

      width: 3px;
      height: 6px;

      border-radius: 0 2px 2px 0;
      background: #687680;
    }

    .st-battery-fill {
      display: block;

      width: 0%;
      height: 100%;

      border-radius: 1px;
      background: #687680;

      transition:
        width 0.25s ease,
        background 0.25s ease;
    }

    .st-hub-battery.full
      .st-battery-fill,
    .st-hub-battery.good
      .st-battery-fill {
      background: #48c98a;
    }

    .st-hub-battery.low
      .st-battery-fill {
      background: #e4aa42;
    }

    .st-hub-battery.critical
      .st-battery-fill {
      background: #e16060;
    }

    .st-hub-battery-value {
      min-width: 34px;

      color: #f0f4f7;
      text-align: right;

      font-size: 0.77rem;
      font-variant-numeric: tabular-nums;
    }
  </style>


  <style>
    /* SENSORTRAIN_BRANDING_V1 */

    .sensortrain-brand {
      display: flex;
      align-items: center;
      gap: 16px;
      min-width: 0;
      flex: 0 1 auto;
    }

    .sensortrain-brand-logo {
      display: block;
      width: auto;
      height: 46px;
      max-width: none;
      flex: 0 0 auto;
      mix-blend-mode: screen;
    }

    .sensortrain-brand-subtitle {
      color: #8795a1;
      font-size: 0.72rem;
      font-weight: 650;
      letter-spacing: 0.14em;
      line-height: 1.15;
      white-space: nowrap;
      text-transform: uppercase;
      flex: 0 0 auto;
    }

    @media (max-width: 1250px) {
      .sensortrain-brand {
        gap: 12px;
      }

      .sensortrain-brand-logo {
        height: 42px;
      }

      .sensortrain-brand-subtitle {
        font-size: 0.64rem;
        letter-spacing: 0.10em;
      }
    }


    /* SENSORTRAIN_V12_UNIFIED_MOTION_V1 */

    @media (min-width: 1000px) {

      .instruments {
        grid-template-columns:
          minmax(0, 1.86fr)
          minmax(0, 0.63fr)
          minmax(0, 1.80fr);
      }

    }

    .combined-motion-panel {
      min-width: 0;
    }

    .combined-motion-grid {
      display: grid;
      grid-template-columns:
        minmax(0, 1.12fr)
        minmax(0, 1.12fr)
        minmax(0, 0.9fr);

      align-items: start;
      gap: 14px;
      padding-top: 22px;
    }

    .combined-motion-grid > div {
      min-width: 0;
    }

    .combined-motion-grid > div:not(:first-child) {
      border-left: 1px solid #303c45;
      padding-left: 12px;
    }

    .combined-motion-grid .motion-group-title {
      font-size: 0.64rem;
      margin-bottom: 10px;
      white-space: nowrap;
    }

    .combined-motion-grid .axis-row {
      display: grid;
      grid-template-columns: 15px minmax(0, 1fr);
      gap: 5px;
      min-height: 31px;
    }

    .combined-motion-grid .axis-value {
      font-size: 0.92rem;
      white-space: nowrap;
    }

    .combined-motion-grid .axis-unit {
      font-size: 0.55rem;
      margin-left: 2px;
    }

    .combined-motion-grid .orientation-values {
      margin-top: 0;
    }

    @media (max-width: 560px) {

      .combined-motion-grid {
        grid-template-columns:
          repeat(2, minmax(0, 1fr));
        gap: 14px;
        padding-top: 14px;
      }

      .combined-motion-grid
      .combined-motion-orientation {
        grid-column: 1 / -1;
        border-left: 0;
        padding-left: 0;
        border-top: 1px solid #303c45;
        padding-top: 10px;
      }

    }

    /* SENSORTRAIN_V12_COMPACT_DASHBOARD_V2 */

    /* Compact controls for the four-column desktop row. */
    @media (min-width: 1000px) {

      .fan-panel {
        padding: 8px 10px;
      }

      .train-control-stack {
        gap: 4px;
        margin-top: 4px;
      }

      .st-dual-strip {
        gap: 4px;
        padding: 5px 6px;
      }

      .st-dual-hub {
        padding: 4px 6px;
      }

      .motor-button {
        padding: 5px 5px;
      }

      .fan-compact .fan-button {
        padding: 4px 3px;
      }

      .fan-compact .all-off {
        padding: 4px 3px;
      }

      .mini-canvas {
        min-height: 0;
      }

      .mini-chart-grid {
        grid-template-rows:
          repeat(4, minmax(0, 1fr));
      }

      .mini-chart {
        min-height: 0;
        overflow: hidden;
      }
    }

    /* SENSORTRAIN_V12_FAN_STATUS_FIT_V1 */

    @media (min-width: 1000px) {
      .fan-status-card {
        grid-template-rows: auto minmax(0, 1fr);
      }

      .fan-status-card .fan-light-area {
        height: 100%;
        min-height: 0;
        padding: 0 8px;
        gap: 8px;
      }

      .fan-status-card .fan-light-item {
        gap: 0;
        min-height: 0;
      }

      .fan-status-card .fan-light-label {
        margin-bottom: 1px;
        font-size: 0.57rem;
        line-height: 1;
      }

      .fan-status-card .fan-graphic {
        width: 46px;
        height: 46px;
      }

      .fan-status-card .fan-light-state {
        margin-top: 1px;
        font-size: 0.57rem;
        line-height: 1;
      }
    }

    /* Full desktop: the eight history charts fit on-screen. */
    @media (min-width: 1000px) and (min-height: 820px) {

      body {
        overflow: hidden;
      }

      .page {
        height: 100vh;
        min-height: 0;

        grid-template-rows:
          auto
          122px
          250px
          minmax(0, 1fr);
      }

      .instruments {
        height: auto;
        min-height: 0;
      }

      .history-panel {
        min-height: 0;
        overflow: hidden;
      }
    }

    /* Short desktop: controlled scrolling, compact charts. */
    @media (min-width: 1000px) and (max-height: 819px) {

      body {
        overflow-x: hidden;
        overflow-y: auto;
      }

      .page {
        height: auto;
        min-height: 100vh;

        grid-template-rows:
          auto
          122px
          minmax(250px, max-content)
          430px;
      }

      .instruments {
        height: auto;
        min-height: 250px;
      }

      .history-panel {
        height: 430px;
        min-height: 0;
        overflow: hidden;
      }
    }

</style>

<!-- SENSORTRAIN_15A1_DASHBOARD_STYLE -->
<style>
  /* 1.5A-1: display-only dashboard. The existing status nodes retain their IDs
     so deployed polling/telemetry handlers continue to update them. */
  body.st15-dashboard .fan-panel .motor-buttons,
  body.st15-dashboard .fan-panel .motor-power-row,
  body.st15-dashboard .fan-panel .motor-command-status,
  body.st15-dashboard .fan-panel .fan-compact,
  body.st15-dashboard .fan-panel .fan-command-status,
  body.st15-dashboard #st-layout-headlight-row,
  body.st15-dashboard #st-headlight-controls {
    display: none !important;
  }
  body.st15-dashboard .fan-panel {padding: 10px 12px;}
  body.st15-dashboard .fan-panel .train-control-stack {
    gap: 5px;
    margin-top: 6px;
  }
  body.st15-dashboard .fan-panel .motor-main {
    display: flex;
    min-height: 27px;
    margin: 0;
  }
  body.st15-dashboard .fan-panel .motor-readout {
    width: 100%;
    flex-direction: row;
    gap: 10px;
    padding: 3px 8px;
    min-height: 25px;
  }
  body.st15-dashboard .fan-panel .motor-speed {margin-top:0;font-size: .89rem;}
  body.st15-dashboard .fan-panel .st-dual-strip {padding:5px 7px;gap:4px;}
  body.st15-dashboard .fan-panel .st-dual-hub {padding:4px 7px;}
  body.st15-dashboard .fan-panel .st-dual-heading {font-size:.63rem;}
  body.st15-dashboard .st15-sensor-grid {
    display:grid;
    grid-template-columns:repeat(2,minmax(0,1fr));
    gap:7px;
    margin-top:8px;
  }
  body.st15-dashboard .st15-sensor-card {
    padding: 9px 10px;
    border: 1px dashed #556273;
    border-radius: 8px;
    background: #1b252d;
    min-width: 0;
  }
  body.st15-dashboard .st15-sensor-heading {
    display:flex; justify-content:space-between; align-items:center;
    gap:8px;color:#d6e3ed; font-size:.73rem; font-weight:850;
    letter-spacing:.025em;
  }
  body.st15-dashboard .st15-sensor-badge {
    font-size: .61rem; color:#aebfca; font-weight:650; letter-spacing:0;
  }
  body.st15-dashboard .st15-sensor-values {
    margin:6px 0 3px;display:flex;flex-wrap:wrap;gap:8px;
    color:#a9b6c1;font-size:.69rem;
  }
  body.st15-dashboard .st15-sensor-note {
    color:#91a1ac;font-size:.62rem;
  }
  body.st15-dashboard .st15-toolbar {display:flex;align-items:center;gap:8px;}
  body.st15-dashboard .st15-nav {display:flex;gap:6px;align-items:center;}
  body.st15-dashboard .st15-nav a,
  body.st15-dashboard .st15-theme-toggle {
    border:1px solid #3a4651; border-radius:7px; color:#b9c9d5;
    background:#222b33;padding:6px 9px;text-decoration:none;
    font-size:.7rem;font-weight:800;white-space:nowrap;
  }
  body.st15-dashboard .st15-nav a[aria-current="page"] {
    background:#193d2d;border-color:#387b57;color:#a8ecc4;
  }
  body.st15-dashboard .st15-theme-toggle {cursor:pointer;}
  body.st15-dashboard .st15-nav a:focus-visible,
  body.st15-dashboard .st15-theme-toggle:focus-visible {
    outline:2px solid #64c88d;outline-offset:3px;
  }
  body.st15-dashboard .header-status {gap:9px;}
  body.st15-dashboard .header {gap:8px;}
  @media (min-width:1120px) {
    body.st15-dashboard .page:has(.st47-middle) {
      grid-template-rows: auto 122px 282px minmax(245px,1fr)!important;
    }
    body.st15-dashboard .st47-middle {
      grid-template-columns:minmax(0,45fr) minmax(0,55fr)!important;
    }
    body.st15-dashboard .st47-control {overflow:hidden!important;}
  }
  @media (max-width:1119px) {
    body.st15-dashboard {overflow:auto;}
    body.st15-dashboard .page {
      height:auto; min-height:100vh;
      grid-template-rows:auto!important;
    }
    body.st15-dashboard .instruments {
      grid-template-columns:1fr!important;
      height:auto;
    }
    body.st15-dashboard .st47-control,
    body.st15-dashboard .st47-motion {overflow:visible!important;}
    body.st15-dashboard .st47-travel-source {display:block!important;}
    body.st15-dashboard #st47-travel-strip {display:none!important;}
    body.st15-dashboard .st15-toolbar {flex-wrap:wrap;justify-content:flex-end;}
  }
  @media(max-width:710px) {
    body.st15-dashboard .header {flex-wrap:wrap;}
    body.st15-dashboard .st15-sensor-grid {grid-template-columns:1fr;}
    body.st15-dashboard .st15-nav {gap:4px;}
  }
  /* Light theme: deliberate surface, typography and chart colors, not inversion. */
  html[data-st15-theme="light"] { color-scheme:light; }
  html[data-st15-theme="light"] body.st15-dashboard {
    background:#edf2f6!important; color:#192a37!important;
  }
  html[data-st15-theme="light"] .panel,
  html[data-st15-theme="light"] .gauge-card,
  html[data-st15-theme="light"] .mini-chart-card,
  html[data-st15-theme="light"] .history-panel {
    background:#fff!important; border-color:#cbd6e0!important;
    color:#1c2e3c!important;
  }
  html[data-st15-theme="light"] .panel-title,
  html[data-st15-theme="light"] .mini-chart-title,
  html[data-st15-theme="light"] .motion-group-title,
  html[data-st15-theme="light"] .wheel-stat-label,
  html[data-st15-theme="light"] .gauge-card .gauge-title,
  html[data-st15-theme="light"] .header-status,
  html[data-st15-theme="light"] .axis-label,
  html[data-st15-theme="light"] .wheel-rpm-unit,
  html[data-st15-theme="light"] .wheel-stat-unit,
  html[data-st15-theme="light"] .st47-metric small {
    color:#526679!important;
  }
  html[data-st15-theme="light"] .header-value,
  html[data-st15-theme="light"] .axis-value,
  html[data-st15-theme="light"] .wheel-rpm,
  html[data-st15-theme="light"] .wheel-stat-value,
  html[data-st15-theme="light"] .gauge-number,
  html[data-st15-theme="light"] .st47-metric strong {
    color:#172c39!important;
  }
  html[data-st15-theme="light"] .gauge-track {stroke:#dce5ec!important;}
  html[data-st15-theme="light"] .gauge-scale-label {fill:#526477!important;}
  html[data-st15-theme="light"] .gauge-scale-tick {stroke:#718599!important;}
  html[data-st15-theme="light"] .gauge-unit {color:#556679!important;}
  html[data-st15-theme="light"] .st-dual-strip {
    background:#ecf7ef!important;border-color:#b5d9c3!important;
  }
  html[data-st15-theme="light"] .st-dual-hub {
    background:#f8fffa!important;border-color:#badcc5!important;
  }
  html[data-st15-theme="light"] .st-dual-hub strong,
  html[data-st15-theme="light"] .st-dual-heading,
  html[data-st15-theme="light"] .st-dual-result {color:#22563a!important;}
  html[data-st15-theme="light"] .st-dual-hub span {color:#325842!important;}
  html[data-st15-theme="light"] .motor-pill {
    background:#eef3f7!important;color:#526376!important;border-color:#c1cfd9!important;
  }
  html[data-st15-theme="light"] .motor-pill.ok {
    background:#dff3e6!important;color:#286342!important;border-color:#a3d1b4!important;
  }
  html[data-st15-theme="light"] .motor-pill.bad {
    background:#fbe8e7!important;color:#923a38!important;border-color:#e1aba8!important;
  }
  html[data-st15-theme="light"] .motor-readout {
    background:#f3f7fa!important;border-color:#cbd8e1!important;
  }
  html[data-st15-theme="light"] .st15-sensor-card {
    background:#f7fafc!important;border-color:#9dafbe!important;
  }
  html[data-st15-theme="light"] .st15-sensor-heading {color:#244156!important;}
  html[data-st15-theme="light"] .st15-sensor-badge,
  html[data-st15-theme="light"] .st15-sensor-values,
  html[data-st15-theme="light"] .st15-sensor-note {color:#526779!important;}
  html[data-st15-theme="light"] .st15-theme-toggle,
  html[data-st15-theme="light"] .st15-nav a {
    background:#fff!important;color:#2d485a!important;border-color:#afc0ce!important;
  }
  html[data-st15-theme="light"] .st15-nav a[aria-current="page"] {
    background:#dff5e7!important;color:#245f41!important;border-color:#9cd4b1!important;
  }
  html[data-st15-theme="light"] .st47-travel-strip,
  html[data-st15-theme="light"] #st47-travel-strip {
    border-color:#cbd8e1!important;
  }
  html[data-st15-theme="light"] .mini-chart {
    background:#f9fbfd!important;border-color:#cbd8e1!important;
  }
  html[data-st15-theme="light"] .range-button {
    background:#fff!important;border-color:#b6c5d0!important;color:#314e60!important;
  }
  html[data-st15-theme="light"] .range-button.active {
    background:#def4e7!important;border-color:#7cbd97!important;color:#21583c!important;
  }
  html[data-st15-theme="light"] .mini-legend,
  html[data-st15-theme="light"] .mini-legend-item,
  html[data-st15-theme="light"] .history-toolbar,
  html[data-st15-theme="light"] .wheel-marker-text,
  html[data-st15-theme="light"] .wheel-sensor {
    color:#496074!important;
  }
  /* SENSORTRAIN_15A2_THEME_LOGO: Use provided light logo in light theme. */
  body.st15-dashboard .sensortrain-brand-logo.st15-logo-light {
    display:none;
  }
  html[data-st15-theme="light"] body.st15-dashboard .sensortrain-brand-logo.st15-logo-dark {
    display:none;
  }
  html[data-st15-theme="light"] body.st15-dashboard .sensortrain-brand-logo.st15-logo-light {
    display:block;
    background:transparent!important;
    padding:0!important;
    border-radius:0!important;
    mix-blend-mode:multiply!important;
  }
  html[data-st15-theme="light"] .mini-swatch {filter:saturate(1.3);}
  html[data-st15-theme="light"] .brand-subtitle,
  html[data-st15-theme="light"] .sensortrain-brand-subtitle {color:#496075!important;}
</style>
</head>

<body>

  <div class="page">

    <header class="header">


      <div class="brand sensortrain-brand">
        <img
          class="sensortrain-brand-logo st15-logo-dark"
          src="data:image/webp;base64,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"
          alt="SensorTrain">
        <img
          class="sensortrain-brand-logo st15-logo-light"
          src="data:image/webp;base64,UklGRpQ6AABXRUJQVlA4IIg6AACwpQCdASpYAn0APi0UiEKhoSEVC11wGALEsQOGOmo2pCv81+Wn5FfLlZH7b/eP0l+9n+Z6Fqw/LR5Y/zn+P/cz/Bf//6Z/7f/p/4P9cvlP+d/+d7gH6Z/3/+4ftv/hP//9iv9L+3nvP/bj8jvgH/NP6//sv8T+7fy//5v/Tf5X3R/2v/T/773Av6r/Y/v0+MX2Gv3E9gX+n/3f/w+zp/uf/P/qP31+jf+of6D/wf5X9+PoP/nH9m/5355/IB6AH/n9QD99fePWP/f/yA/df1d/Efnf7R/cv8P/kf77/6/9r7Ufl66j/4HoR/Iftb+A/vX7Rf4n9wfm7/e+F/xz/u/7x+QnyEfiP8t/t39j/Zz++/uZ85cRrlH87/yvUL9dPpH+K/vn7Zf5j90fce/qPyx95fsx/sPuP+wH+W/0v/F/3r91v7r//+jD9R9gT+d/2P/ef4T8ufpY/mv+H/kv8//5/9J///gH+Zf4P/i/4z/W/tR9g/8k/o/+j/uH+k/7n+W////W+9P2N/tp7Jv7Df/k1jYW+/fMdHhXBOFWCZoQhE9U3AoOPpQdnLD6Q+BH3LMnQKrsTl7o/QXWpTUf8frH9A+hrPlMM3OK4SR6qVXq2bkEe4gfYalbT7A8Vixt2V1oOWUpcneK/wOv//lm/74svkeVzEMrvnxFMrQEqMjdoVYp3gZ0BxGVDuVVf8vDNbV7C58jtkYRFWLhxyfFQ56UQutw2lYrC1ywoDPCp+GOH3A+OP2IxOYtEJW7sf9kpZcbcjLkw6ZKrzjZEkdx8fluEUcz2cB+ypysLZ/0fHwWxyvZHewdTgLVSV812tViKjlVHHtT66waE++JqYbSt9nfcxsaGlJ4ilWPRAS0ZOqNrNo4bZ4AsBtIwLwCu7/xlDnDl/j4y8SgkQHtXb2pbkHjP2NpyoFbSLfzgCf2oY8R/HhKTkYNeyONAA5wG/5qsiYUdsxfqdIZQ+Xd2OtzG20oMRclVMIuxEYI5AJ34f+qoK8FEAb5cp2BYoIyaHcjJJccHxZvvg/TUHNX4N/JRuAeZVXtzGv1LW/YNi/130HXb+87Xmkx6o3fAQoj8jof+b3XFavwCfwwazY7Cd4byisRdXizXK3WEPXZDSOTfoLiqSgK7ZijbP7g8xY6+VmJtZ3EEDJkSMqCPGH2uevWsAqSoOswBlNVO1OQQsx+SYNl1BmAlSbQY2yBj1/VrdrOfpcs8BR8qC4hEYSMHdIbpbXD+9ZMEiwGj8kFGdWKgjAG8YsP9PrLHxXOyiEnaRA9b4Z1r74jqy/texCjKR6rQT7Nw3Za5Pv3ywP+T/EJYxrKcZtgyDzDH4xERcmmzY3JMVSKGN0URwqU02AeoJ4XZb/Uga79d0qujUZcRiRu/BGqfxW3FlspRR/+mV9PvBoHkSaR0O2j1koaWYyNu1AW9eNzjvKqIuwYIUWMV1EQaVIRPYZxz2/+TEwqYC9mW793SlUpWvTMbJVYnZI5sPv5dSTvez/Rz1fdGNcJJGG0u8ujYes8u1BgBR/X3FtpJj55RjYvqkME2FyZ+1LrSQGlnAomc+KAv+Q963hI6hDCBuIFKtEGwCBkdrJHidmVbl9tF1zuoSYvHi0WaN3/a3gCcCqm0lXNdQgjeEMMdI+/J5Zv8/VSXQQlblxv6g4rkRhrXHdtWY1xUJgz0ov3Neqn1fqWMjpTPIqzGPBkIAj60mzE5QCeqip7Ig6r1NXPSUvko/+5x1Laf0nY0dSaClHzBTXzuHsJVqMXDaVvs9KEBP19obk4RrzoAAD+//+Wi6ZMiDH94T1of8iLfV52xkxeVpGO8ay5PeHB5Zt35YHDKkAo+WY1SpSa2KggFi9Ivfe4UhVGh4GCUaZq0LmhTR9yQI29ei9PSuNdZVf7BxhLDVSyaYzuVDNqa9djmUH8FeO7Fc6Xp3bhDqKJq4VlhqBh38Kzpt9s+dzjAbOXbMqz6aSFle+1jqSuX/xtliTQon7stAf9hpaL8mHlX1S6J4zoY/dq+NXNNFfbyX7OkmzERrLHujtL1nb7JPXXLLbd+gYt9bJNHU37qNraQulazPYddC3LBZc803DugyX4Hp5J1wXDSQalQFkbQVNRyIULH3fPG2fieLD1TfjXVu7yO5RIUz79JUW7U2Dv2gvgptGDDTX88aD+ATD858nP7mujoitlLKdDP3W037KEoL2vyRXXJCsvx0wDhaSYvs5sefXKGh2AjftZAjjYq++SMCw8qAwPw7mue/INQWBeosXHW4dahLvzzF4bw/ODUOQiMa+8HCCt/fsNpWUvvJjp8FF1kmNbgOuBPeBLLS90rCC84MFJ3HR0akpCYScqakZ6LVqWqw3Zy2ZR6VVy8ev5nyrOrKnyHWdIHJIMp/15XsKQJwVOZY65Kxp1utiJR7aFNKlSRvJ5A1Y+rYIi64uOl2ouMUHR45LDY+YuICqYR+ec++hJa+brvuNDmvrdNPEUkR2PBV2HicZd51dMkKX7/2NtRcL1O1shwOJb84Pnjx6MqZwvtKJRcNSFdzUhujldvZZzHqeenmr8CddlzE/FjyEPthJUYRTIcezWhxa5NZyc7dVRPoElFk7VQQTFuCSd28F0KHHRYPmFQrD8YgcWcWyf0rVzL49UzVgMR+v+3ClBpdywcX7eP/2OsLtI+kNl7nn+zERmkjKbMfTLXH47UP3mIOpMlw434kkYHday1HYF1plXSYJ9qt+5bSkS3MW/rprPeXiDrkd3X7W6AzlSF68u0tMBX0N1bdZ9c25Zc8L4bjoKL4scer+QirauTdn2hB57fA80UNpnZ2AQ7oTtiLcsNYozfFufd2dyO5W+rOX+HdDZf1m2BZjmfZZd142VTj/PpvzhR+P3Vx5WH3tpQIJoKUHcpFm1KhwdXJClX45ictfpXC3SCqagqvyDtBiWjtXEKtaAWwpTrmawJPFailtMjqHIcf3fkY6Q84ZTzHd20KGbV00CwRk5DUAo0id19aPI2ETCkFx5Jw76iXraM0gMGAe8JcgFc+gyNkO6F/u+l6b4qAi4qc0Q671BcLo6vMNGFXHMUxMEHXnfvMi1KwEnXsOHEEQQF3zzu2cGBRptOlWhrdJ+rcqPcX/+6tYNQypaF2EuXXQA/bylJRgbXN3Q/kS5mLqYKCiUTrsnRJOLHehq7LZ4qSGl8eD917ZSiJVumLa/XUYzrmCJSl8pqKzL510YeeHxs3QjwZ/SvwRPKWQvqL5q/wtE3cMfiGt+0sFO7hnRCuFPuwxKJm+f5iCRxsT11LvU6O8tDIqPyXxBR1vbM65EtZ3Io7Ds8obYIDvMqO6dxSPiRhuHn7QMup/1xeLmvElMZGQi5zEk+cLeVUClWWWFOhuca4Bx8x3/9+2T0Umc8DMAhJ0OKXgEb0aOU6rcP3URNjbL3jHePrAmkEyV03geZS/uYc3SthAFJdLlKTKuXEaIvTbmW4fGj4iJ4J4jweZtig72/+7oEe8NMNcgYBLgZuMB+6CxLQoz1zBcSruJUsDdifHZmQhdFv6X4nRxUc9Sq5k13/wc2P7DAP1TWfeX5e0Td++akUBUbd10b2ydB09qBxacri8mGYs4J1SuBaEeL0aFkp/gV4/s//geYMFmKXOUIwYz/aofNei6cPSHxRU9o4r3BFEVjQQ6fnyH5vv9vie682fxwNfvD9STyBArit8CT2o5dOKCb5Kg/YD/BrEU+1O6+jWcg+oVQuszz01rkj9jTuVdg6jCTNaKLYYS8z+eckIPx5g3QX3H07dywlvGevIXH8MXqve/9KrSecWaWlj1ktUdjLoC74kr2MzniQUywPR84i0mJ7xgigS2Dl3HYxh/0j7I/JzDS7crlEMNfAtxLe6VGlAIhMNWm+z1Jk4uFj+lCRCIi8sxD+Ndu7jn3hmCPLD814ReW+6bAFwnU71bGYISU2TlNKbbVmLez0WwyNbBf6btBBZsXvbgG7Y6a+20HszYquqWNNGyzuuXN9eTGMaHZ3Q6GhDEDIlAWzVXd0cLZqmrkZJWVkW4a80BZY69YKmYNSqjbE3dFtrLmrzxo/v5IDvdPl1zfXzlyMLiBq20KMgrAfxzQnkoQWoMIuv+k46tj4Wj0Fixx7+zF+8IYc2Ncsh7kGOF8kRPX3Vs8/59dQF+xmp+AGnL9nje4a3G7TR5IkBfDcAhni2jqf8kY6whwnH2tVTDW/m6CT9Fhq94m48004H/0rQA3gLhoPYrZUPAuvuYXrAAKYI2OwHuVSF5NQXQV7nuWYbaoHE0Zh5lMOoWmVV2VADNZ3fqLN2qmGUDikz0eYkvaKS8dmPvz9y6sMbuDiuVMdVwXzUQgUH930Sv+IXAlIEmcGnYySb/u2YrssOfvMHbwRFE9Pos2svtXoDi/cHtBc+Ka6KVxnO8PVrNzp9xPzN3dHkORRvoXpdkUXO9a4UixaaXgwdUJRFHNri8sgYl81NqZFASmPRbWkDDAy+aPP4o07SrDxVZhRfHBJ1yVOpozVl1BfhQmNkCq0laxilQo+skxttlNVTO7UD940hIhuAkyBIr10RXH0JNIYwm2RoFeOitkofEJ5DhmrACi7vkcLfhL3tPBSWjCEOnbWpempI/NuVSTsWHoHD4Kl/OPvjqW4YdjZnekCrv/yZlmLdBCp67Aa+8rQvbR1bBgjWbUQA2gzsUcva9KGfQxFobHW30RyiDsXJfljTJEuQW2OdEkxVjvJaR0shDOdDxQsMTbYSsARRkTVALwR+9AnJfp4ib0pql7uQTDg1DFw3pqobOFRnpXpnZTsneiZZMitLbCk+iBdfA+vn9Fs/U147nXo9EXeLBxN3p0U5//0Md8wqqLIgM1EYCsE4JDMbHXC+/qz1iNqg5Fa+bpFMn1j87VK0IRIRuT3S7wwJtM9aS97L8TB/If2KQz800RuuSgN+TMKs5apq99giUNz/EznU/3PqEb49OqwHSR8cB5gODINTJrS1xu+S/n2fkczgQKGd+eeQtEDeHbq/Vy4TQr4rm/sNDGa326/LQbC9PXZzMweYBd6wqgQQZ/+1u6w0QAHzjBCjj3BmvHbwQTuZTw9CZwEAqJa89YdlagJXh2SNTqMFZkdqOeeVrmPQMFlCEO1tREooudVHcGZ7Yu5OUh+vWKAoqx72pOCOIxftJ7bwO7ohBX2l+59ldqofKfUCihKTmGAbftIeR3JjX64+ddyRhjRLMDQkWEuJhVshsLR2uZznk8KRzq+tZSn+qMcIzHq5qgJUf/8UEpu28A6uOMgS76gOru12ydSgwfmwHePovgS/o+UrPgkvjVK9wd9RGTUqOk/m9hV7nZggig5DyVFZ44QBAsgTuuqIPWfom3yVS6H5l8xQOGGz76dIKdPeo/3a57w2H3nlxKN8uHKp71Omok6estk6tg20kkriTdpayNZPmla1JJbCd0l1Z4HGBEjhzwnH6pW7wgzDsNjvJgvPEK68d6m2CCxne8nUgZVqWJu7tirAmL8pMCAW4E4ExohnXeCkRsuQ4cwo89/IYgOopR2NWdMwCnyeBohEH4Ufk9WQp37Aa1P4ZTorGF0qpZKPEaf9HB1nIQW7cgMhZxqkl36L68wYWdgWd3EaO3O+xJOkV38WGmTAXwlqKkVWPaSz8cxe+igcZx59qX3lCzdHx1wGGWikOfUZahWjWzO1nVkxYh/OtmmBhAfZ+/0XsrmS755HnniDZzf17ZYplpyVxaJddkLlYOXKB+oGnPEplbhN0RcRL17oY7hNw/kZWn4KQKBQL9CzGdCb2slK0IZsO0HMupxyh7Q7XgZjCNa8ctQPUI281QrhzSYIZLmQ/QAT2yDfkHICK1T3B+Umi78yZp+5PcTxrPXqFgrkwdkDojRd/ngaYSwJBenzcaYhrRCmyd02GsNptMmYv4oq92DAn9n/bK6BVclI5QzTbu6fy1tlr/yGj0zkLwXmNq5GcAzxk2MZGwUSzhFJpzxTDHlqDRpsL9jMVZD5yccKIo5sQr9rnkKdX+d95cS9sCsAOV1GcEziJdZmnR+w5LvghChv8hgaLPgUN1Xoszj+7P8P5xxQf8t1rK+TiBnngfC9Qir8ieW9sVOdNM++L/RJp07ldjbbk1ZfnfhMBEkifwJUDTR9zQsymMNEYBcz2jTF19GHIqXqH0k6iTuHQu3A2lShMFadu4yQrmyobUSjH2eA70bCOZrJychKOwulmEaif2y1Xx8sAVVuj1zrtjnfP2nQmBFHokKf+d1CHokXSaYdWne+iJ9lkTRBrlWiEjRRaZipJpDcEZIe0cMlrIa28eXpZ2SH+s0MS2K7/XLAP/96sWDmuX+mUvJ2RFufNKv+9WXYWUKOjNp26Y0GLVKH+xLE7/EWZ0zng3wgN2qJWMEYtnX9ayHfuB7ea+XrfSxOrd9E1ioigKop9Ig11IjoY0ESjjfGFIcmKoWLb1HjWh81ud7oMHuZ6FMTW2uxtC/iesyM1olDQ8xVeP24jiNzqHZugG75HpC3yd5JPnQtC4l6XwTBoJ6CA7kEbDre8Qe1o8JM9zRAcsT6P2IlF3iPCjsG9kDhnPttv8ta1xeeG7fPPJfa6qTguf1U2ik6ZVXM/RsANc5zmDOwyKwDlfmE+F7nI/fdEIvyLl5zlleq8B+CSASjQTJwU913YgPik0vXNYyKFoP6gT1pgXHxEwhohvxIwShMpJ0ghwmuTrTPJuMOhk9cKhbGpzDGOZu2KsmyuJZWrZsyW5RIPGvdmEm4FVn6AZjlyCHOfvi7n9/+jaFtvAjG9lsuN6s2QGYLunjxJW/kCS4CjfchV0LsBdBXhBYV6iUenozxni4h8Rp6M1+mwgtTIZNZqU8nicD/RSfIiXIhWCcZGkfOCsfcRVptjSfQgJgC/8X8eAen7KxmIC7PSpULi+/yl9uZrevOqUOGCx1uTdH699A9uobgk83WJ63m9SHFZGiFagUx8fBHm8JUbi91RPPxSbd7HQzWUcoOlAsqgOG9263p3aZrhdXA8yo/DT5iGJS2AGFbmHTGyXiMom+DK2ZQVqeXquXu7vyEaot9lNx2rP+Brlim9jGhg7B3oTxCeo3ZpwbR8jr2o0ri2ZuYS6XoYYFEyfuMNDnOF7c1cpeKysv/lsSEckAtsSQ2v1BGdVkzvVb3lmrSoVH0ujY1bYUn2A7qf2zJvfFNLTdnHJ4Y+Ze/YcOhojFOHwrzGQ01bD/C9aJeXJwS8FRzqMvBs1FGheQ251BnH+iIQ7wGptH9D3xbN2SOQtXIWPRnINETJJ65qKCqprPJ+92OPmqtTHFfrAnliLoRbXoGd1XGNwBJvqnWtf32zRdKrAU1NaIWEv2sdmiBVFbvwSjIU24m1t2Wnd8e3oo5wiI4E+A6hdGwAlpQ4Bb6/pmXBm/g6imJaz+ddYJxOrYG9C3nq/yMOWEpfCReJbxRrvEIjPtYa9/Tp0ZG+oJ5moU60IB5NbnU7QMfp9UMqS3y8HY6x9IwnWY/duBWf1tmN5rI16Q+MHKq536Bez2W0eY3p7P5lM4/FhY1zbkt0w90FFgEDzo+G6FiHPT0MobZQiVxlN/AEex/KHzHDCy17EGU4XcY5dMiNuUurbFUf4xN8DX4iGOeTM7sBDNv3y9qLuLY6MZl26kJAxy1WfYKkqFLi3WIx+aZPlUz1x4w2Qekexpomd2txWl+RhThaQu33ZUOEmelJuiL75O0Roed9mzoiYkWgmqHc+Tb/j2uKxYvNfThqmzGAQRFrD+W65vfN4j4pPNf/xvcI9JKPT7WY/dF4ic0BuPnZ4hWfI8ySQSRowkkdMtuYr5CwiS9PjQu7uANCnlrb1n1TF8J5bTQK+F9zLOpACMHlbG5U4tw6WpmctDi4HkmXMpYBxAxR46HeJeWPCPT/a1vQoL5rbrOUOwz8OsQrcPMJQLfK8p+t+HfYa/I+/8CEEO9lIaTHNRzBH98Qi2LAtmJ8Sa+rAxdNyZTvy44SrUvED+yXUL/PE2zTCtBRhEGJw/d4TbAUyYXexe3AMmNcQLMfibE2RuFANfvjp+G5nlPH+7Iw2ePIPNcU5EVyNPHrf1d6yHICf3DNUVYXZtleac7DgSrhsdXaRQo5qqKPAxi/Fj3c8qEjMFa67hdGiipz3prvaPiGXZ4JvFNchaYMihk4NZx37Xivyf1U5iMHaBc60t8uKhYhcROoKVukewTQSxARuzLjDu5mjPj0BTmMY6HeYSHBHHdt+76F6JgwKggGEqas1SpB4kuAdBJSeLZPmxAG0yzjmPLq2VOKggWqnxBXi3zwOCGixYYk8owKXbAKOoGfdp3ThWDs0rs25tWoCeJYb3F5t4OmP6TY/QWtORJNuBLtxlssRet3JdyBS5gau3dlc4Rx/kTsxfAxPoCM7fdUvIabfpPM6sQS6nz71xGXXirx59mJsI4nKUnqCxW4f3haE28oC0izbkfQfXF8mUDFlh4Icig8oGytSEK+RK4cz2JoQwq/SfvYuZFC8g8zp4eoV2u7n4h/SemGKeivkNeEWPi5GPLWp7CbEvjv7shr3TcoQ0J9kjpx+UfxjmF1lAmK6L3yQi6NpN3zBSHHOzoQEDtq0FV4FxbBDdktSlokF/NwzeXRCJB3gxMz90VqPc4K51MyvaLEhIRjwZTcovVljwJctYekiAwlunAJfhTTzJ2BZiTCx2pu97YexkqVjAMv5KcYWzX/Rj3VCphuiSrLZrFwxLXZPVKJS4NTKcQ59aMA2zYofwuuLvKkSPFM90sNgT9VZh1DVG0bUkdqXGiS8iHAsDcT9TD8VVZJyaQQGcPLkPWf4VrKSzvgXeCytY8lpG8ekz/IEl5jZXNJrn+PUiQQxTajn2SYqYGP9oecuMahebcPttTFNuYCmq2zk56GKgubxNlMaQ+U9tWHejSu3NjKv8qV/huWpPJzleVroYLPxfC+tmfuE8P54RqG/HQc7gTps3r6dDSa1tm2PuXV+s3iiKq9Gink0K+lkAwGkO3sYyVTRDik4cZLPOyUW7SSlULT0t+pVDOLO0yHWjhyZhxtl5TfXxT0j+N94ldWEvvBrS+eyM153ZIlpPqH6kvNyXleZXq9dFuQ2Bd2hiEVylojNyZAE72PMfq9dDcw8KCOj6q5RBCClp1xSugd+YXTI/mizOB8KYlEb2v7lHx3yEhyXucLRax+QGQ1I6j+efF1XgRiDwohQdmAXEG6UNonCArpSm5K+7zfi7r0LuDOvUspQfL0f4KSltEP5l/23ScgoAKkcMSLbFnJByewmy0RebK6bIpIVEuwKmZQHbDDhMOnXdtCspLiauFCo72mhp4j7IAytVL+eIRK+H9DG/1U2mdMScjwgYJwaX9gbF1MTnu4Y336/KfrvCK21tGvCXuBCc1roQOxiTkIzJxq2Ro8inROJWqkTqtRW5c3YK9W6fNUIvYxFaNnu23twaRn9Mv9gyZovkjuAHsIzq2aRgoSrSoiFNr00UNjoiimN3/yhav1e64l3rRyHIFalNznFhmBPmbMA1SOK5EoT9PEySc5aZHMh0dBsEHePvsPdxvdL99xODoIF7ciZ+onIGa1ner9uz0+QnoF3dP6JVAkIPaksf82yADNXB5IThdqMXoy5yecuWz4lr9NNwr2ed4R1H4qVfG56TouEvNSX8xGTxyZyG0orkmHqp1ldf0Xe5QAx8AAhKV58SPt4LpzXbZXQm0/F22MC/QGQ9rIhej/bTSs99BUFPh9yaaVw0enmmDTEdfhQM4BZprNAb7D9Ed5b1CW62FBxHOFIcufFHRN4HqV8/O7EmJLAeLzAtr3oUWRMo1sSc7RFAleeL4pamyqDzd+3OHx6nWxqejvuwXxOUCwhlqGIDbYbeU2KVi/F3Uv9vCQWgxIPlI43UIZayyJshDsFSHfHHki6mAjy3xPiU1/YnveMgBxuA0wM2WKwZ+0b7m1Fjboj9kGN51D1vwBqLkiHG/4rPBgkoyOIwAx2b/aSAzzDGtUOQeZqKQvTmx0NQxEsvcv2PsyAReKZnsrWfKy/7PsLYQGTe/e6Y4tktQ2yct+ni6LEGchgu5dcHEiSDh5cp6khNI2fgtx00DwXI+KbxZHIdhAHbWTDmShWtbA+q1IlKIvrpl9iqzFIIDSPm1nD4FC13vGrKeRcnGZnVcPawhxLFx2dcM9ac38mPudfnFVYrH4A2nTGaSfsXh1Ul8NWG4nvqPgZsa+yPE3PjpN29+dRphoJiRm02yNn4b1PHmWFxQ4a3exNosEUt3E3KIoQbn5oPqTr9ZJZEhv9yE29usRIBJlYSyh5l2LcCwCh8/ISLIiorX0LcFRkiJwH/BIciEhW9fLTjjPoaviceeTHmX0iIZSUcY64LMl3LumL2PAIO7fjvUF3tImKBQWPDm/9kl/ejdUmqpfwHYwlGNkbEL3tGX/bgvLWtT1Q0VHDt8KZxiqhVQG4EkgJcbx+gEPWgV5qdO5c0gWTIl6wUvg/cfpuwnLLNzXholkejnRHg9EysW7jSsZ4ZVMGnPfjA3PSLazxuRu/g3tYCFFaNO4APjE56Ai8LP2CZt++AHElEiAhbROc2kw3zbAD+STjbMJZP0YqC4yD/pKP4Btonvp+eD8fzmt4IKOItsFl6cPLKqy2HU0nRPx16sBXbL4YNUsMx5TDJL6Uw7Y7W49JB08gf1rn9R+zeE4vgUeMelSfU4PEPGbqBzlbICB8+ZrqnEPWPE8oZH8JXIj93Q6c7rD3bR9Hx4a99tOl1c3ghLvnoW/NCwE+133bVutGcVrqRZMWxZ64bv75fY7zijXlPFgcVfHqxvxkSZmAxAg10vMSyNDyvuKwhSF8pP1nowUfszj8NYDeL6/lUHRP9Qfb32qWd4nrqCzy4qZ0ziAxX22S2xWkIkWlVREBdRM/rFGWqjrjVk1ydgh1e4G5FSaEoT336BTO9klZXjrGmTutGd39t/Id5DPcchhkhUHcLTbGYvzcAMggw3/VWMnfY6ZD6TS3GCiOz1uSZrWpXyS/Vf/iFHS76aRhUlJ825S/+AQ2jzaW6/7+DNcYL6r7uFoZm8azzXj1kKKd1ZON2jwHFgolkNWtknEJYpOicmmDGpV6lp+K7+OLhb42yFVX5BUnUzqLgdMh7fAN0NpYqm5h6Wjkr62jm5zujQ6ofEd5gc7o0i5w+4HenRtSMeqbKJerSyPndxvwsMPLiqbphz/kkq//4Rs0c/5UN/iFEol9JlVktKeWbCslS2UG7Y7oNcR5M04INqgflW2t91EB1cnN9IEjgMGk2ba6+t/+Q6m3SorWkvVThgy5OT19YEdHPH9bCY58EngyTXYtPb/36/7Gxpjhalir0hUukrMj+cwQyzq0M3fAANHlXgv2YONX70CMLkcHczznv/NUlM9MJ0ZC6+g5jjys/U9zPJqKX3MYIA514SqhAMiP4VgkPLi6A8V3Bf/dfDHCjVepzBOyEu2IGqvx4ZJu3XDUcMVFz2kONh4DDTmDaWViHLHiPhiEyGjVA8j1RNN/hovdpW7+6H3d+PM7cMtCXR+YwvDPDILG53i6ZCy52i947injGovFwGoN1oYPdBGECUMKuKHls6yA3LjuUAFt6AgiUuTZEsqYsDi+bP2/sBbdVe4SasvRZ/4nPI+xjC+o4100lrN43QYIDrXcxmJfMi/+noZ4LLWDTVt5AfPu6U001VwladAeWdcUy5Q3AK+XHo0Iemivu78XXLZWv58SWCYvpgwuyHPbW4o1ebOIls8Y7K4htuM0dRh22RM1F5yEStg+y7Kb/3N8VGufxpkGrEvC+kBs+tbvbFlQ070i9dZV8kVW/WsY3kl/mq8INMv6tsQy3Q+WwQ9oQv7Fl0df5RK48XA1nbbnUyMJbxqRNq9ukCVPMTvdT6yXHbsGo5ojYpXI7UWkB+aIDaQeYP8SrgteqOnFzW7wXA7nuOON1thGKHMcWqgL7U26gB8iXVV6OIpRlMdgogF3X6RnNte32sR2rfmAXPdfYo/YyTSoXFJl+6T7D+RNuyflSutA41JdWwpWGlKRkEl/33gnjYc4GhKgw0ACVcNB366fnOisYqoeCJUZJuWXcArkwP5Thdw7qGCVvBRoSphytsKJU+gjOgQ9XlV6IHOn7JuYC34FC6IVDTdaG/ej2XmGFnbDJc4zg1wrFtVuTFTRrqVUzOVxhy6o96dK6mNfVUiw6n+vjLJ7MJg39D75+6wrlV1KBWrXQl1FyUTeEMKBeNwf2bNe2MbhUpyrZSVSeJZ+vJsvbScd/e5Zj/ZJMjuhqwq9vMOiiNHP5qMNbaKmAPNvojsyT4Ge4r0NnbR48wtVe7o6BXCKgbUhTKWzsQ/tVlcR20+VJVpxorIXy6xunUfYzpgODVR3q2vbWzzRxbjHeK+sqx1zZ2t4jIBuYfB1W0pevicdcbv8t1H3Wdq1K5U5KBU5cqeA+bXpxsicSJO2t1IwSvsRnAseE3RSWQ2BxI/3e5nj9MFCEkhBxVV1lw+jtStSu9shiqnmt7p5ZNcF4E37aFCwGxmauVV33eT+uuyfJ58VnRaZtPoro172jUnws8fE+wPj/Wepo68FfdHOr9LfpXgfX7Hx+mu9wDFCKu1QCbcLXRt4XSWufhzIbvDg3FfyKLENsY1Tg/aiIA3i+XugrlnMH7YNcEBgde4cZY0X8/qbqM7rDa0qqY5lStWwlUYmeF1whAk497SwC1QxkjcezvbvaS2mvpVEyHmXg4qw7OeOCddc569Lia4sk7MpC2CwFuoaTWHkhqeVBTyVSg/KqGtgpLJFY2ny4sI9JCJKsfFLfha4UJsIPxI0/cQDwyg+/oxYfcqVN1eeE7pzXOnwMbreWJ9XCX0rhRWPEc/3iKbYZCGgt967wxGLRalmACIL5nr4RLmD3d69DrCgcvHgqmy6NY3zsl4q4BMSui/bwCpr6gT22/qGh6JzCJbJchhduOCXlWfEQaM50ITR9hIF6EOK4FI8jW4yYTQAYnxrYmMLb+Jx1ViD2qgdjHAJY+l7n1guf5LpGqDixynpwksQ4EzQI4DEB+N8VHAW6ZSuRbOZR50ocKzgBbUk/LL6zCNwcXG7vidPfCKEkQXotLwjX5JfQguKxv3wj3N8arg5d5yD7xsn11YEyzZOMIHHZBfI+csxKx21IA4cOLH6fllBNjDaqCDvRLS+fUbEGqWMsfqo2ndOXyBuBibwNCd7ERBJwooa+n/kZAFegChFUtMV0vrFgBtGon7LoFvorOlk0PyPrXr9nSTCoNiYWP/dUM7tMbHjtcKlV3H7EMiWflAfvSsHLl8+/gA7TrJVyNlmUSGPJcLM0eMrvOdnA6wIqCRgyghyBi67F79n11czWBnFoALtp0pgESW/JmsWIYqGkFtrm30bevlyT3yEdOHufWeajyLMw9lkrQgyzWlQojYavXRmzyNVpE6Vtf/aBnrRUHMZaUMIZLiLj1TlgXEQutrHX7jgdDPqv/qOrF+JHixQP2q1ewKY3SdulZJYBWDUJDDe1Dp50dJcA14e8PDyaGYQGOYuOhJz/SuLr9k80akxX9OEy4ptzPnnvGogivP7Vt0oSPPYtXOqkTt12kyrB2SvMApMm0IvDhEJBd049mszyH/3M5LuTSEBf66eQeFBr1yvy+G/NHLaWu5g64htRj8fgdas2KkF0Tr0nZycdCvFBFQu2073hkuTUYo1uKtizyccrcsa4hH6WqcZX1kkBghxCvFG+R64Y76M0EaDexPYoMt+RpM7xcZJmk0Vku4XSzSANQ6eiSVH+NrM8PwBpfxlWjQNqCFNcnyToHVjmhkwVIMql3O1K/Ojonj3GrxDUFqaenF02PhNFROh12wT+Q78tFPMuX7WLaLz9HZXbVGvVkVGBLK5QZ79XAM7dTrLwQGGjHuY6v+U58wHuy+3+XIFJVVRaBA/5skqOSBRBhu+Gw8RsXunbiOWhxYsfb5kPhpOEieIoHazJwBp061LIVt2uj5YQPDcWRBPti/HYycJKwDqFlC01B3kqBtEbPOQcHwTxX9hJzeWO1v7SZQn46HaBBJmSvz4TzL60roRSrdOWw5QwPUv1M+CjPR3OIbA6zDfTUuitJ+RurIzCL7UGoHFHkYHq3mOe72q8qCu9Yug1fcfsahT469sB/7zonNDKBF4iXVr0Cv1JTCzt2qRH+eNT+HncEpmcG1+asximiFUj7UmLRL4L6f449yZbT6dJO2Bm0PyAfD9pcdyn7NUNGc467ASyEqdnnPz00UHMkwH1z1wLPTOHUGIjrkJsU4hxZ8J9b6jYMmR9Yoev4jL5436rxz+sdTQL4hdo/3uYaWrrNf8GtfAjDRKrq3xbUDI4ZWMJMRhVlizUsRaczW1PBLq27GYmxlyAxjc//soJVl5pPqfs688DN3nSAoHg6fyCI0GzCqj8lDHcsHgiz6uyKVucbpLtwe3z1KoeMdmlnuHBopL0oRWVKhNeMhnh0thB3cEjofP0x0G0/hSIsUEZDHKTV+ic31sXQ2WvLEbYGuDsk69erajHk0bJzePz5r9LwI9KHJIkRb1Ggf/TShMYqTG1KwnLlXFBvIYUElC5WPve2t3KkFcjcygLr8Xg+LEm8IZp/uVybP6zM/ZTmBO9dC36G7SG5Xxmon0qF1bFSMtnk4DC0FTeBXFyVcsJYs5pMVrJ65rSl2xb8wtLp6YXcF88+ewlU1Ner1e5JwALwwuSeVX9eJa1EI7C5LDCX+lvEWAlcqKnIu57rW4KdQCEKAxAZVM6x6z8Y5VXSwb6cG2ulTLeRvnWTspUzKJItkFtrfg2HNojq0WUKL2DgxBrX3TkJNmKN8BPF96kFHqTDoahLW0EWQ2ZM+vb5eERTWm/wQjVeUvqDCSxv8g+PgJiJ0uG+GT8ojIt7XCPFBPhNpH1BrOzjXv3PClsL8brApaVAhdDgA2ncmPIKnMrpHSz/7bQ9SLVJZJXt+0sLeg/gDPcSAY/an7WMvuDq7go9N5c3qgwr7KWUSYPegoJU3SYga05iaiR8513WKHYAbpDoPF6CozWGrNE0TDDg5eqku0xuc4QaZgfm5r9wuG4AJFCRw3MyKXwS6ebE5mj6clJDxmExN6ZiVQFdkc9dn0fDcjQb47ljGXJZBrKCMDWxH/0jLBWRjAplUs0tlPcErS9A1AoYlNKUy6ArBlFYGZ5QHtgJSF6dE8GdtI//+2YUytGZ3t+tEIq3fr6MQb5KTmrw0jK8xwJ4vp93khSAWPG0meC68AmvRlxxZ+mH+vnLGc5RwspBNHj4sS9ukW0yhQDGuftAj8QwtkGjqIx47hAYUzyE+syrHyfW1cGYhkJIlb9DnkCD4ibeKQmZpVuxaO6kuFLjwieHHZ6WdL6HrYo7TGgIGxrbVqeuExYyhRFEO4kPPZfr6Dznrg8PvFru0csULOGq0GWUYG8xWmfmRy8dtIYm3WGr/wBe4flayVzgjOsFU0qklcbqbak9VEAj1Ubcwi4LxhVDcYOj3kIrhWYBITAIRn4RVCLJoBXEhj+OXvFQbi/PCWpZFv/8ybt+P5K78+e89RjL5d/blPzevGUxpmiNZtlLyyg5UzgbxslSEe8zFsJpBUztlm8E9NQkhOdJP77MT4dohxeMoUwyw857AHX3VzLbYg95XLxyuqUwRtRb7JItMovUYChoztxP23pvsR6bsaYhc1UrYl/JyZ2vBX6s86e+Ah280H0bH8SQvE6zFMNmgCse5fWLljGJ3AB38cmUuC04CwKWdTZ557u6wlZL/sMV7YACSSSX0mdHmU8nHRP2Hic3RRyf665AzrAeDS1lVOKfxtTClUrQDGmYXYk8puo39elcnde8NtAlMgmtIGJAGiNCL4LUGouOBELjrdb3qxHJDR38I3u2PPDupN7/hEtyhreg7ArAXN4W4W4fo6C5T5buHfz3VBkem3pjzA33OXzQU4G0Mhud4YmAwybXaFvf+Gt0uPrOQTjU2MeTLrR3R9EqpCOfQOEI/FjOd2DIvb1vGX8U99BA2wyfkaw2D+i84aj4dPzFjKrlg9M2dnXhzbfA46tQhaSbIvWYGgAGVaS3Lv4WwRAAmJT9WEy17OmQN8eyzJgw5n9Uii3YA73XvOg93xdkkGj5B5Or01DICBYEYC1+k9fOS35b7S5LRcOVYWkTLX+i0CZ1ytCmw5gwmunnNGmD1cUkIhqTcaXaJI/R3d1y8ZGdS0aB4/NFmNdi5nOR75qXEyRyXdLLiUXlHnKln0CFXj5bIe6NM86spv5WgGxs9N47uzJtQFWfysZVyr2dCnzWlOALDbOZEn4ZDz4ZelnRoK+dt7d9OnjRQT85d0bezyJOSyQmdh05E24QpiPjU/OzTgHGHW4EPfuTpb0AK+PP+Mu4xG6VIQAnIbn1FqvXBxeQBkzp5+Q/MArxmBSd1PdlpZ/XIwUsANb49lWU3MxGq/4WEA83GvJx846O+f2k4mJnZWDs88+MGMzjaxS3BZcQ+wU9suSUdd28OSsxZ1kp35sh4UlY0I4olY4ZtxVMtPZ/O/YH+mxI1cIE55NOcA0woOBI23gzySU0ThjlQH3Uv//uVkLhtws2eOuN2OCYXrLgIJxDFXxawBmrWDpUJbgK+Fq+U762R/J1pTvrSm7l3/YsE4wezwn7pGuJh3dY9sVpQuxTwsHnTUP7/VNtqUs9SrEld7vOXn4XI1Hn1HbYd/k/ZpqdWF/UlyyV4NafRZ+kVnm5sTSeiziJ5y531xzmKXZhiqv9Ry9VACWYDEnxYv/za5VeeUbMYP1QDO9KoPVcGJPwijwReHjX15x5kIhcjLbz9sUfCBsOY2TLcUNuarhtJMxt3MXhK5xMtQHop9vPTxr9IAoWlgzg1+vtssD21v0Ti2e+w34dlv4dVrPzz/4aYWHI6EO/+DRaXSjU9Br9MuK0MaiIrJwbJj6lvYpln3JIr1Ke74cLTH1GCcWUvz7pZygfZeP3uCo2f8JOnCRVfEJDQ6jeEXTEkJ5tqwahjgGmY3N29pUTk/zhSHOAzbdLdUGrM31/AvT5vT5txdNZhiKQQ8rK7qasCW/e8j/YCGEsqrjfsQhf2804zg2c83A9B5bOgUmj6sohInN3ybvladB/xpG5nSnSFE5Jaz3PUYG9l1VRQOcxIW6QXJmm3Vp3QFc/qfvwQiAxbCHMdn04U+eGbXdKWf1pvCV7H7xl68djBq5BCefm/orl/jJnooEbhnymUxqgM2cVNVwZC2oscuk3J2CgHXSvQC0iHPbbFsQNQM2IgAldLIeYKjxiKupWVv499+SF63EQ04ZP1quD5tqsVF1ITMsFYKf1iUNUlcxWmi2p2nWkgKPQBKX02tOU279KTwiAB/Hmp8+lzHuzmsl4wm017rcQa3IO/sPXpH428OYao6Cm4Ox4iRG2Gq/UYviQpHm3iAugW87OCGBRbsbBx6VffYaC2vjk/g2wMFz2IJWhB2TKkK0xMIaAXDxaWL4OXPnO/yJlAI1Sg3WwtsYazB9MNDzEdFSOOTCQtCwZedYHwnOpm00qnirfJAgbHQ4hSwjN8OUBBppyAjscqks7UV+D+Ae+TS6elPLWcfMaH3/2ydOGJyEvvS0omfBtnfbVZnvu/qbJyhH+0/3lO5v/gIbPJz4q04GBkXZ1uqqvQ3pxFn2i2zPhsNQeH7v5kSEDC6+DSCG3mPGw1riDOylh3ODfT7LpPW8KwtSv0xF6ZS+X4h9jcHF6JFk4B22GOOTiCtS78ok87U9ApQR2lsA3crCgQ/nIjPusOjmElJSY4IkDTZLo+XP2m4adopI5bjsfds7BFsBu3iPF/UEGB6aXhezleWwll5SSY6QJq/7oAcqJxBs7+ec6rUMDHwQulsQWuo/kQkxBEf6Au9QVqYhu4LIflpvRlgfppY/r4F8yTbaibz9KQqWCR7rWSr3LNf4KbCQrnH1/j2eL4O4ZdwlYw/XG2BcXDVsNT8Q+E/FSlntxoH5oz3K3vAwAS38fh0IsWf/JGspN91x31pLyLRzksp4ruMN3ouvZwo1fE8TU2S5ro7q0cI2XJ1xLaxn2aFyXDe9eXeT9ZAFxU+g+RRqIDWALcn6rWfVMaBzJLut6J02ojptfRcWe0HC9md2+NxqyIxzVvOr5dgcqNdTZ7FBquRNeNnt4+wUfTmcgCepiz1Dh38F+QsZ6E3o92LB9Mh0ewTy70GY7ij/za2s4OV2GAD4R/RIZISQWCFn5o7LPAazND0d8Lx4SVy8EdVeDQ1jLKq0Rc0TstXUqpAM/V0uuduBkn1qGH2LvgQ0HYtXgsGTv6las6CJJR13pP0bgLE+8ufe3St9TyQqmWzZsVWLDefqjdQEc379wjxZ9oOrhq+1By3Jb44czvhAMYSdq9yUOoHQtTG20PAL1jako9sJgVVYrSj2sfmX9phyECHhZOfuNgsXBYDowZLzl5UY2ixfYQUYpFguJjj+Z1QiwYDzL5Vs3saWaSqKD0UQilLWcPUdwngLYRmH9aVlEP988Z+GQiRPBGIosK5DYaH5hj+GpN4o5L2j6FSekT6xUDAoD6twd9awOzB/85J3ZDeydYSzASPOu1nxaMso+sIG2ZIuB0o/LmcMc4bow7cPMXO1RegbkiZMjmxtbCeNZrMekAJjZEXEIjJN3EMTcw9zyM6sY/sx+hXvql4PoEz5crpP2Rzg3K7C2j1fwJuoX6fy5v/CplJo5ku5uLVaHYLtH0N0ePRx5OdUWbzPWXucwqgad1yPGxRhrGf/wgSSbA2xVzjSbeXyfLFGIVK516/HL9/FLsILxiLMGeCUTpMKymYf+q4Gk4mokowI7ROZyM9JojopKsAjtk7k9ZioHAmH3kK+fJfw7BGmUz68GvxAlDQt0Yt8Z3HtJWrnw/Ek3M86zPXlZ/2ElrSYE19yY74b38rfhm1Na4wEMXWaJxz3QTgWxxv0BXQQJSZgmLLmODBzNXyTAXH0ZLQalX1VaPLIxQJ9fRfoFR5Q1wJk2ju82YuF9LGWpNs39laqZwMLPXtqHDw+MhhWQoSha/dyZCPMQaaFoR58Lss0hGXbI/UQHtMz2YeHa+H8b0fuJOg+S3r8dUBEDrxPb/i81WJS/CpQTGKOQtmhanAhY29AhKWc+2QwxuRaSZlGxnot5hmY6HoAZfuBb654yVFVAQPafWJJuHoBwQAMUzSwQA9FumkAKNd01qP2d6AqsSjMIH1DTBTrmKcfvW55k9t4KuHWKyA8T0Q+/uHI4WsqduMDu8tOFgv93KmVjaFIX4T68/xJvEOrIbnIQYKizPvWnASMXc0UsyDatLshz0YgqUgRi7iPUKsEKqpuEuYoegSTkaZchrDz9HM1YvkVjrXwwC43RmGzMPnBMZiP3yxH8Y20VstqH8sSxnGC/CrjGvPOgGoL6ueMIXyfvcOycwG2Eb3INJH6VLoeNwEgOqNRFifYlc2Kwd8mjBxmiy7YzJx/O8DXHt9QsImcZvU+au7KvzzHeURZzrtAkZ1gcS4AHlTHAkjNAybvB2WRSSGThMlFQJhxsJimnZspMVgcIc45wXZmB8MxMBp8FGxfbRs46N3mPMdGbehpn/Gv0zUy3OQ69kUKz2NnrSqree416GuiOCDKc7dPbSIX4upsA5I1tLlGQGSY/Df0tthnKXThBrIFqVfz2GwXzdLXAqqGyYLLtUPTxJhxVXk9ZSH0DLQ/U6gtJxFqQEzZ96IXrMG4QkeyxhPhZ3z3jqVZtYYi3oSDhWeBdPSJneUgb7nuLaV6AVNyax4Azg354l/NZjn6qi4AYHU4xLnhZsnjJef6QvJcXsv4/TTKsOK79WrRTjjb1o7wkbi1GYZVEeMW/Mqs48iMc9xrEO18GLManAx+Q3Jk5myRJdPDwlnY6CvsCyPzaYQeFKSWxa+qqhSaz0YXa7HSbj+cdcem/lMz665EXk6M+tLad0NjP580KsYhROmSa+ISpKDKpfCc64ZkrMbOlcAqlVUIZNpEPzdmczfx4cpaIAcz37VjPBDMX6n9MuucaZAGD0Qx6AdyExVB5J++N2LWR0qs6C1EaHwexGaSdtlVhy27AjqIozqpcrS6VF3ew2+6XtAd0JE8rYgtnJgAAAAA=="
          alt="SensorTrain">
        <div class="sensortrain-brand-subtitle">
          MICROSHIFT MOBILE SENSOR PLATFORM
        </div>
      </div>

      <div class="header-status">

        <div class="system-strip">

          <span
            class="system-item"
            title="Pico telemetry status">
            <span
              id="systemPico"
              class="system-dot unknown">
            </span>
            PICO
          </span>

          <span
            class="system-item"
            title="Host bridge service status">
            <span
              id="systemBridge"
              class="system-dot unknown">
            </span>
            BRIDGE
          </span>

          <span
            class="system-item"
            title="MicroShift API readiness">
            <span
              id="systemCluster"
              class="system-dot unknown">
            </span>
            CLUSTER
          </span>

        </div>


        <span
          id="statusBadge"
          class="status-badge waiting">
          WAITING
        </span>

        <span>
          FW
          <span
            id="firmware"
            class="header-value">--</span>
        </span>

        <span>
          Seq
          <span
            id="seq"
            class="header-value">--</span>
        </span>

        <span>
          Pico
          <span
            id="uptime"
            class="header-value">--</span>
        </span>

        <span>
          Age
          <span
            id="age"
            class="header-value">--</span>
        </span>

      </div>

    </header>


    <div
      id="errorStrip"
      class="error-strip">
    </div>


    <!-- ==========================================
         Gauges
         ========================================== -->

    <section class="gauges">

      <div class="gauge-card">

        <div class="gauge-title">
          Pi CPU
        </div>

        <div class="gauge">

          <svg viewBox="0 0 120 70">

            <path
              class="gauge-track"
              pathLength="100"
              d="M 10 60 A 50 50 0 0 1 110 60">
            </path>

            <path
              id="piCpuArc"
              class="gauge-value-arc"
              pathLength="100"
              d="M 10 60 A 50 50 0 0 1 110 60">
            </path>

          </svg>

          <div class="gauge-number">

            <span id="piCpu">--</span>
            <span class="gauge-unit">°F</span>

          </div>

        </div>

      </div>


      <button
        class="gauge-card"
        type="button"
        onclick="selectHistory('temperature')">

        <div class="gauge-title">
          Compute
        </div>

        <div class="gauge">
          <svg viewBox="0 0 120 70">
            <path
              class="gauge-track"
              pathLength="100"
              d="M 10 60 A 50 50 0 0 1 110 60">
            </path>

            <path
              id="computeArc"
              class="gauge-value-arc"
              pathLength="100"
              d="M 10 60 A 50 50 0 0 1 110 60">
            </path>
          </svg>

          <div class="gauge-number">
            <span id="compute">--</span>
            <span class="gauge-unit">°F</span>
          </div>
        </div>

      </button>


      <button
        class="gauge-card"
        type="button"
        onclick="selectHistory('temperature')">

        <div class="gauge-title">
          Exhaust
        </div>

        <div class="gauge">
          <svg viewBox="0 0 120 70">
            <path
              class="gauge-track"
              pathLength="100"
              d="M 10 60 A 50 50 0 0 1 110 60">
            </path>

            <path
              id="exhaustArc"
              class="gauge-value-arc"
              pathLength="100"
              d="M 10 60 A 50 50 0 0 1 110 60">
            </path>
          </svg>

          <div class="gauge-number">
            <span id="exhaust">--</span>
            <span class="gauge-unit">°F</span>
          </div>
        </div>

      </button>


      <button
        class="gauge-card"
        type="button"
        onclick="selectHistory('temperature')">

        <div class="gauge-title">
          Ambient
        </div>

        <div class="gauge">
          <svg viewBox="0 0 120 70">
            <path
              class="gauge-track"
              pathLength="100"
              d="M 10 60 A 50 50 0 0 1 110 60">
            </path>

            <path
              id="ambientArc"
              class="gauge-value-arc"
              pathLength="100"
              d="M 10 60 A 50 50 0 0 1 110 60">
            </path>
          </svg>

          <div class="gauge-number">
            <span id="ambient">--</span>
            <span class="gauge-unit">°F</span>
          </div>
        </div>

      </button>


      <button
        class="gauge-card"
        type="button"
        onclick="selectHistory('humidity')">

        <div class="gauge-title">
          Humidity
        </div>

        <div class="gauge">
          <svg viewBox="0 0 120 70">
            <path
              class="gauge-track"
              pathLength="100"
              d="M 10 60 A 50 50 0 0 1 110 60">
            </path>

            <path
              id="humidityArc"
              class="gauge-value-arc"
              pathLength="100"
              d="M 10 60 A 50 50 0 0 1 110 60">
            </path>
          </svg>

          <div class="gauge-number">
            <span id="humidity">--</span>
            <span class="gauge-unit">%</span>
          </div>
        </div>

      </button>


      <button
        class="gauge-card"
        type="button"
        onclick="selectHistory('pressure')">

        <div class="gauge-title">
          Pressure
        </div>

        <div class="gauge">
          <svg viewBox="0 0 120 70">
            <path
              class="gauge-track"
              pathLength="100"
              d="M 10 60 A 50 50 0 0 1 110 60">
            </path>

            <path
              id="pressureArc"
              class="gauge-value-arc"
              pathLength="100"
              d="M 10 60 A 50 50 0 0 1 110 60">
            </path>
          </svg>

          <div class="gauge-number">
            <span id="pressure">--</span>
            <span class="gauge-unit">hPa</span>
          </div>
        </div>

      </button>

    </section>


    <!-- ==========================================
         Instruments
         ========================================== -->

    <section class="instruments">

      <div class="panel motion-panel combined-motion-panel">

        <div class="panel-title">
          Motion &amp; Orientation
        </div>

        <div class="combined-motion-grid">

          <div>
            <div class="motion-group-title">
              Acceleration
            </div>

            <div class="axis-row">
              <span class="axis-label">X</span>
              <span class="axis-value">
                <span id="accelX">--</span>
                <span class="axis-unit">m/s²</span>
              </span>
            </div>

            <div class="axis-row">
              <span class="axis-label">Y</span>
              <span class="axis-value">
                <span id="accelY">--</span>
                <span class="axis-unit">m/s²</span>
              </span>
            </div>

            <div class="axis-row">
              <span class="axis-label">Z</span>
              <span class="axis-value">
                <span id="accelZ">--</span>
                <span class="axis-unit">m/s²</span>
              </span>
            </div>
          </div>


          <div>
            <div class="motion-group-title">
              Gyroscope
            </div>

            <div class="axis-row">
              <span class="axis-label">X</span>
              <span class="axis-value">
                <span id="gyroX">--</span>
                <span class="axis-unit">rad/s</span>
              </span>
            </div>

            <div class="axis-row">
              <span class="axis-label">Y</span>
              <span class="axis-value">
                <span id="gyroY">--</span>
                <span class="axis-unit">rad/s</span>
              </span>
            </div>

            <div class="axis-row">
              <span class="axis-label">Z</span>
              <span class="axis-value">
                <span id="gyroZ">--</span>
                <span class="axis-unit">rad/s</span>
              </span>
            </div>
          </div>

          <div class="combined-motion-orientation">

            <div class="motion-group-title">
              Orientation
            </div>

            <div class="orientation-values">
          <div class="axis-row">
            <span class="axis-label">I</span>
            <span
              id="quatI"
              class="axis-value">--</span>
          </div>

          <div class="axis-row">
            <span class="axis-label">J</span>
            <span
              id="quatJ"
              class="axis-value">--</span>
          </div>

          <div class="axis-row">
            <span class="axis-label">K</span>
            <span
              id="quatK"
              class="axis-value">--</span>
          </div>

          <div class="axis-row">
            <span class="axis-label">R</span>
            <span
              id="quatReal"
              class="axis-value">--</span>
          </div>
            </div>

          </div>

        </div>

      </div>


      <div class="panel wheel-panel">

        <div class="panel-title">
          Travel
        </div>


        <div class="wheel-center">

          <div class="wheel-primary">
            <span
              id="wheelRpm"
              class="wheel-rpm">--</span>
            <span class="wheel-rpm-unit">
              RPM
            </span>
          </div>

          <div class="wheel-stats">

            <div class="wheel-stat">
              <span class="wheel-stat-label">
                SPEED
              </span>
              <span
                id="wheelSpeed"
                class="wheel-stat-value">--</span>
              <span class="wheel-stat-unit">
                MPH
              </span>
            </div>

            <div class="wheel-stat">
              <span class="wheel-stat-label">
                REVS
              </span>
              <span
                id="wheelCount"
                class="wheel-stat-value">--</span>
            </div>

            <div class="wheel-stat">
              <span class="wheel-stat-label">
                DIST
              </span>
              <span
                id="wheelDistance"
                class="wheel-stat-value">--</span>
              <span class="wheel-stat-unit">
                mi
              </span>
            </div>

            <div class="wheel-stat">
              <span class="wheel-stat-label">
                IR
              </span>
              <span
                id="wheelRc"
                class="wheel-stat-value">--</span>
              <span class="wheel-stat-unit">
                &micro;s
              </span>
            </div>

          </div>

        </div>

        <div class="wheel-marker-row">

          <span
            id="wheelLamp"
            class="wheel-lamp clear">
          </span>

          <span
            id="wheelMarker"
            class="wheel-marker-text">
            CLEAR
          </span>

          <span class="wheel-sensor">
            QTR-HD-01RC
          </span>

        </div>

      </div>


      <div class="panel fan-panel">

        <div class="panel-title">
          Train Control
        </div>

        <div class="train-control-stack">

          <div class="motor-status-grid">

            <div id="motorBridgePill"
                 class="motor-pill">
              BRIDGE
            </div>

            <div id="motorBlePill"
                 class="motor-pill">
              BLE
            </div>

            <div id="motorHubPill" class="motor-pill st15-dual-pill">
              <span class="st15-dual-label">TRAIN</span>
              <span class="st15-dual-unit st15-unknown" data-role="locomotive" title="Locomotive status unavailable">L</span>
              <span class="st15-dual-unit st15-unknown" data-role="caboose" title="Caboose status unavailable">C</span>
            </div>

            <div id="motorLinkPill" class="motor-pill st15-dual-pill">
              <span class="st15-dual-label">LEGO LINK</span>
              <span class="st15-dual-unit st15-unknown" data-role="locomotive" title="Locomotive status unavailable">L</span>
              <span class="st15-dual-unit st15-unknown" data-role="caboose" title="Caboose status unavailable">C</span>
            </div>

          </div>



          <div class="st-dual-strip">
            <div class="st-dual-heading">
              <span>BOTH MOTORS · SYNCHRONIZED</span>
              <span class="st-dual-result"
                    id="stMainPair">WAITING</span>
            </div>

            <div class="st-dual-grid">
              <div class="st-dual-hub">
                <strong>LOCOMOTIVE</strong>
                <span id="stMainLoco">Waiting...</span>
              </div>

              <div class="st-dual-hub">
                <strong>CABOOSE</strong>
                <span id="stMainCaboose">Waiting...</span>
              </div>
            </div>
          </div>

          <div class="motor-main">

            <div class="motor-readout">

              <div id="motorDirection"
                   class="motor-direction">
                STOPPED
              </div>

              <div id="motorSpeed"
                   class="motor-speed">
                0%
              </div>

            </div>


            <div class="motor-buttons">

              <button id="motorReverse"
                      class="motor-button"
                      type="button"
                      disabled
                      onclick="sendMotorDirection(-1)">
                ◀ REVERSE
              </button>

              <button id="motorStop"
                      class="motor-button stop"
                      type="button"
                      disabled
                      onclick="sendMotorSpeed(0)">
                STOP
              </button>

              <button id="motorForward"
                      class="motor-button"
                      type="button"
                      disabled
                      onclick="sendMotorDirection(1)">
                FORWARD ▶
              </button>

            </div>

          </div>


          <div class="motor-power-row">

            <div class="motor-power-label">
              POWER
            </div>

            <input
              id="motorPower"
              class="motor-power-slider"
              type="range"
              min="10"
              max="100"
              step="10"
              value="20"
              oninput="updateMotorPower(this.value)">

            <div
              id="motorPowerValue"
              class="motor-power-value">
              20%
            </div>

          </div>


          <div id="motorControlStatus"
               class="motor-command-status">
            Waiting for motor bridge...
          </div>


          <div class="fan-compact">

            <div class="fan-compact-name">
              INTAKE
            </div>

            <div class="fan-compact-buttons">

              <button id="fanIntake0"
                      class="fan-button"
                      type="button"
                      onclick="sendFanLevel('intake',0)">
                OFF
              </button>

              <button id="fanIntake100"
                      class="fan-button"
                      type="button"
                      onclick="sendFanLevel('intake',100)">
                ON
              </button>

            </div>

            <div id="intakeActual"
                 class="fan-actual">
              0%
            </div>


            <div class="fan-compact-name">
              EXHAUST
            </div>

            <div class="fan-compact-buttons">

              <button id="fanExhaust0"
                      class="fan-button"
                      type="button"
                      onclick="sendFanLevel('exhaust',0)">
                OFF
              </button>

              <button id="fanExhaust100"
                      class="fan-button"
                      type="button"
                      onclick="sendFanLevel('exhaust',100)">
                ON
              </button>

            </div>

            <div id="exhaustActual"
                 class="fan-actual">
              0%
            </div>


            <button id="fanAllOff"
                    class="all-off"
                    type="button"
                    onclick="sendFans(0,0)">
              ALL OFF
            </button>

          </div>


          <div id="controlStatus"
               class="control-status fan-command-status">
            Ready
          </div>

        </div>

      </div>

    </section>


    <!-- ==========================================
         Always-visible history
         ========================================== -->

    <section class="panel history-panel">

      <div class="history-toolbar">

        <div class="panel-title">
          Sensor History
        </div>

        <div class="history-ranges">

          <button id="range1"
                  class="range-button"
                  type="button"
                  onclick="setHistoryRange(1)">
            1m
          </button>

          <button id="range5"
                  class="range-button active"
                  type="button"
                  onclick="setHistoryRange(5)">
            5m
          </button>

          <button id="range10"
                  class="range-button"
                  type="button"
                  onclick="setHistoryRange(10)">
            10m
          </button>

          <button id="range20"
                  class="range-button"
                  type="button"
                  onclick="setHistoryRange(20)">
            20m
          </button>

          <button id="range30"
                  class="range-button"
                  type="button"
                  onclick="setHistoryRange(30)">
            30m
          </button>

        </div>

      </div>


      <div class="mini-chart-grid">


        <div
          id="chartCardTemperature"
          class="mini-chart">

          <div class="mini-chart-header">
            <div class="mini-chart-title">
              Temperature
            </div>

            <div class="mini-legend">
              <span class="mini-legend-item">
                <span class="mini-swatch"
                      style="background:#58a6ff"></span>
                Compute
              </span>

              <span class="mini-legend-item">
                <span class="mini-swatch"
                      style="background:#ff9b66"></span>
                Exhaust
              </span>

              <span class="mini-legend-item">
                <span class="mini-swatch"
                      style="background:#63d69d"></span>
                Ambient
              </span>
            </div>
          </div>

          <canvas
            id="chartTemperature"
            class="mini-canvas">
          </canvas>

        </div>


        <div
          id="chartCardHumidity"
          class="mini-chart">

          <div class="mini-chart-header">
            <div class="mini-chart-title">
              Humidity
            </div>
          </div>

          <canvas
            id="chartHumidity"
            class="mini-canvas">
          </canvas>

        </div>


        <div
          id="chartCardPressure"
          class="mini-chart">

          <div class="mini-chart-header">
            <div class="mini-chart-title">
              Pressure
            </div>
          </div>

          <canvas
            id="chartPressure"
            class="mini-canvas">
          </canvas>

        </div>


        <div
          id="chartCardAcceleration"
          class="mini-chart">

          <div class="mini-chart-header">
            <div class="mini-chart-title">
              Acceleration
            </div>

            <div class="mini-legend">
              <span class="mini-legend-item">
                <span class="mini-swatch"
                      style="background:#58a6ff"></span>X
              </span>

              <span class="mini-legend-item">
                <span class="mini-swatch"
                      style="background:#ff9b66"></span>Y
              </span>

              <span class="mini-legend-item">
                <span class="mini-swatch"
                      style="background:#63d69d"></span>Z
              </span>
            </div>
          </div>

          <canvas
            id="chartAcceleration"
            class="mini-canvas">
          </canvas>

        </div>


        <div
          id="chartCardGyro"
          class="mini-chart">

          <div class="mini-chart-header">
            <div class="mini-chart-title">
              Gyroscope
            </div>

            <div class="mini-legend">
              <span class="mini-legend-item">
                <span class="mini-swatch"
                      style="background:#58a6ff"></span>X
              </span>

              <span class="mini-legend-item">
                <span class="mini-swatch"
                      style="background:#ff9b66"></span>Y
              </span>

              <span class="mini-legend-item">
                <span class="mini-swatch"
                      style="background:#63d69d"></span>Z
              </span>
            </div>
          </div>

          <canvas
            id="chartGyro"
            class="mini-canvas">
          </canvas>

        </div>


        <div
          id="chartCardOrientation"
          class="mini-chart">

          <div class="mini-chart-header">
            <div class="mini-chart-title">
              Orientation
            </div>

            <div class="mini-legend">
              <span>I</span>
              <span>J</span>
              <span>K</span>
              <span>R</span>
            </div>
          </div>

          <canvas
            id="chartOrientation"
            class="mini-canvas">
          </canvas>

        </div>


        <div
          id="chartCardWheel"
          class="mini-chart">

          <div class="mini-chart-header">
            <div class="mini-chart-title">
              Wheel Speed
            </div>

            <div class="mini-legend">
              m/s
            </div>
          </div>

          <canvas
            id="chartWheel"
            class="mini-canvas">
          </canvas>

        </div>


        <div
          id="chartCardFans"
          class="mini-chart fan-status-card">

          <div class="mini-chart-header">

            <div class="mini-chart-title">
              Fan Status
            </div>

          </div>


          <div class="fan-light-area">

            <div class="fan-light-item">

              <div class="fan-light-label">
                INTAKE
              </div>

              <svg
                class="fan-graphic"
                viewBox="0 0 100 100"
                aria-label="Intake fan">

                <circle
                  class="fan-housing"
                  cx="50"
                  cy="50"
                  r="43">
                </circle>

                <circle
                  class="fan-inner-ring"
                  cx="50"
                  cy="50"
                  r="35">
                </circle>

                <g
                  id="intakeFanRotor"
                  class="fan-rotor">

                  <path
                    class="fan-blade"
                    d="M50 48
                       C53 29 65 20 76 24
                       C84 27 84 38 77 45
                       C70 52 60 54 50 52 Z">
                  </path>

                  <path
                    class="fan-blade"
                    d="M52 50
                       C71 53 80 65 76 76
                       C73 84 62 84 55 77
                       C48 70 46 60 48 50 Z">
                  </path>

                  <path
                    class="fan-blade"
                    d="M50 52
                       C47 71 35 80 24 76
                       C16 73 16 62 23 55
                       C30 48 40 46 50 48 Z">
                  </path>

                  <path
                    class="fan-blade"
                    d="M48 50
                       C29 47 20 35 24 24
                       C27 16 38 16 45 23
                       C52 30 54 40 52 50 Z">
                  </path>

                  <circle
                    class="fan-hub"
                    cx="50"
                    cy="50"
                    r="9">
                  </circle>

                </g>
              </svg>

              <div
                id="intakeFanState"
                class="fan-light-state">
                OFF
              </div>

            </div>


            <div class="fan-light-item">

              <div class="fan-light-label">
                EXHAUST
              </div>

              <svg
                class="fan-graphic"
                viewBox="0 0 100 100"
                aria-label="Exhaust fan">

                <circle
                  class="fan-housing"
                  cx="50"
                  cy="50"
                  r="43">
                </circle>

                <circle
                  class="fan-inner-ring"
                  cx="50"
                  cy="50"
                  r="35">
                </circle>

                <g
                  id="exhaustFanRotor"
                  class="fan-rotor">

                  <path
                    class="fan-blade"
                    d="M50 48
                       C53 29 65 20 76 24
                       C84 27 84 38 77 45
                       C70 52 60 54 50 52 Z">
                  </path>

                  <path
                    class="fan-blade"
                    d="M52 50
                       C71 53 80 65 76 76
                       C73 84 62 84 55 77
                       C48 70 46 60 48 50 Z">
                  </path>

                  <path
                    class="fan-blade"
                    d="M50 52
                       C47 71 35 80 24 76
                       C16 73 16 62 23 55
                       C30 48 40 46 50 48 Z">
                  </path>

                  <path
                    class="fan-blade"
                    d="M48 50
                       C29 47 20 35 24 24
                       C27 16 38 16 45 23
                       C52 30 54 40 52 50 Z">
                  </path>

                  <circle
                    class="fan-hub"
                    cx="50"
                    cy="50"
                    r="9">
                  </circle>

                </g>
              </svg>

              <div
                id="exhaustFanState"
                class="fan-light-state">
                OFF
              </div>

            </div>

          </div>

        </div>


      </div>

    </section>

  </div>


  <script>

    const STALE_SECONDS = 8;

    let latestFanState = {
      intake_pct: 0,
      exhaust_pct: 0
    };

    let telemetryReady = false;
    let controlBusy = false;
    let pendingFanCommand = null;

    let historyMetric = "temperature";
    let historyMinutes = 5;
    let latestHistory = [];


    const SERIES_COLORS = [
      "#58a6ff",
      "#ff9b66",
      "#63d69d",
      "#c084fc"
    ];


    function cToF(value) {

      if (
        value === null ||
        value === undefined ||
        value === ""
      ) {
        return null;
      }

      const number =
        Number(value);

      if (!Number.isFinite(number)) {
        return null;
      }

      return (
        number * 9 / 5 + 32
      );
    }


    const HISTORY = {

      temperature: {
        title: "Temperature History",
        unit: "°F",

        series: [
          {
            label: "Pi CPU",
            value: s =>
              cToF(
                s.temperatures?.pi_cpu_c
              )
          },

          {
            label: "Compute",
            value: s =>
              cToF(s.temperatures?.compute_c)
          },
          {
            label: "Exhaust",
            value: s =>
              cToF(s.temperatures?.exhaust_c)
          },
          {
            label: "Ambient",
            value: s =>
              cToF(s.environment?.temperature_c)
          }
        ]
      },


      humidity: {
        title: "Humidity History",
        unit: "%",

        series: [
          {
            label: "Humidity",
            value: s =>
              s.environment?.humidity_pct
          }
        ]
      },


      pressure: {
        title: "Pressure History",
        unit: "hPa",

        series: [
          {
            label: "Pressure",
            value: s =>
              s.environment?.pressure_hpa
          }
        ]
      },


      acceleration: {
        title: "Acceleration History",
        unit: "m/s²",

        series: [
          {
            label: "X",
            value: s => s.motion?.accel_x
          },
          {
            label: "Y",
            value: s => s.motion?.accel_y
          },
          {
            label: "Z",
            value: s => s.motion?.accel_z
          }
        ]
      },


      gyro: {
        title: "Gyroscope History",
        unit: "",

        series: [
          {
            label: "X",
            value: s => s.motion?.gyro_x
          },
          {
            label: "Y",
            value: s => s.motion?.gyro_y
          },
          {
            label: "Z",
            value: s => s.motion?.gyro_z
          }
        ]
      },


      orientation: {
        title: "Orientation History",
        unit: "",

        series: [
          {
            label: "I",
            value: s => s.motion?.quat_i
          },
          {
            label: "J",
            value: s => s.motion?.quat_j
          },
          {
            label: "K",
            value: s => s.motion?.quat_k
          },
          {
            label: "Real",
            value: s => s.motion?.quat_real
          }
        ]
      },


      wheel: {
        title: "Travel Speed History",
        unit: "MPH",
        yMin: 0,
        yMax: 150,

        series: [
          {
            label: "Scale Speed",

            value: s =>
              s.wheel?.speed_mps == null
                ? null
                : (
                    s.wheel.speed_mps
                    * 40
                    * 2.2369362920544
                  )
          }
        ]
      },


      fans: {
        title: "Fan Output History",
        unit: "%",
        yMin: 0,
        yMax: 100,

        series: [
          {
            label: "Intake",
            step: true,

            value: s =>
              s.fans?.intake_pct
          },
          {
            label: "Exhaust",
            step: true,

            value: s =>
              s.fans?.exhaust_pct
          }
        ]
      }

    };


    function el(id) {
      return document.getElementById(id);
    }


    function finite(value) {
      return (
        typeof value === "number" &&
        Number.isFinite(value)
      );
    }


    function show(value, digits = 2) {

      if (!finite(value)) {
        return "--";
      }

      return value.toFixed(digits);
    }


    function clamp(value, min, max) {
      return Math.max(
        min,
        Math.min(max, value)
      );
    }

    function ensureGaugeScale(
      arcId,
      min,
      max
    ) {

      const arc =
        el(arcId);

      const svg =
        arc.closest("svg");


      if (
        svg.querySelector(
          ".gauge-scale"
        )
      ) {
        return;
      }


      const ns =
        "http://www.w3.org/2000/svg";

      const group =
        document.createElementNS(
          ns,
          "g"
        );

      group.setAttribute(
        "class",
        "gauge-scale"
      );


      /*
       * Gauge geometry:
       *
       * center = 60,60
       * arc radius = 50
       *
       * Five marks at:
       * 180°, 135°, 90°, 45°, 0°
       */

      for (let i = 0; i <= 4; i++) {

        const fraction =
          i / 4;

        const angle =
          Math.PI -
          fraction * Math.PI;


        const outerRadius = 48;
        const innerRadius = 42;

        const labelRadius =
          arcId === "pressureArc"
            ? 39
            : 36;


        const x1 =
          60 +
          Math.cos(angle) *
          outerRadius;

        const y1 =
          60 -
          Math.sin(angle) *
          outerRadius;


        const x2 =
          60 +
          Math.cos(angle) *
          innerRadius;

        const y2 =
          60 -
          Math.sin(angle) *
          innerRadius;


        const tx =
          60 +
          Math.cos(angle) *
          labelRadius;

        const ty =
          60 -
          Math.sin(angle) *
          labelRadius;


        const tick =
          document.createElementNS(
            ns,
            "line"
          );

        tick.setAttribute(
          "x1",
          x1
        );

        tick.setAttribute(
          "y1",
          y1
        );

        tick.setAttribute(
          "x2",
          x2
        );

        tick.setAttribute(
          "y2",
          y2
        );

        tick.setAttribute(
          "class",
          i === 2
            ? "gauge-scale-tick major"
            : "gauge-scale-tick"
        );


        const label =
          document.createElementNS(
            ns,
            "text"
          );


        const scaleValue =
          min +
          fraction *
          (max - min);


        label.textContent =
          Number.isInteger(scaleValue)
            ? scaleValue
            : scaleValue.toFixed(1);


        label.setAttribute(
          "x",
          tx
        );

        label.setAttribute(
          "y",
          ty
        );

        label.setAttribute(
          "class",
          arcId === "pressureArc"
            ? "gauge-scale-label pressure"
            : "gauge-scale-label"
        );

        label.setAttribute(
          "text-anchor",
          "middle"
        );

        label.setAttribute(
          "dominant-baseline",
          "middle"
        );


        group.appendChild(tick);
        group.appendChild(label);
      }


      /*
       * Insert the scale before the live value arc
       * so the active arc remains visually dominant.
       */

      svg.insertBefore(
        group,
        arc
      );
    }


    function setGauge(
      valueId,
      arcId,
      value,
      min,
      max,
      digits
    ) {

      ensureGaugeScale(
        arcId,
        min,
        max
      );


      el(valueId).textContent =
        show(value, digits);


      let pct = 0;


      if (finite(value)) {

        pct =
          100 *
          (
            clamp(
              value,
              min,
              max
            ) -
            min
          ) /
          (
            max -
            min
          );

      }


      el(arcId).style.strokeDasharray =
        pct.toFixed(1) +
        " 100";
    }


    function setStatus(data) {

      const badge =
        el("statusBadge");

      const age =
        data.age_s;

      let text = "LIVE";
      let cls = "live";


      if (data.status === "waiting") {

        text = "WAITING";
        cls = "waiting";

      }
      else if (
        age === undefined ||
        age === null ||
        age > STALE_SECONDS
      ) {

        text = "STALE";
        cls = "stale";

      }
      else if (data.status !== "ok") {

        text = "DEGRADED";
        cls = "degraded";

      }


      badge.textContent = text;
      badge.className =
        "status-badge " + cls;
    }


    function setErrors(errors) {

      const strip =
        el("errorStrip");

      if (!errors || errors.length === 0) {

        strip.style.display = "none";
        strip.textContent = "";
        return;
      }

      strip.textContent =
        errors.join(" · ");

      strip.style.display = "block";
    }


    function setFanIndicator(
      lampId,
      stateId,
      on
    ) {

      const lamp =
        el(lampId);

      const state =
        el(stateId);

      if (!lamp || !state) {
        return;
      }

      lamp.classList.toggle(
        "on",
        on
      );

      state.textContent =
        on ? "ON" : "OFF";
    }


    function refreshFanControls() {

      const buttons =
        document.querySelectorAll(
          ".fan-button"
        );

      for (const button of buttons) {

        button.disabled =
          controlBusy ||
          !telemetryReady;
      }


      el("fanAllOff").disabled =
        controlBusy;
    }


    function updateFanDisplay(data) {

      for (const button of
        document.querySelectorAll(
          ".fan-button"
        )) {

        button.classList.remove(
          "actual"
        );
      }

      el("fanAllOff").classList.remove(
        "actual"
      );


      const intake =
        Number(
          data.fans?.intake_pct ?? 0
        );

      const exhaust =
        Number(
          data.fans?.exhaust_pct ?? 0
        );


      latestFanState = {
        intake_pct: intake,
        exhaust_pct: exhaust
      };

      updateFanGraphic(
        "intakeFanRotor",
        "intakeFanState",
        intake
      );

      updateFanGraphic(
        "exhaustFanRotor",
        "exhaustFanState",
        exhaust
      );


      el("intakeActual").textContent =
        intake >= 100 ? "ON" : "OFF";

      el("exhaustActual").textContent =
        exhaust >= 100 ? "ON" : "OFF";


      const intakeButton =
        el("fanIntake" + intake);

      const exhaustButton =
        el("fanExhaust" + exhaust);


      if (intakeButton) {
        intakeButton.classList.add(
          "actual"
        );
      }

      if (exhaustButton) {
        exhaustButton.classList.add(
          "actual"
        );
      }


      if (intake === 0 && exhaust === 0) {

        el("fanAllOff").classList.add(
          "actual"
        );
      }


      if (pendingFanCommand !== null) {

        if (
          intake ===
            pendingFanCommand.intake_pct &&
          exhaust ===
            pendingFanCommand.exhaust_pct
        ) {

          el("controlStatus").textContent =
            "Confirmed by Pico";

          el("controlStatus").className =
            "control-status confirmed";

          pendingFanCommand = null;
        }
      }


      refreshFanControls();
    }




    // SENSORTRAIN_FAN_GRAPHICS_V1

    function fanSpinDuration(pct) {

      if (!Number.isFinite(pct) || pct <= 0) {
        return null;
      }

      const level = Math.max(
        0,
        Math.min(100, pct)
      );

      return (
        2.0 -
        (level / 100) * 1.4
      ).toFixed(2) + "s";
    }


    function updateFanGraphic(
      rotorId,
      stateId,
      pct
    ) {

      const rotor = el(rotorId);
      const state = el(stateId);

      if (!rotor || !state) {
        return;
      }

      const level =
        Number.isFinite(pct)
          ? pct
          : 0;

      const running =
        level > 0;


      if (running) {

        rotor.classList.add(
          "spinning"
        );

        rotor.style.animationDuration =
          fanSpinDuration(level);

        state.textContent =
          Math.round(level) + "%";

        state.classList.add(
          "running"
        );

      }
      else {

        rotor.classList.remove(
          "spinning"
        );

        rotor.style.animationDuration =
          "";

        state.textContent =
          "OFF";

        state.classList.remove(
          "running"
        );
      }
    }



    // SENSORTRAIN_PI_CPU_THERMAL_ARC_V1

    function updatePiCpuArcColor(tempC) {

      const arc = el("piCpuArc");

      if (!arc) return;

      let color = "#70808c";

      if (finite(tempC)) {

        if (tempC >= 80) {
          color = "#ef4444";  // Red
        }
        else if (tempC >= 75) {
          color = "#f28b39";  // Orange
        }
        else if (tempC >= 60) {
          color = "#e8c84d";  // Yellow
        }
        else {
          color = "#40c878";  // Green
        }
      }

      arc.style.stroke = color;
    }


    function updateDisplay(d) {

      setStatus(d);


      setGauge(
        "piCpu",
        "piCpuArc",
        cToF(
          d.temperatures?.pi_cpu_c
        ),
        32,
        212,
        1
      );

      updatePiCpuArcColor(d.temperatures?.pi_cpu_c);


      setGauge(
        "compute",
        "computeArc",
        cToF(
          d.temperatures?.compute_c
        ),
        32,
        212,
        1
      );

      setGauge(
        "exhaust",
        "exhaustArc",
        cToF(
          d.temperatures?.exhaust_c
        ),
        32,
        140,
        1
      );

      setGauge(
        "ambient",
        "ambientArc",
        cToF(
          d.environment?.temperature_c
        ),
        32,
        140,
        1
      );

      setGauge(
        "humidity",
        "humidityArc",
        d.environment?.humidity_pct,
        0,
        100,
        1
      );

      setGauge(
        "pressure",
        "pressureArc",
        d.environment?.pressure_hpa,
        950,
        1050,
        1
      );


      el("accelX").textContent =
        show(d.motion?.accel_x, 3);

      el("accelY").textContent =
        show(d.motion?.accel_y, 3);

      el("accelZ").textContent =
        show(d.motion?.accel_z, 3);


      el("gyroX").textContent =
        show(d.motion?.gyro_x, 3);

      el("gyroY").textContent =
        show(d.motion?.gyro_y, 3);

      el("gyroZ").textContent =
        show(d.motion?.gyro_z, 3);


      el("quatI").textContent =
        show(d.motion?.quat_i, 3);

      el("quatJ").textContent =
        show(d.motion?.quat_j, 3);

      el("quatK").textContent =
        show(d.motion?.quat_k, 3);

      el("quatReal").textContent =
        show(d.motion?.quat_real, 3);


      const wheel =
        d.wheel ?? {};

      el("wheelRpm").textContent =
        finite(wheel.rpm)
          ? wheel.rpm.toFixed(1)
          : "--";

      el("wheelSpeed").textContent =
        finite(wheel.speed_mps)
          ? (
              wheel.speed_mps
              * 40
              * 2.2369362920544
            ).toFixed(1)
          : "--";

      el("wheelCount").textContent =
        finite(wheel.count)
          ? String(Math.trunc(wheel.count))
          : "--";

      el("wheelDistance").textContent =
        finite(wheel.distance_m)
          ? (
              wheel.distance_m
              * 40
              / 1609.344
            ).toFixed(3)
          : "--";

      el("wheelRc").textContent =
        finite(wheel.rc_us)
          ? wheel.rc_us.toFixed(0)
          : "--";

      const wheelMarkerActive =
        wheel.marker_active === true;

      el("wheelMarker").textContent =
        wheelMarkerActive
          ? "MARKER"
          : "CLEAR";

      el("wheelLamp").className =
        "wheel-lamp " +
        (
          wheelMarkerActive
            ? "marker"
            : "clear"
        );


      el("firmware").textContent =
        d.firmware ?? "--";

      el("seq").textContent =
        d.seq ?? "--";


      el("uptime").textContent =
        finite(d.uptime_s)
          ? d.uptime_s.toFixed(0) + "s"
          : "--";

      el("age").textContent =
        finite(d.age_s)
          ? d.age_s.toFixed(1) + "s"
          : "--";


      telemetryReady =
        d.status !== "waiting" &&
        finite(d.age_s) &&
        d.age_s <= STALE_SECONDS;


      updateFanDisplay(d);
      setErrors(d.errors);
    }


    function setSystemLamp(
      elementId,
      state,
      age,
      error
    ) {

      const lamp =
        el(elementId);


      if (state === true) {

        lamp.className =
          "system-dot ok";

      }
      else if (state === false) {

        lamp.className =
          "system-dot bad";

      }
      else {

        lamp.className =
          "system-dot unknown";

      }


      let title = "";


      if (age !== null &&
          age !== undefined &&
          finite(age)) {

        title +=
          "Age: " +
          age.toFixed(1) +
          "s";
      }


      if (error) {

        if (title) {
          title += " · ";
        }

        title += error;
      }


      lamp.parentElement.title =
        title || "Status unavailable";
    }


    async function updateSystem() {

      try {

        const response =
          await fetch(
            "/api/system",
            {
              cache: "no-store"
            }
          );


        if (!response.ok) {

          throw new Error(
            "HTTP " +
            response.status
          );
        }


        const data =
          await response.json();


        setSystemLamp(
          "systemPico",
          data.pico?.ok,
          data.pico?.age_s,
          null
        );


        setSystemLamp(
          "systemBridge",
          data.bridge?.ok,
          data.bridge?.age_s,
          null
        );


        setSystemLamp(
          "systemCluster",
          data.cluster?.ok,
          data.cluster?.check_age_s,
          data.cluster?.error
        );

      }
      catch (err) {

        setSystemLamp(
          "systemPico",
          null,
          null,
          null
        );

        setSystemLamp(
          "systemBridge",
          null,
          null,
          null
        );

        setSystemLamp(
          "systemCluster",
          null,
          null,
          err.message
        );
      }
    }


    async function updateTelemetry() {

      try {

        const response =
          await fetch(
            "/api/telemetry",
            {
              cache: "no-store"
            }
          );


        if (!response.ok) {
          throw new Error(
            "HTTP " + response.status
          );
        }


        const data =
          await response.json();

        updateDisplay(data);

      }
      catch (err) {

        telemetryReady = false;
        refreshFanControls();

        el("statusBadge").textContent =
          "OFFLINE";

        el("statusBadge").className =
          "status-badge stale";
      }
    }


    async function sendFanLevel(
      fan,
      level
    ) {

      let intake =
        latestFanState.intake_pct;

      let exhaust =
        latestFanState.exhaust_pct;


      if (fan === "intake") {

        intake = level;

      }
      else if (fan === "exhaust") {

        exhaust = level;

      }
      else {

        return;

      }


      await sendFans(
        intake,
        exhaust
      );
    }


    async function sendFans(
      intake,
      exhaust
    ) {

      controlBusy = true;
      refreshFanControls();

      el("controlStatus").textContent =
        "Sending...";

      el("controlStatus").className =
        "control-status";


      try {

        const response =
          await fetch(
            "/api/control",
            {
              method: "POST",

              headers: {
                "Content-Type":
                  "application/json"
              },

              body: JSON.stringify({
                intake_pct: intake,
                exhaust_pct: exhaust
              })
            }
          );


        if (!response.ok) {

          const text =
            await response.text();

          throw new Error(
            "HTTP " +
            response.status +
            (text ? ": " + text : "")
          );
        }


        await response.json();


        pendingFanCommand = {
          intake_pct: intake,
          exhaust_pct: exhaust
        };


        el("controlStatus").textContent =
          "Waiting for Pico...";

      }
      catch (err) {

        pendingFanCommand = null;

        el("controlStatus").textContent =
          "Control failed";

        el("controlStatus").className =
          "control-status error";
      }
      finally {

        controlBusy = false;
        refreshFanControls();
      }
    }


    function selectHistory(metric) {

      const ids = {
        temperature:
          "chartCardTemperature",

        humidity:
          "chartCardHumidity",

        pressure:
          "chartCardPressure",

        acceleration:
          "chartCardAcceleration",

        gyro:
          "chartCardGyro",

        orientation:
          "chartCardOrientation",

        wheel:
          "chartCardWheel",

        fans:
          "chartCardFans"
      };


      for (const id of
        Object.values(ids)) {

        const card = el(id);

        if (card) {
          card.classList.remove(
            "selected"
          );
        }
      }


      const target =
        el(ids[metric]);

      if (target) {

        target.classList.add(
          "selected"
        );

        setTimeout(
          () => target.classList.remove(
            "selected"
          ),
          1200
        );
      }
    }


    function setHistoryRange(minutes) {

      historyMinutes = minutes;

      for (const value of [
        1,
        5,
        10,
        20,
        30
      ]) {

        el(
          "range" + value
        ).classList.toggle(
          "active",
          value === minutes
        );
      }

      updateHistory();
    }


    function formatTime(timestamp) {

      const date =
        new Date(timestamp * 1000);

      return date.toLocaleTimeString(
        [],
        {
          hour: "2-digit",
          minute: "2-digit"
        }
      );
    }


    function drawMiniChart(
      canvasId,
      configKey
    ) {

      const canvas =
        el(canvasId);

      const config =
        HISTORY[configKey];

      const samples =
        latestHistory;


      const rect =
        canvas.getBoundingClientRect();

      const ratio =
        window.devicePixelRatio || 1;

      const width =
        Math.max(
          240,
          rect.width
        );

      const height =
        Math.max(
          60,
          rect.height
        );


      canvas.width =
        Math.round(
          width * ratio
        );

      canvas.height =
        Math.round(
          height * ratio
        );


      const ctx =
        canvas.getContext("2d");


      ctx.setTransform(
        ratio,
        0,
        0,
        ratio,
        0,
        0
      );


      ctx.clearRect(
        0,
        0,
        width,
        height
      );


      const left = 39;
      const right = 5;
      const top = 5;
      const bottom = 16;

      const plotWidth =
        width - left - right;

      const plotHeight =
        height - top - bottom;


      if (!samples ||
          samples.length < 2) {

        ctx.fillStyle = document.documentElement.dataset.st15Theme === "light"
          ? "#536477" : "#667681";
        ctx.font = "10px system-ui";
        ctx.textAlign = "center";

        ctx.fillText(
          "Collecting...",
          width / 2,
          height / 2
        );

        return;
      }


      const startTime =
        samples[0].received_at;

      const endTime =
        samples[
          samples.length - 1
        ].received_at;


      let values = [];


      for (const series of
        config.series) {

        for (const sample of samples) {

          const value =
            series.value(sample);

          if (finite(value)) {
            values.push(value);
          }
        }
      }


      if (values.length === 0) {
        return;
      }


      let minValue =
        config.yMin !== undefined
          ? config.yMin
          : Math.min(...values);

      let maxValue =
        config.yMax !== undefined
          ? config.yMax
          : Math.max(...values);


      if (minValue === maxValue) {

        minValue -= 1;
        maxValue += 1;

      }
      else if (
        config.yMin === undefined &&
        config.yMax === undefined
      ) {

        const pad =
          (maxValue - minValue) *
          0.10;

        minValue -= pad;
        maxValue += pad;
      }


      function xFor(time) {

        if (endTime === startTime) {
          return left;
        }

        return left +
          (
            (time - startTime) /
            (endTime - startTime)
          ) *
          plotWidth;
      }


      function yFor(value) {

        return top +
          (
            1 -
            (
              (value - minValue) /
              (maxValue - minValue)
            )
          ) *
          plotHeight;
      }


      ctx.font = "9px system-ui";


      for (let i = 0; i <= 2; i++) {

        const fraction =
          i / 2;

        const y =
          top +
          fraction *
          plotHeight;

        const value =
          maxValue -
          fraction *
          (maxValue - minValue);


        ctx.strokeStyle = document.documentElement.dataset.st15Theme === "light"
          ? "#d7e1e9" : "#263039";
        ctx.lineWidth = 1;

        ctx.beginPath();
        ctx.moveTo(left, y);
        ctx.lineTo(
          left + plotWidth,
          y
        );
        ctx.stroke();


        ctx.fillStyle = document.documentElement.dataset.st15Theme === "light"
        ? "#536477" : "#687783";
        ctx.textAlign = "right";
        ctx.textBaseline = "middle";

        ctx.fillText(
          value.toFixed(1),
          left - 4,
          y
        );
      }


      ctx.fillStyle = document.documentElement.dataset.st15Theme === "light"
        ? "#536477" : "#687783";
      ctx.textBaseline = "top";

      ctx.textAlign = "left";

      ctx.fillText(
        formatTime(startTime),
        left,
        top + plotHeight + 4
      );


      ctx.textAlign = "right";

      ctx.fillText(
        formatTime(endTime),
        left + plotWidth,
        top + plotHeight + 4
      );


      config.series.forEach(
        (series, index) => {

          ctx.strokeStyle =
            SERIES_COLORS[
              index %
              SERIES_COLORS.length
            ];

          ctx.lineWidth = 1.5;
          ctx.lineJoin = "round";
          ctx.lineCap = "round";

          ctx.beginPath();


          let drawing = false;
          let previousY = null;


          for (const sample of samples) {

            const value =
              series.value(sample);

            const time =
              sample.received_at;


            if (
              !finite(value) ||
              !finite(time)
            ) {

              drawing = false;
              previousY = null;
              continue;
            }


            const x =
              xFor(time);

            const y =
              yFor(value);


            if (!drawing) {

              ctx.moveTo(x, y);
              drawing = true;

            }
            else if (series.step) {

              ctx.lineTo(
                x,
                previousY
              );

              ctx.lineTo(
                x,
                y
              );

            }
            else {

              ctx.lineTo(x, y);

            }


            previousY = y;
          }


          ctx.stroke();
        }
      );
    }


    function drawAllHistory() {

      drawMiniChart(
        "chartTemperature",
        "temperature"
      );

      drawMiniChart(
        "chartHumidity",
        "humidity"
      );

      drawMiniChart(
        "chartPressure",
        "pressure"
      );

      drawMiniChart(
        "chartAcceleration",
        "acceleration"
      );

      drawMiniChart(
        "chartGyro",
        "gyro"
      );

      drawMiniChart(
        "chartOrientation",
        "orientation"
      );

      drawMiniChart(
        "chartWheel",
        "wheel"
      );

    }


    async function updateHistory() {

      try {

        const response =
          await fetch(
            "/api/history?minutes=" +
            historyMinutes,
            {
              cache: "no-store"
            }
          );


        if (!response.ok) {
          throw new Error(
            "HTTP " + response.status
          );
        }


        const data =
          await response.json();


        latestHistory =
          data.samples || [];


        drawAllHistory();

      }
      catch (err) {

        console.error(
          "History unavailable:",
          err
        );
      }
    }


    window.addEventListener(
      "resize",
      drawAllHistory
    );


    updateTelemetry();
    updateSystem();
    updateHistory();

    setInterval(
      updateTelemetry,
      1000
    );


    setInterval(
      updateSystem,
      2000
    );

    setInterval(
      updateHistory,
      3000
    );


    // SENSORTRAIN_MOTOR_UI_V1

    let motorControlBusy = false;
    let latestMotorStatus = null;



    // SENSORTRAIN_V12_DUAL_WEB_V1

    function sensorBridgeReady(data) {
      return Boolean(
        data &&
        data.fresh === true &&
        data.serial_connected === true &&
        data.control_source_ok === true
      );
    }

    function sensorHubReady(hub) {
      return Boolean(
        hub &&
        hub.connected === true &&
        hub.gatt === "READY" &&
        hub.notify === "READY" &&
        hub.motor0 === true &&
        (hub.sign === 1 || hub.sign === -1)
      );
    }

    function sensorPairReady(data) {
      return Boolean(
        sensorBridgeReady(data) &&
        data.dual_status_fresh === true &&
        data.pair_ready === true &&
        data.fault_latched === false &&
        data.hubs &&
        sensorHubReady(data.hubs.locomotive) &&
        sensorHubReady(data.hubs.caboose)
      );
    }

    function renderSensorPair(prefix, data) {
      const pair = document.getElementById(
        prefix + "Pair"
      );

      const loco = document.getElementById(
        prefix + "Loco"
      );

      const caboose = document.getElementById(
        prefix + "Caboose"
      );

      if (!pair || !loco || !caboose) {
        return;
      }

      const live = Boolean(
        data &&
        data.fresh === true &&
        data.dual_status_fresh === true
      );

      const fault = Boolean(
        live && data.fault_latched === true
      );

      const ready = sensorPairReady(data);

      pair.textContent = fault
        ? "FAULT LATCHED"
        : ready
          ? "BOTH READY"
          : "WAITING";

      pair.classList.toggle("fault", fault);

      function hubLine(role, powerKey) {
        if (!live || !data.hubs) {
          return "STATUS UNAVAILABLE";
        }

        const hub = data.hubs[role];

        if (!hub) {
          return "STATUS UNAVAILABLE";
        }

        const status = sensorHubReady(hub)
          ? "READY"
          : hub.connected
            ? "MOTOR/LINK NOT READY"
            : "DISCONNECTED";

        const battery = Number.isFinite(
          hub.battery_pct
        )
          ? hub.battery_pct + "%"
          : "--";

        const commanded = Number.isFinite(
          data[powerKey]
        )
          ? data[powerKey] + "%"
          : "--";

        return status +
          " · BAT " + battery +
          " · CMD " + commanded;
      }

      loco.textContent = hubLine(
        "locomotive",
        "locomotive_power"
      );

      caboose.textContent = hubLine(
        "caboose",
        "caboose_power"
      );
    }

    function motorStatusGood(data) {
      return sensorPairReady(data);
    }



    // Individual locomotive/caboose status lights.
    function renderStDualPill(node, data, mode) {
      if (!node) return;
      const live = Boolean(
        data && data.fresh === true &&
        data.dual_status_fresh === true &&
        data.serial_connected === true &&
        data.control_source_ok === true &&
        data.hubs
      );
      for (const role of ["locomotive", "caboose"]) {
        const lamp = node.querySelector(
          '[data-role="' + role + '"]'
        );
        if (!lamp) continue;
        const hub = live ? data.hubs[role] : null;
        let state = "unknown";
        if (hub && typeof hub.connected === "boolean") {
          if (!hub.connected) {
            state = "bad";
          } else if (mode === "link") {
            state = hub.gatt === "READY" &&
              hub.notify === "READY" ? "ok" : "warn";
          } else {
            // Per-hub readiness; global fault warning remains separate.
            state = sensorHubReady(hub) ? "ok" : "warn";
          }
        }
        lamp.className = "st15-dual-unit st15-" + state;
        const label = role === "locomotive"
          ? "Locomotive" : "Caboose";
        const meaning = state === "ok" ? "ready" :
          state === "warn" ? "connecting/not ready" :
          state === "bad" ? "disconnected or faulted" :
          "unavailable";
        lamp.title = label + " " + mode + ": " + meaning;
        lamp.setAttribute("aria-label", lamp.title);
      }
    }

    function setMotorPill(
      id,
      good
    ) {

      const node = el(id);

      if (node.classList.contains("st15-dual-pill")) {
        renderStDualPill(node, latestMotorStatus,
          id === "motorLinkPill" ? "link" : "train");
        return;
      }

      node.classList.remove(
        "ok",
        "bad"
      );

      node.classList.add(
        good ? "ok" : "bad"
      );
    }


    function refreshMotorControls() {

      const ready =
        motorStatusGood(
          latestMotorStatus
        );

      for (const button of
        document.querySelectorAll(
          ".motor-button"
        )) {

        button.disabled =
          button.id === "motorStop"
            ? !sensorBridgeReady(latestMotorStatus)
            : (motorControlBusy || !ready);
      }
    }


    function renderMotorStatus(data) {

      latestMotorStatus = data;
      renderSensorPair("stMain", data);

      const fresh =
        data.fresh === true;

      const bridgeOk =
        fresh &&
        data.serial_connected === true &&
        data.control_source_ok === true;

      const bleOk =
        fresh &&
        data.ble === "READY";

      const hubOk =
        fresh &&
        data.hub === "CONNECTED";

      const linkOk =
        fresh &&
        data.gatt === "READY" &&
        data.notify === "READY";


      setMotorPill(
        "motorBridgePill",
        bridgeOk
      );

      setMotorPill(
        "motorBlePill",
        bleOk
      );

      setMotorPill(
        "motorHubPill",
        hubOk
      );

      setMotorPill(
        "motorLinkPill",
        linkOk
      );


      const speed =
        Number(
          data.speed ?? 0
        );

      el("motorSpeed").textContent =
        Math.abs(speed) + "%";


      if (speed > 0) {

        el("motorDirection").textContent =
          "FORWARD";
      }
      else if (speed < 0) {

        el("motorDirection").textContent =
          "REVERSE";
      }
      else {

        el("motorDirection").textContent =
          "STOPPED";
      }


      if (!fresh) {

        el("motorControlStatus").textContent =
          "Motor status stale";
      }
      else if (!bridgeOk) {

        el("motorControlStatus").textContent =
          "Motor bridge unavailable";
      }
      else if (!hubOk) {

        el("motorControlStatus").textContent =
          "Waiting for SensorTrain hub";
      }
      else if (!linkOk) {

        el("motorControlStatus").textContent =
          "Establishing LEGO link";
      }
      else if (!motorControlBusy) {

        el("motorControlStatus").textContent =
          motorStatusGood(data)
            ? "Both motors ready"
            : data.fault_latched === true
              ? "Safety fault latched"
              : "Waiting for dual-motor readiness";
      }


      refreshMotorControls();
    }


    async function updateMotorStatus() {

      try {

        const response =
          await fetch(
            "/api/motor/status",
            {
              cache: "no-store"
            }
          );

        if (!response.ok) {

          throw new Error(
            "HTTP " + response.status
          );
        }

        const data =
          await response.json();

        renderMotorStatus(
          data
        );
      }
      catch (err) {

        latestMotorStatus = null;
        renderSensorPair("stMain", null);

        setMotorPill(
          "motorBridgePill",
          false
        );

        setMotorPill(
          "motorBlePill",
          false
        );

        setMotorPill(
          "motorHubPill",
          false
        );

        setMotorPill(
          "motorLinkPill",
          false
        );

        el("motorDirection").textContent =
          "OFFLINE";

        el("motorSpeed").textContent =
          "--";

        el("motorControlStatus").textContent =
          "Motor API unavailable";

        refreshMotorControls();
      }
    }


    function updateMotorPower(value) {

      const power =
        Math.max(
          10,
          Math.min(
            100,
            Number(value) || 20
          )
        );

      el("motorPowerValue").textContent =
        power + "%";
    }


    function sendMotorDirection(direction) {

      const power =
        Math.max(
          10,
          Math.min(
            100,
            Number(
              el("motorPower").value
            ) || 20
          )
        );

      return sendMotorSpeed(
        direction < 0
          ? -power
          : power
      );
    }


    async function sendMotorSpeed(speed) {

      if (
        (speed !== 0 && motorControlBusy) ||
        !sensorBridgeReady(latestMotorStatus) ||
        (speed !== 0 &&
         !motorStatusGood(latestMotorStatus))
      ) {

        return;
      }

      motorControlBusy = true;
      refreshMotorControls();

      el("motorControlStatus").textContent =
        speed === 0
          ? "Sending STOP..."
          : "Sending command...";


      try {

        const response =
          await fetch(
            "/api/motor/control",
            {
              method: "POST",

              headers: {
                "Content-Type":
                  "application/json"
              },

              body: JSON.stringify({
                speed: speed
              })
            }
          );

        if (!response.ok) {

          const body =
            await response.text();

          throw new Error(
            body ||
            ("HTTP " + response.status)
          );
        }

        await response.json();

        el("motorControlStatus").textContent =
          speed === 0
            ? "STOP requested"
            : "Command accepted";
      }
      catch (err) {

        el("motorControlStatus").textContent =
          "Control failed";
      }
      finally {

        motorControlBusy = false;

        setTimeout(
          updateMotorStatus,
          300
        );

        setTimeout(
          updateMotorStatus,
          900
        );
      }
    }


    updateMotorStatus();

    setInterval(
      updateMotorStatus,
      1000
    );


  </script>


  <script>
  (() => {
    "use strict";

    const BATTERY_ID =
      "sensortrainHubBattery";

    function findLegoLinkAnchor() {

      const nodes =
        document.querySelectorAll(
          "span, div, strong"
        );

      for (const node of nodes) {

        if (node.children.length !== 0) {
          continue;
        }

        const text =
          (node.textContent || "")
            .trim()
            .replace(/\s+/g, " ");

        if (text === "PICO") {
          return node;
        }
      }

      return null;
    }


    function mountBattery() {

      let pill =
        document.getElementById(
          BATTERY_ID
        );

      if (pill) {
        return pill;
      }

      pill =
        document.createElement("div");

      pill.id = BATTERY_ID;

      pill.className =
        "st-hub-battery unknown";


        pill.innerHTML = `
          <span style="font-weight:700">
            BATTERIES
          </span>

          <span data-role="locomotive"
                style="display:inline-flex;
                       align-items:center;
                       gap:5px;
                       white-space:nowrap">

            <span class="st-battery-shell"
                  aria-hidden="true">
              <span class="st-battery-fill"></span>
            </span>

            <span>LOCO</span>
            <span class="st-hub-battery-value">—</span>
          </span>

          <span data-role="caboose"
                style="display:inline-flex;
                       align-items:center;
                       gap:5px;
                       white-space:nowrap">

            <span class="st-battery-shell"
                  aria-hidden="true">
              <span class="st-battery-fill"></span>
            </span>

            <span>CABOOSE</span>
            <span class="st-hub-battery-value">—</span>
          </span>
        `;

      const anchor =
        findLegoLinkAnchor();

      if (anchor) {

        const target =
          anchor.closest(
            ".status-pill, " +
            ".health-pill, " +
            ".pill"
          )
          || anchor.parentElement
          || anchor;

        target.insertAdjacentElement(
          "beforebegin",
          pill
        );
      }
      else {

        pill.classList.add(
          "floating"
        );

        document.body.appendChild(
          pill
        );
      }

      return pill;
    }



    function updateBattery(data) {

      const pill = mountBattery();

      pill.classList.remove(
        "unknown", "full", "good",
        "low", "critical"
      );

      pill.style.flexWrap = "wrap";

      const fresh =
        data && data.fresh === true;

      const hubs =
        data && data.hubs
          ? data.hubs
          : {};

      const roles = [
        ["locomotive", "Locomotive"],
        ["caboose", "Caboose"]
      ];

      for (const [role, label] of roles) {

        const item = pill.querySelector(
          '[data-role="' + role + '"]'
        );

        if (!item) {
          continue;
        }

        const hub = hubs[role] || {};
        const pct = hub.battery_pct;

        const valid =
          fresh &&
          hub.connected === true &&
          Number.isInteger(pct) &&
          pct >= 0 &&
          pct <= 100;

        const valueNode = item.querySelector(
          ".st-hub-battery-value"
        );

        const fill = item.querySelector(
          ".st-battery-fill"
        );

        valueNode.textContent =
          valid ? pct + "%" : "—";

        fill.style.width =
          valid ? pct + "%" : "0%";

        fill.style.backgroundColor =
          !valid ? "transparent" :
          pct < 20 ? "#e55353" :
          pct < 40 ? "#f0a331" :
          "#83d13d";

        item.title = valid
          ? label + " battery: " + pct + "%"
          : label + " battery unavailable";
      }
    }

    async function pollHubBattery() {

      try {

        const response =
          await fetch(
            "/api/motor/status",
            {
              cache: "no-store"
            }
          );

        if (!response.ok) {
          throw new Error(
            "HTTP " +
            response.status
          );
        }

        const data =
          await response.json();

        updateBattery(data);

      }
      catch (error) {

        updateBattery(null);
      }
    }


    mountBattery();

    pollHubBattery();

    setInterval(
      pollHubBattery,
      1000
    );

  })();
  </script>


  <script>
  /* SENSORTRAIN_BATTERY_HEADER_PLACEMENT_V1 */
  (() => {
    "use strict";

    function placeTrainBattery() {

      const pill =
        document.getElementById(
          "sensortrainHubBattery"
        );

      const picoDot =
        document.getElementById(
          "systemPico"
        );

      if (!pill || !picoDot) {
        return false;
      }

      const picoItem =
        picoDot.closest(
          ".system-item"
        );

      if (!picoItem) {
        return false;
      }

      const systemStrip =
        picoItem.parentElement;

      if (!systemStrip) {
        return false;
      }

      /* Stop using the floating fallback. */
      pill.classList.remove(
        "floating"
      );

      pill.style.position = "";
      pill.style.top = "";
      pill.style.right = "";
      pill.style.zIndex = "";

      /*
       * Put BATTERIES immediately
       * before PICO in the existing
       * system status strip.
       */
      systemStrip.insertBefore(
        pill,
        picoItem
      );

      return true;
    }

    if (!placeTrainBattery()) {

      const observer =
        new MutationObserver(() => {

          if (placeTrainBattery()) {
            observer.disconnect();
          }

        });

      observer.observe(
        document.body,
        {
          childList: true,
          subtree: true
        }
      );

      setTimeout(
        () => observer.disconnect(),
        5000
      );
    }

  })();
  </script>

<!-- SENSORTRAIN_134I_LIGHT_UI_V1 -->
<style>
  #st-headlight-controls {
    display: inline-flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
    padding: 7px 11px;
    border: 1px solid rgba(141, 209, 61, .45);
    border-radius: 12px;
    background: rgba(141, 209, 61, .08);
    color: inherit;
    font: inherit;
  }

  #st-headlight-controls .st-light-label {
    font-weight: 700;
    white-space: nowrap;
  }

  #st-headlight-controls .st-light-buttons {
    display: inline-flex;
    gap: 5px;
  }

  #st-headlight-controls button {
    border: 1px solid rgba(141, 209, 61, .6);
    border-radius: 7px;
    padding: 5px 9px;
    background: transparent;
    color: inherit;
    font: inherit;
    cursor: pointer;
  }

  #st-headlight-controls button[aria-pressed="true"] {
    background: #8dd13d;
    border-color: #8dd13d;
    color: #172310;
    font-weight: 700;
  }

  #st-headlight-controls button:disabled {
    opacity: .4;
    cursor: not-allowed;
  }

  #st-headlight-controls [data-light-info] {
    font-size: .78em;
    opacity: .85;
  }
</style>

<div id="st-headlight-controls"
     role="group"
     aria-label="Locomotive headlights">

  <span class="st-light-label">
    HEADLIGHTS
  </span>

  <span class="st-light-buttons">
    <button type="button"
            data-light-level="0"
            disabled>OFF</button>

    <button type="button"
            data-light-level="50"
            disabled>50%</button>

    <button type="button"
            data-light-level="100"
            disabled>100%</button>
  </span>

  <span data-light-info aria-live="polite">
    Checking headlight status...
  </span>
</div>

<script>
/* SENSORTRAIN_134I_LIGHT_WIDGET_V1 */
(() => {
  "use strict";

  const panel =
    document.getElementById("st-headlight-controls");

  if (!panel) return;

  const buttons = Array.from(
    panel.querySelectorAll("button[data-light-level]")
  );

  const info =
    panel.querySelector("[data-light-info]");

  let busy = false;
  let polling = false;
  let canSend = false;
  let notice = "";
  let noticeUntil = 0;

  function putNextToBattery() {
    const battery =
      document.getElementById("sensortrainHubBattery");

    if (
      battery &&
      battery.parentNode &&
      battery.nextElementSibling !== panel
    ) {
      battery.insertAdjacentElement("afterend", panel);
    }
  }

  function lightRecent(status) {
    return (
      typeof status.headlight_status_at === "number" &&
      typeof status.received_at === "number" &&
      status.received_at >= status.headlight_status_at &&
      status.received_at - status.headlight_status_at <= 3
    );
  }

  function freshLightAvailable(status, control) {
    if (!status || !control) return false;

    const hubs = status.hubs || {};

    return (
      status.fresh === true &&
      status.serial_connected === true &&
      status.control_source_ok === true &&
      status.dual_status_fresh === true &&
      status.pair_ready === true &&
      status.headlight_portb_ready === true &&
      status.fault_latched === false &&
      typeof status.dual_drive_active === "boolean" &&
      typeof status.drive_l_pending === "boolean" &&
      typeof status.drive_c_pending === "boolean" &&
      Number.isInteger(status.speed) && Math.abs(status.speed) <= 100 &&
      Number.isInteger(status.locomotive_power) && Math.abs(status.locomotive_power) <= 100 &&
      Number.isInteger(status.caboose_power) && Math.abs(status.caboose_power) <= 100 &&
      control.command === "speed" &&
      Number.isInteger(control.speed) && Math.abs(control.speed) <= 100 &&
      lightRecent(status) &&
      ["locomotive", "caboose"].every(role => {
        const hub = hubs[role] || {};

        return (
          hub.connected === true &&
          hub.gatt === "READY" &&
          hub.notify === "READY" &&
          hub.motor0 === true
        );
      })
    );
  }

  function setButtons(selectedLevel) {
    for (const button of buttons) {
      const selected =
        Number(button.dataset.lightLevel) === selectedLevel;

      button.setAttribute(
        "aria-pressed",
        selected ? "true" : "false"
      );

      button.disabled = !canSend || busy;
    }
  }

  async function refresh() {
    if (polling) return;

    polling = true;
    putNextToBattery();

    try {
      const responses = await Promise.all([
        fetch("/api/motor/status", {cache: "no-store"}),
        fetch("/api/motor/control", {cache: "no-store"})
      ]);

      if (responses.some(response => !response.ok)) {
        throw new Error("Status API unavailable");
      }

      const [status, control] = await Promise.all(
        responses.map(response => response.json())
      );

      canSend = freshLightAvailable(status, control);

      const commanded = status.headlight_commanded_pct;

      const validLevel =
        status.headlight_portb_ready === true &&
        status.fresh === true &&
        lightRecent(status) &&
        [0, 50, 100].includes(commanded);

      let label = validLevel
        ? "Commanded " + commanded +
          "% (not measured light output)"
        : "Headlights unavailable";

      if (
        validLevel &&
        status.headlight_last_write === "ACKNOWLEDGED"
      ) {
        label += " · BLE write accepted";
      } else if (
        validLevel &&
        status.headlight_last_write === "FAILED"
      ) {
        label += " · Last BLE write failed";
      }

      if (!canSend) {
        label += " · Controls unavailable: check link, telemetry, or safety";
      }

      if (Date.now() < noticeUntil) {
        label = notice + " · " + label;
      }

      info.textContent = label;
      setButtons(validLevel ? commanded : null);

    } catch (error) {
      canSend = false;
      info.textContent = "Headlight status unavailable";
      setButtons(null);

    } finally {
      polling = false;
    }
  }

  for (const button of buttons) {
    button.addEventListener("click", async () => {
      if (busy || !canSend) return;

      const level = Number(button.dataset.lightLevel);

      if (![0, 50, 100].includes(level)) return;

      busy = true;
      setButtons(null);

      try {
        const response = await fetch(
          "/api/headlight/control",
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json"
            },
            body: JSON.stringify({level})
          }
        );

        const payload = await response.json();

        if (
          !response.ok ||
          payload.accepted !== true
        ) {
          throw new Error(
            payload.error || "Request rejected"
          );
        }

        notice = "Requested " + level + "%";

      } catch (error) {
        notice = "Not sent: " + error.message;

      } finally {
        noticeUntil = Date.now() + 6000;
        busy = false;
        await refresh();
      }
    });
  }

  refresh();
  setInterval(refresh, 2500);
})();
</script>

<!-- SENSORTRAIN_134I_46_LAYOUT_ONLY -->
<style>
  #st-layout-headlight-row {
    display: flex;
    align-items: center;
    justify-content: flex-start;
    flex-wrap: wrap;
    gap: 8px;
    width: 100%;
    min-height: 38px;
    padding: 7px 0 2px;
    margin-top: 5px;
    border-top: 1px solid rgba(125,150,160,.22);
    box-sizing: border-box;
    grid-column: 1 / -1;
  }
  #st-layout-headlight-row #st-headlight-controls {
    position: static !important;
    top: auto !important;
    right: auto !important;
    bottom: auto !important;
    left: auto !important;
    transform: none !important;
    margin: 0 !important;
    box-shadow: none !important;
    max-width: 100%;
  }
</style>
<script>
(function () {
  'use strict';
  var scheduled = false;
  var expanded = false;

  function labelOf(el) {
    if (!el) return '';
    return (el.textContent || '').replace(/\s+/g, ' ').trim().toUpperCase();
  }

  function findControlPanel() {
    var nodes = document.querySelectorAll('h1,h2,h3,h4,span,div,strong');
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      if (node.children.length || labelOf(node) !== 'TRAIN CONTROL') continue;
      var parent = node.parentElement;
      while (parent && parent !== document.body &&
             !parent.classList.contains('page')) {
        var buttons = Array.from(parent.querySelectorAll('button'));
        var hasStop = buttons.some(function (b) {return labelOf(b) === 'STOP';});
        var hasForward = buttons.some(function (b) {return labelOf(b).indexOf('FORWARD') >= 0;});
        if (hasStop && hasForward) return parent;
        parent = parent.parentElement;
      }
    }
    return null;
  }

  function hideDuplicateBatteryChip(panel) {
    var nodes = document.querySelectorAll('span,div,strong,small,label');
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      if (node.children.length || labelOf(node) !== 'BATTERIES') continue;
      if (panel && panel.contains(node)) continue;
      var target = node.parentElement;
      for (var depth = 0; target && depth < 5; depth++, target = target.parentElement) {
        if (panel && panel.contains(target)) break;
        var txt = labelOf(target);
        if (txt.length > 260) break;
        if (txt.indexOf('LOCO') >= 0 && txt.indexOf('CABOOSE') >= 0) {
          target.style.display = 'none';
          break;
        }
      }
    }
  }

  function growPanelIfNeeded(panel) {
    if (expanded) return;
    var page = panel.closest('.page');
    if (!page || window.innerWidth < 1100) return;
    var rows = getComputedStyle(page).gridTemplateRows.split(/\s+/);
    if (rows.length !== 4 || !rows.every(function (v) {return /^\d+(\.\d+)?px$/.test(v);} )) return;
    var deficit = panel.scrollHeight - panel.clientHeight;
    if (deficit < 5) return;
    var add = Math.min(Math.ceil(deficit + 10), 75);
    if (parseFloat(rows[3]) - add < 240) return;
    page.style.gridTemplateRows = rows[0] + ' ' + rows[1] + ' ' +
       (parseFloat(rows[2]) + add) + 'px minmax(240px,1fr)';
    expanded = true;
  }

  function arrange() {
    var panel = findControlPanel();
    if (!panel) return;
    hideDuplicateBatteryChip(panel);
    var lights = document.getElementById('st-headlight-controls');
    if (!lights) return;
    var slot = document.getElementById('st-layout-headlight-row');
    if (!slot) {
      slot = document.createElement('div');
      slot.id = 'st-layout-headlight-row';
      panel.appendChild(slot);
    }
    if (lights.parentElement !== slot) slot.appendChild(lights);
    document.documentElement.setAttribute('data-st-layout', 'train-controls');
    requestAnimationFrame(function () {growPanelIfNeeded(panel);});
  }

  function schedule() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(function () {
      scheduled = false;
      arrange();
    });
  }

  function start() {
    arrange();
    new MutationObserver(schedule).observe(document.body, {
      childList: true, subtree: true
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, {once:true});
  } else {
    start();
  }
}());
</script>


<!-- SENSORTRAIN_134I_47_COMPACT_MIDDLE -->
<style>
  @media (min-width: 1120px) {
    /* The 3-column middle band becomes 2 columns: motion+travel / control. */
    .page:has(.st47-middle) {
      grid-template-rows: auto 122px 284px minmax(245px, 1fr) !important;
    }
    .st47-middle {
      display: grid !important;
      grid-template-columns: minmax(0, 44fr) minmax(0, 56fr) !important;
      grid-template-rows: minmax(0, 1fr) !important;
      gap: 9px !important;
      align-items: stretch !important;
      min-width: 0 !important;
      min-height: 0 !important;
    }
    .st47-middle > .st47-motion {
      grid-column: 1 !important;
      grid-row: 1 !important;
      min-width: 0 !important;
      min-height: 0 !important;
      overflow: hidden !important;
      display: flex !important;
      flex-direction: column !important;
      gap: 4px !important;
    }
    .st47-middle > .st47-control {
      grid-column: 2 !important;
      grid-row: 1 !important;
      min-width: 0 !important;
      min-height: 0 !important;
      overflow: hidden !important;
    }
    .st47-motion .st47-travel-source { display: none !important; }
    #st47-travel-strip {
      display: flex !important;
      align-items: center;
      justify-content: flex-start;
      flex-wrap: nowrap;
      gap: 7px;
      width: 100%;
      margin-top: auto;
      padding: 7px 0 0;
      border-top: 1px solid rgba(139,160,170,.25);
      min-width: 0;
      flex: 0 0 auto;
      font-variant-numeric: tabular-nums;
    }
    .st47-travel-title {
      color: #96a8b4;
      font-size: .65rem;
      font-weight: 850;
      letter-spacing: .10em;
      padding-right: 5px;
      flex: 0 0 auto;
    }
    .st47-metric {
      display: flex;
      align-items: baseline;
      flex: 1 1 0;
      gap: 4px;
      white-space: nowrap;
      min-width: 0;
    }
    .st47-metric small {
      color: #95a8b6;
      font-size: .59rem;
      font-weight: 800;
      letter-spacing: .035em;
    }
    .st47-metric strong {
      color: #edf4f8;
      font-size: .75rem;
      font-weight: 800;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    #st-layout-headlight-row {
      margin-top: auto !important;
      padding: 6px 2px 2px !important;
      min-height: 42px !important;
      border-top: 1px solid rgba(139,160,170,.25) !important;
      flex: 0 0 auto !important;
      display: flex !important;
      align-items: center !important;
      justify-content: flex-start !important;
      gap: 10px !important;
    }
    #st-layout-headlight-row #st-headlight-controls {
      position: static !important;
      transform: none !important;
      margin: 0 !important;
      flex-wrap: wrap;
      max-width: 100%;
    }
  }
  @media (min-width: 1120px) and (max-width: 1330px) {
    .st47-travel-title { display: none; }
    #st47-travel-strip { gap: 3px; }
    .st47-metric { gap: 3px; }
    .st47-metric strong { font-size: .67rem; }
  }
  @media (max-width: 1119px) {
    /* Tablet and narrow layouts keep their existing safe arrangement. */
    #st47-travel-strip { display: none !important; }
    .st47-travel-source { display: initial; }
  }
</style>
<script>
(function () {
  'use strict';
  var initialized = false;

  function cleanText(node) {
    return (node && node.textContent || '').replace(/\s+/g, ' ').trim();
  }
  function heading(label) {
    var nodes = document.querySelectorAll('h1,h2,h3,h4,h5,span,div,strong');
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].children.length === 0 &&
          cleanText(nodes[i]).toUpperCase() === label) return nodes[i];
    }
    return null;
  }
  function ancestorPath(node) {
    var a = [];
    while (node) { a.push(node); node = node.parentElement; }
    return a;
  }
  function directChild(parent, descendant) {
    while (descendant && descendant.parentElement !== parent) {
      descendant = descendant.parentElement;
    }
    return descendant && descendant.parentElement === parent ? descendant : null;
  }
  function commonParent(a, b, c) {
    var path = ancestorPath(a);
    for (var i = 0; i < path.length; i++) {
      if (path[i].contains(b) && path[i].contains(c)) return path[i];
    }
    return null;
  }
  function value(source, regex, suffix) {
    var result = source.match(regex);
    return result ? result[1] + (suffix || '') : '\u2014';
  }
  function createTravelStrip(travelPanel) {
    var strip = document.createElement('div');
    strip.id = 'st47-travel-strip';
    strip.setAttribute('aria-label', 'Travel telemetry');
    strip.innerHTML =
      '<span class="st47-travel-title">TRAVEL</span>' +
      '<span class="st47-metric"><small>RPM</small><strong data-st47="rpm">\u2014</strong></span>' +
      '<span class="st47-metric"><small>MPH</small><strong data-st47="mph">\u2014</strong></span>' +
      '<span class="st47-metric"><small>REVS</small><strong data-st47="revs">\u2014</strong></span>' +
      '<span class="st47-metric"><small>DIST</small><strong data-st47="dist">\u2014</strong></span>' +
      '<span class="st47-metric"><small>IR</small><strong data-st47="ir">\u2014</strong></span>';

    function set(key, text) {
      var target = strip.querySelector('[data-st47="' + key + '"]');
      if (target && target.textContent !== text) target.textContent = text;
    }
    function render() {
      // textContent joins adjacent HTML nodes without separators;
      // join text nodes explicitly so labels and values remain parseable.
      var tokens = [];
      var walker = document.createTreeWalker(travelPanel, NodeFilter.SHOW_TEXT);
      var node;
      while ((node = walker.nextNode())) {
        var token = cleanText(node);
        if (token) tokens.push(token);
      }
      var text = tokens.join(' ');
      set('rpm', value(text, /(-?\d+(?:\.\d+)?)\s*RPM\b/i));
      set('mph', value(text, /(-?\d+(?:\.\d+)?)\s*MPH\b/i));
      set('revs', value(text, /\bREVS\s*([\d,]+)/i));
      set('dist', value(text, /\bDIST\s*(-?\d+(?:\.\d+)?)/i));
      var ir = text.match(/\bIR\s*([\d.]+)\s*(?:\u00b5s|\u03bcs|us)\b/i);
      var state = text.match(/\b(CLEAR|BLOCKED|DETECTED)\b/i);
      set('ir', state ? state[1].toUpperCase() :
          (ir ? ir[1] + ' \u00b5s' : '\u2014'));
    }
    var pending = false;
    new MutationObserver(function () {
      if (pending) return;
      pending = true;
      requestAnimationFrame(function () { pending = false; render(); });
    }).observe(travelPanel, {subtree: true, childList: true, characterData: true});
    render();
    return strip;
  }
  function setup() {
    if (initialized || window.innerWidth < 1120) return;
    var motionTitle = heading('MOTION & ORIENTATION');
    var travelTitle = heading('TRAVEL');
    var controlTitle = heading('TRAIN CONTROL');
    if (!motionTitle || !travelTitle || !controlTitle) return;
    var middle = commonParent(motionTitle, travelTitle, controlTitle);
    if (!middle || !middle.classList || middle.classList.contains('page')) return;
    var motion = directChild(middle, motionTitle);
    var travel = directChild(middle, travelTitle);
    var control = directChild(middle, controlTitle);
    if (!motion || !travel || !control ||
        motion === travel || travel === control || control === motion) return;
    if (!control.querySelector('button') ||
        !document.getElementById('st-layout-headlight-row')) return;
    if (!middle.closest('.page')) return;

    // Real telemetry nodes and their existing update bindings stay in the DOM.
    // Do not clone or re-create any interactive motor/headlight elements.
    middle.classList.add('st47-middle');
    motion.classList.add('st47-motion');
    control.classList.add('st47-control');
    travel.classList.add('st47-travel-source');
    var strip = createTravelStrip(travel);
    motion.appendChild(travel);
    motion.appendChild(strip);
    initialized = true;
    document.documentElement.setAttribute('data-st47-layout', 'compact-middle');
  }
  function boot() {
    setup();
    if (initialized) return;
    // Some dashboard sections are populated after DOMContentLoaded.
    // Retry when their DOM nodes appear, without touching controls.
    var observer = new MutationObserver(function () {
      if (!initialized) setup();
      if (initialized) observer.disconnect();
    });
    observer.observe(document.body, {childList: true, subtree: true});
    window.addEventListener('resize', setup);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, {once: true});
  } else {
    boot();
  }
}());
</script>

<!-- SENSORTRAIN_15A1_DASHBOARD_SCRIPT -->
<script>
(function () {
  'use strict';
  function mount() {
    if (document.body.classList.contains('st15-dashboard')) return;
    document.body.classList.add('st15-dashboard');
    document.documentElement.setAttribute('data-st15-release','1.5A-1');
    var title = document.querySelector('.fan-panel .panel-title');
    var panel = document.querySelector('.fan-panel');
    var stack = panel && panel.querySelector('.train-control-stack');
    var header = document.querySelector('.header');
    var status = header && header.querySelector('.header-status');
    if (!title || !stack || !status) {
      document.body.classList.remove('st15-dashboard');
      console.error('SensorTrain 1.5A mount: expected nodes not found');
      return;
    }
    title.textContent = 'Drive Status';
    var placeholders = document.createElement('div');
    placeholders.className = 'st15-sensor-grid';
    placeholders.innerHTML =
      '<section class="st15-sensor-card" aria-label="ENS161 air quality sensor">' +
        '<div class="st15-sensor-heading"><span>AIR QUALITY · ENS161</span><span id="st15-aq-badge" class="st15-sensor-badge">WAITING</span></div>' +
        '<div class="st15-sensor-values"><span style="display:inline-block;white-space:nowrap">AQI <strong id="st15-aqi">—</strong></span> &nbsp; <span style="display:inline-block;white-space:nowrap">TVOC <strong id="st15-tvoc">—</strong> ppb</span> &nbsp; <span style="display:inline-block;white-space:nowrap">eCO<sub>2</sub> <strong id="st15-eco2">—</strong> ppm</span></div>' +
        '<div id="st15-aq-note" class="st15-sensor-note">Waiting for telemetry</div>' +
      '</section>' +
      '<section class="st15-sensor-card" aria-label="VEML7700 ambient light sensor">' +
        '<div class="st15-sensor-heading"><span>AMBIENT LIGHT · VEML7700</span><span id="st15-lux-badge" class="st15-sensor-badge">WAITING</span></div>' +
        '<div class="st15-sensor-values">Illuminance <strong id="st15-lux">—</strong> lux</div>' +
        '<div id="st15-lux-note" class="st15-sensor-note">Waiting for telemetry</div>' +
      '</section>';
    panel.appendChild(placeholders);

    // SensorTrain 1.5D: display only; no control requests or backend changes.
    var sensorRefreshBusy = false;
    var sensorField = function (id, value, decimals) {
      var node = document.getElementById(id);
      if (!node) return false;
      var valid = typeof value === 'number' && Number.isFinite(value);
      node.textContent = valid ? value.toFixed(decimals) : '—';
      return valid;
    };
    var sensorBadge = function (id, label) {
      var node = document.getElementById(id);
      if (node) node.textContent = label;
    };
    var sensorNote = function (id, label) {
      var node = document.getElementById(id);
      if (node) node.textContent = label;
    };
    var sensorUnavailable = function (label) {
      sensorField('st15-aqi', null, 0);
      sensorField('st15-tvoc', null, 0);
      sensorField('st15-eco2', null, 0);
      sensorField('st15-lux', null, 1);
      sensorBadge('st15-aq-badge', label);
      sensorBadge('st15-lux-badge', label);
      sensorNote('st15-aq-note', 'Waiting for fresh sensor data');
      sensorNote('st15-lux-note', 'Waiting for fresh sensor data');
    };
    async function refreshNewSensors() {
      if (sensorRefreshBusy) return;
      sensorRefreshBusy = true;
      var controller = new AbortController();
      var timeout = setTimeout(function () { controller.abort(); }, 4000);
      try {
        var response = await fetch('/api/telemetry', {
          cache: 'no-store', signal: controller.signal
        });
        if (!response.ok) throw new Error('Telemetry HTTP ' + response.status);
        var telemetry = await response.json();
        var age = telemetry && telemetry.age_s;
        if (!telemetry || telemetry.schema !== 'sensortrain.telemetry.v1' ||
            typeof age !== 'number' || !Number.isFinite(age) || age < 0 || age > 5) {
          sensorUnavailable('STALE');
          return;
        }
        var air = telemetry.air_quality || {};
        var light = telemetry.light || {};
        var a = sensorField('st15-aqi', air.aqi, 0);
        var t = sensorField('st15-tvoc', air.tvoc_ppb, 0);
        var c = sensorField('st15-eco2', air.eco2_ppm, 0);
        var l = sensorField('st15-lux', light.lux, 1);
        sensorBadge('st15-aq-badge', a && t && c ? 'LIVE' : 'WAITING');
        sensorBadge('st15-lux-badge', l ? 'LIVE' : 'WAITING');
        sensorNote('st15-aq-note', a && t && c ? 'Live sensor readings' : 'Waiting for air-quality readings');
        sensorNote('st15-lux-note', l ? 'Live sensor reading' : 'Waiting for light reading');
      } catch (error) {
        sensorUnavailable('OFFLINE');
      } finally {
        clearTimeout(timeout);
        sensorRefreshBusy = false;
      }
    }
    refreshNewSensors();
    setInterval(refreshNewSensors, 2000);

    /* Remove dashboard command surfaces from mouse and keyboard accessibility;
       controls remain as inert status-binding targets for the legacy JS. */
    ['motorReverse','motorStop','motorForward','motorPower',
     'fanIntake0','fanIntake100','fanExhaust0','fanExhaust100','fanAllOff'
    ].forEach(function (id) {
      var node = document.getElementById(id);
      if (!node) return;
      node.setAttribute('tabindex','-1');
      node.setAttribute('aria-hidden','true');
      node.removeAttribute('onclick');
      node.removeAttribute('oninput');
      if (node.tagName === 'BUTTON' || node.tagName === 'INPUT') node.disabled = true;
    });
    var lightPanel = document.getElementById('st-headlight-controls');
    if (lightPanel) {
      lightPanel.setAttribute('inert','');
      lightPanel.setAttribute('aria-hidden','true');
      lightPanel.querySelectorAll('button').forEach(function (button) {
        button.disabled = true;
        button.tabIndex = -1;
      });
    }

    var toolbar = document.createElement('div');
    toolbar.className = 'st15-toolbar';
    toolbar.innerHTML =
      '<nav class="st15-nav" aria-label="SensorTrain pages">' +
        '<a href="/" aria-current="page">Dashboard</a>' +
        '<a href="/tablet">Control Panel</a>' +
      '</nav>' +
      '<button id="st15-theme-toggle" class="st15-theme-toggle" type="button" aria-label="Switch dashboard theme">Light mode</button>';
    status.appendChild(toolbar);
    var themeButton = toolbar.querySelector('#st15-theme-toggle');
    function setTheme(theme) {
      theme = theme === 'light' ? 'light' : 'dark';
      document.documentElement.dataset.st15Theme = theme;
      themeButton.textContent = theme === 'light' ? 'Dark mode' : 'Light mode';
      themeButton.setAttribute('aria-pressed',String(theme === 'light'));
      try { localStorage.setItem('sensortrain_dashboard_theme',theme); } catch (_) {}
      if (typeof window.drawAllHistory === 'function') window.drawAllHistory();
    }
    var current = 'dark';
    try { current = localStorage.getItem('sensortrain_dashboard_theme') || 'dark'; } catch (_) {}
    setTheme(current);
    themeButton.addEventListener('click',function () {
      setTheme(document.documentElement.dataset.st15Theme === 'light' ? 'dark' : 'light');
    });
    /* Helpful for observational status; no API command is issued by this script. */
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded',mount,{once:true});
  else mount();
}());
</script>
<!-- SENSORTRAIN_15E_DASHBOARD_OVERHAUL -->
<style>
/* SENSORTRAIN_15E_DASHBOARD_CSS */
body.st15-dashboard .combined-motion-panel .combined-motion-grid {
  display: flex !important;
  flex-direction: column !important;
  align-items: stretch !important;
  gap: 5px !important;
  height: auto !important;
}
body.st15-dashboard .combined-motion-panel .combined-motion-grid > div {
  display: flex !important;
  flex-flow: row nowrap !important;
  align-items: center !important;
  gap: 11px !important;
  min-width: 0;
}
body.st15-dashboard .combined-motion-panel .motion-group-title {
  flex: 0 0 105px;
  margin: 0 !important;
  min-width: 0;
}
body.st15-dashboard .combined-motion-panel .axis-row {
  display: flex !important;
  flex-flow: row nowrap !important;
  align-items: baseline !important;
  gap: 4px !important;
  margin: 0 !important;
  min-width: 0;
  white-space: nowrap;
}
body.st15-dashboard .combined-motion-panel .axis-value { white-space: nowrap; }
body.st15-dashboard .combined-motion-panel .st15-sensor-grid {
  display: flex !important;
  flex-direction: column !important;
  gap: 5px !important;
  margin-top: 6px !important;
  min-width: 0;
}
body.st15-dashboard .combined-motion-panel .st15-sensor-card {
  display: flex !important;
  align-items: center !important;
  justify-content: space-between !important;
  gap: 12px !important;
  min-height: 34px;
  padding: 5px 9px !important;
  border-style: solid;
  min-width: 0;
}
body.st15-dashboard .combined-motion-panel .st15-sensor-heading {
  display: flex;
  align-items: center;
  flex: 0 0 190px;
  gap: 7px;
  min-width: 0;
  margin: 0 !important;
}
body.st15-dashboard .combined-motion-panel .st15-sensor-values {
  flex: 1 1 auto;
  min-width: 0;
  text-align: right;
  white-space: nowrap;
  margin: 0 !important;
}
body.st15-dashboard .combined-motion-panel .st15-sensor-note { display: none !important; }
body.st15-dashboard .st15-sensor-values sub,
body.st15-dashboard #st15e-sensor-history sub {
  font-size: .72em !important;
  line-height: 0;
  vertical-align: sub;
}
body.st15-dashboard #st15e-bottom-row {
  grid-column: 1 / -1;
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(0, 1fr) minmax(0, 1fr);
  gap: 6px;
  min-width: 0;
  min-height: 0;
}
body.st15-dashboard #st15e-bottom-row > .mini-chart {
  min-width: 0;
  min-height: 0;
  height: 100%;
  overflow: hidden;
}
body.st15-dashboard #st15e-sensor-history {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 6px;
}
body.st15-dashboard #st15e-sensor-history .st15e-hist-row {
  display: grid;
  grid-template-columns: 38px minmax(0,1fr) 48px;
  align-items: center;
  gap: 4px;
  min-width: 0;
  flex: 1 1 0;
}
body.st15-dashboard #st15e-sensor-history .st15e-hist-label {
  font-size: 9px;
  font-weight: 750;
  white-space: nowrap;
}
body.st15-dashboard #st15e-sensor-history .st15e-hist-value {
  font-size: 9px;
  font-variant-numeric: tabular-nums;
  text-align: right;
  white-space: nowrap;
}
body.st15-dashboard #st15e-sensor-history .st15e-hist-svg {
  width: 100%;
  height: 100%;
  min-height: 18px;
  max-height: 32px;
  overflow: hidden;
}
body.st15-dashboard #st15e-sensor-history .st15e-hist-empty {
  color: #8e9ba7;
  font-size: 9px;
  text-align: center;
}
@media (min-width: 1120px) {
  body.st15-dashboard { overflow-y: auto !important; }
  body.st15-dashboard .page:has(.st47-middle) {
    height: auto !important;
    min-height: 100vh !important;
    grid-template-rows: auto 122px minmax(365px,auto) minmax(435px,auto) !important;
  }
  body.st15-dashboard .mini-chart-grid {
    grid-template-rows: repeat(3,minmax(75px,1fr)) minmax(190px,2.1fr) !important;
    min-height: 420px !important;
  }
  body.st15-dashboard .combined-motion-panel { overflow: visible !important; }
}
@media (max-width: 1119px) {
  body.st15-dashboard .combined-motion-panel .combined-motion-grid > div,
  body.st15-dashboard .combined-motion-panel .st15-sensor-card {
    overflow-x: auto;
  }
  body.st15-dashboard .combined-motion-panel .motion-group-title { flex-basis: 100px; }
  body.st15-dashboard #st15e-bottom-row {
    grid-template-columns: minmax(0,1fr);
    grid-column: 1 / -1;
  }
  body.st15-dashboard #st15e-bottom-row > .mini-chart { min-height: 150px; }
  body.st15-dashboard #st15e-sensor-history { min-height: 210px; }
  body.st15-dashboard .mini-chart-grid { grid-template-rows: auto !important; }
}

</style>
<script>
/* SENSORTRAIN_15E_DASHBOARD_JS */
(function () {
  'use strict';
  var initialized = false;
  var historyTimer = null;
  var historyBusy = false;
  var svgNS = 'http://www.w3.org/2000/svg';
  var metrics = [
    {key: 'aqi', label: 'AQI', color: '#84c95b', digits: 0,
      read: function (s) {return s.air_quality && s.air_quality.aqi;}},
    {key: 'tvoc', label: 'TVOC', color: '#e6b05b', digits: 0,
      read: function (s) {return s.air_quality && s.air_quality.tvoc_ppb;}},
    {key: 'eco2', label: 'eCO<sub>2</sub>', color: '#6ca9ed', digits: 0,
      read: function (s) {return s.air_quality && s.air_quality.eco2_ppm;}},
    {key: 'lux', label: 'LUX', color: '#b19bec', digits: 1,
      read: function (s) {return s.light && s.light.lux;}}
  ];

  function make(tag, cls) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    return node;
  }

  function drawBars(svg, values, color) {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    var buckets = 20;
    var valid = values.filter(function (v) {
      return typeof v === 'number' && Number.isFinite(v) && v >= 0;
    });
    if (!valid.length) return;
    var maximum = Math.max(1, Math.max.apply(null, valid));
    for (var i = 0; i < buckets; i++) {
      var lo = Math.floor(i * values.length / buckets);
      var hi = Math.max(lo + 1, Math.floor((i + 1) * values.length / buckets));
      var group = values.slice(lo, hi).filter(function (v) {
        return typeof v === 'number' && Number.isFinite(v) && v >= 0;
      });
      if (!group.length) continue;
      var v = group.reduce(function (a, b) {return a + b;}, 0) / group.length;
      var h = Math.max(1, 28 * v / maximum);
      var rect = document.createElementNS(svgNS, 'rect');
      rect.setAttribute('x', String(2 + 8 * i));
      rect.setAttribute('y', String(30 - h));
      rect.setAttribute('width', '6');
      rect.setAttribute('height', String(h));
      rect.setAttribute('rx', '0.7');
      rect.setAttribute('fill', color);
      svg.appendChild(rect);
    }
  }

  function showMissing() {
    metrics.forEach(function (m) {
      var item = document.querySelector('[data-st15e-hist="' + m.key + '"]');
      if (!item) return;
      item.querySelector('.st15e-hist-value').textContent = '\u2014';
      drawBars(item.querySelector('svg'), [], m.color);
    });
  }

  async function refreshHistory() {
    // Defer to the new history graphs only after they mount successfully.
    if (window.__sensortrainE7HistoryMounted) return;
    if (historyBusy) return;
    historyBusy = true;
    var controller = new AbortController();
    var timeout = setTimeout(function () {controller.abort();}, 5000);
    try {
      var response = await fetch('/api/history?minutes=5', {
        cache: 'no-store', signal: controller.signal
      });
      if (!response.ok) throw new Error('History HTTP ' + response.status);
      var payload = await response.json();
      if (!payload || !Array.isArray(payload.samples)) throw new Error('Invalid history');
      var samples = payload.samples;
      var newest = samples[samples.length - 1];
      if (!newest || typeof newest.received_at !== 'number' ||
          !Number.isFinite(newest.received_at) ||
          (Date.now() / 1000 - newest.received_at) > 10 ||
          (Date.now() / 1000 - newest.received_at) < -5) {
        showMissing();
        return;
      }
      metrics.forEach(function (m) {
        var item = document.querySelector('[data-st15e-hist="' + m.key + '"]');
        if (!item) return;
        var values = samples.map(function (s) {return m.read(s || {});});
        var current = values[values.length - 1];
        item.querySelector('.st15e-hist-value').textContent =
          typeof current === 'number' && Number.isFinite(current) && current >= 0 ?
          current.toFixed(m.digits) : '\u2014';
        drawBars(item.querySelector('svg'), values, m.color);
      });
    } catch (err) {
      showMissing();
    } finally {
      clearTimeout(timeout);
      historyBusy = false;
    }
  }

  function buildHistory() {
    var card = make('div', 'mini-chart');
    card.id = 'st15e-sensor-history';
    card.setAttribute('aria-label', 'Five minute air quality and ambient light histograms');
    var header = make('div', 'mini-chart-header');
    var title = make('div', 'mini-chart-title');
    title.textContent = 'Sensor History';
    var time = make('div', 'mini-legend');
    time.textContent = '5 min';
    header.appendChild(title);
    header.appendChild(time);
    card.appendChild(header);
    metrics.forEach(function (m) {
      var row = make('div', 'st15e-hist-row');
      row.setAttribute('data-st15e-hist', m.key);
      var label = make('span', 'st15e-hist-label');
      label.innerHTML = m.label;
      var svg = document.createElementNS(svgNS, 'svg');
      svg.setAttribute('viewBox', '0 0 164 32');
      svg.setAttribute('preserveAspectRatio', 'none');
      svg.setAttribute('class', 'st15e-hist-svg');
      svg.setAttribute('role', 'img');
      svg.setAttribute('aria-label', m.key + ' recent histogram');
      var value = make('strong', 'st15e-hist-value');
      value.textContent = '\u2014';
      row.appendChild(label);
      row.appendChild(svg);
      row.appendChild(value);
      card.appendChild(row);
    });
    return card;
  }

  function boot(attempt) {
    if (initialized || !document.body || !document.body.classList.contains('st15-dashboard')) return;
    // Let the existing desktop compact-middle script finish finding its original heading.
    if (window.innerWidth >= 1120 &&
        !document.documentElement.hasAttribute('data-st47-layout') && attempt < 50) {
      setTimeout(function () {boot(attempt + 1);}, 50);
      return;
    }
    var motion = document.querySelector('.combined-motion-panel');
    var sensors = document.querySelector('.fan-panel .st15-sensor-grid');
    var fans = document.getElementById('chartCardFans');
    var wheel = document.getElementById('chartCardWheel');
    var chartGrid = fans && fans.closest('.mini-chart-grid');
    if (!motion || !sensors || !fans || !wheel || !chartGrid ||
        fans.parentElement !== chartGrid || wheel.parentElement !== chartGrid) {
      console.error('SensorTrain 1.5E: expected Dashboard nodes unavailable; layout unchanged');
      return;
    }
    initialized = true;
    var titles = document.querySelectorAll('.gauges .gauge-title');
    titles.forEach(function (t) {
      var old = (t.textContent || '').trim().toLowerCase();
      if (old === 'pi cpu') t.textContent = 'CPU';
      else if (old === 'compute') t.textContent = 'INTAKE';
    });
    var heading = motion.querySelector('.panel-title');
    if (heading) heading.textContent = 'SENSORS';
    motion.appendChild(sensors); // Move existing live nodes; keep all IDs and polling.
    var row = make('div');
    row.id = 'st15e-bottom-row';
    chartGrid.insertBefore(row, wheel);
    row.appendChild(wheel);
    row.appendChild(fans);
    row.appendChild(buildHistory());
    document.documentElement.setAttribute('data-st15e-dashboard', '1.5E');
    refreshHistory();
    historyTimer = setInterval(refreshHistory, 15000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {boot(0);}, {once: true});
  } else {
    boot(0);
  }
}());

</script>
<!-- SENSORTRAIN_15E4_SENSORS_ROWS -->
<style>
/* SENSORTRAIN_15E4_SENSORS_CSS: only the SENSORS panel */
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows {
  display: flex !important;
  flex-direction: column !important;
  align-items: stretch !important;
  gap: 5px !important;
  margin: 7px 0 0 !important;
  padding: 0 !important;
  width: 100%;
  min-width: 0;
}
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows > .st15e4-row {
  display: flex !important;
  flex-direction: row !important;
  align-items: center !important;
  flex-wrap: nowrap !important;
  justify-content: flex-start !important;
  gap: 8px !important;
  padding: 6px 9px !important;
  min-height: 30px;
  margin: 0 !important;
  min-width: 0;
  box-sizing: border-box;
  overflow: visible !important;
  white-space: nowrap;
  border-style: solid !important;
}
body.st15-dashboard .combined-motion-panel .st15e4-label {
  flex: 0 0 100px !important;
  width: 100px !important;
  min-width: 0;
  font-size: .67rem;
  font-weight: 800;
  letter-spacing: .07em;
  text-transform: uppercase;
  margin: 0 !important;
  line-height: 1.2;
}
body.st15-dashboard .combined-motion-panel .st15e4-measures {
  display: flex !important;
  flex-direction: row !important;
  align-items: baseline !important;
  flex-wrap: nowrap !important;
  gap: clamp(9px, 1.7vw, 22px) !important;
  flex: 1 1 auto;
  min-width: 0;
  white-space: nowrap;
  overflow-x: auto;
  scrollbar-width: thin;
}
body.st15-dashboard .combined-motion-panel .st15e4-metric {
  display: inline-flex !important;
  flex: 0 0 auto;
  align-items: baseline !important;
  gap: 4px !important;
  min-width: 0;
  margin: 0 !important;
  padding: 0 !important;
  white-space: nowrap;
}
body.st15-dashboard .combined-motion-panel .st15e4-metric-label {
  font-size: .7rem;
  font-weight: 800;
  letter-spacing: .03em;
}
body.st15-dashboard .combined-motion-panel .st15e4-metric-value {
  display: inline-flex !important;
  align-items: baseline !important;
  flex-direction: row !important;
  gap: 3px !important;
  margin: 0 !important;
  padding: 0 !important;
  white-space: nowrap;
}
body.st15-dashboard .combined-motion-panel .st15e4-metric-value :is(.axis-unit, .orientation-unit) {
  font-size: .63rem;
  opacity: .85;
}
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows .st15e4-travel #st47-travel-strip {
  display: flex !important;
  flex-direction: row !important;
  align-items: baseline !important;
  flex-wrap: nowrap !important;
  flex: 1 1 auto;
  gap: clamp(9px, 1.6vw, 24px) !important;
  padding: 0 !important;
  margin: 0 !important;
  border: 0 !important;
  border-top: 0 !important;
  box-shadow: none !important;
  min-width: 0;
  overflow-x: auto;
  white-space: nowrap;
}
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows #st47-travel-strip .st47-travel-title {
  display: none !important;
}
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows #st47-travel-strip .st47-metric {
  white-space: nowrap !important;
  flex: 0 0 auto;
}
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows > .st15-sensor-card .st15-sensor-heading {
  display: flex !important;
  align-items: center !important;
  gap: 10px !important;
  flex: 0 0 172px !important;
  width: 172px !important;
  margin: 0 !important;
  min-width: 0;
}
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows > .st15-sensor-card .st15-sensor-values {
  flex: 1 1 auto !important;
  margin: 0 !important;
  min-width: 0;
  text-align: left !important;
  white-space: nowrap !important;
  overflow-x: auto;
}
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows > .st15-sensor-card .st15-sensor-note {
  display: none !important;
}
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows sub {
  font-size: .70em !important;
  vertical-align: sub;
  line-height: 0;
}
body.st15-dashboard .combined-motion-panel .combined-motion-grid.st15e4-original-hidden,
body.st15-dashboard .combined-motion-panel .st15-sensor-grid.st15e4-original-hidden {
  display: none !important;
  border: none !important;
  margin: 0 !important;
  padding: 0 !important;
}
@media (max-width: 700px) {
  body.st15-dashboard .combined-motion-panel .st15e4-label {
    flex-basis: 88px !important;
    width: 88px !important;
  }
  body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows > .st15-sensor-card .st15-sensor-heading {
    flex-basis: 148px !important;
    width: 148px !important;
  }
}

</style>
<script>
/* SENSORTRAIN_15E4_SENSORS_JS: relocate bound nodes; no telemetry polling */
(function () {
  'use strict';
  var done = false;

  function elementWithId(candidates) {
    for (var i = 0; i < candidates.length; i++) {
      var node = document.getElementById(candidates[i]);
      if (node) return node;
    }
    return null;
  }

  function valueNode(el) {
    // Preserve the live <span id=...> and, where possible, its existing unit.
    var wrapper = el.closest('.axis-value, .orientation-value, .quat-value, .quaternion-value');
    if (wrapper && wrapper.querySelectorAll('[id]').length === 1) return wrapper;
    return el;
  }

  function makeRow(label, cls) {
    var row = document.createElement('section');
    row.className = 'st15-sensor-card st15e4-row ' + (cls || '');
    row.setAttribute('aria-label', label + ' sensor readings');
    var title = document.createElement('span');
    title.className = 'st15e4-label';
    title.textContent = label;
    row.appendChild(title);
    return row;
  }

  function makeMetric(label, boundValue) {
    var metric = document.createElement('span');
    metric.className = 'st15e4-metric';
    var axis = document.createElement('span');
    axis.className = 'st15e4-metric-label';
    axis.textContent = label;
    metric.appendChild(axis);
    var wrap = document.createElement('span');
    wrap.className = 'st15e4-metric-value';
    wrap.appendChild(valueNode(boundValue));
    metric.appendChild(wrap);
    return metric;
  }

  var specs = [
    {name: 'ACCELERATION', keys: [
      ['X', ['accelX']], ['Y', ['accelY']], ['Z', ['accelZ']]]},
    {name: 'GYROSCOPE', keys: [
      ['X', ['gyroX']], ['Y', ['gyroY']], ['Z', ['gyroZ']]]},
    {name: 'ORIENTATION', keys: [
      ['I', ['quatI', 'orientationI', 'oriI']],
      ['J', ['quatJ', 'orientationJ', 'oriJ']],
      ['K', ['quatK', 'orientationK', 'oriK']],
      ['R', ['quatR', 'quatReal', 'quatW', 'orientationR', 'orientationReal', 'oriR']]]}
  ];

  function boot(attempt) {
    if (done || !document.body || !document.body.classList.contains('st15-dashboard')) return;
    if (document.getElementById('st15e4-sensor-rows')) {done = true; return;}
    // v1.5E moves the existing live cards after the desktop layout initializes.
    if (!document.documentElement.hasAttribute('data-st15e-dashboard')) {
      if (attempt < 100) setTimeout(function () {boot(attempt + 1);}, 60);
      return;
    }

    var panel = document.querySelector('.combined-motion-panel');
    var grid = panel && panel.querySelector('.combined-motion-grid');
    var travel = panel && panel.querySelector('#st47-travel-strip');
    var sensorGrid = panel && panel.querySelector('.st15-sensor-grid');
    var aq = sensorGrid && sensorGrid.querySelector('#st15-aq-badge');
    var lux = sensorGrid && sensorGrid.querySelector('#st15-lux-badge');
    var title = panel && panel.querySelector('.panel-title');
    if (!panel || !grid || !travel || !sensorGrid || !aq || !lux || !title) {
      console.error('SensorTrain 1.5E-4: expected SENSORS nodes missing; unchanged');
      return;
    }

    // Resolve every live binding before touching the DOM: all-or-nothing.
    var resolved = [];
    var used = [];
    for (var i = 0; i < specs.length; i++) {
      var spec = specs[i];
      var values = [];
      for (var j = 0; j < spec.keys.length; j++) {
        var pair = spec.keys[j];
        var live = elementWithId(pair[1]);
        if (!live || !panel.contains(live) || used.indexOf(live) !== -1) {
          console.error('SensorTrain 1.5E-4: missing/duplicate bound sensor:', spec.name, pair[0]);
          return;
        }
        used.push(live);
        values.push({label: pair[0], node: live});
      }
      resolved.push({name: spec.name, values: values});
    }

    var cards = Array.prototype.slice.call(sensorGrid.querySelectorAll(':scope > .st15-sensor-card'));
    if (cards.length !== 2 || !cards[0].contains(aq) || !cards[1].contains(lux)) {
      console.error('SensorTrain 1.5E-4: expected two live sensor cards; unchanged');
      return;
    }

    var rows = document.createElement('div');
    rows.id = 'st15e4-sensor-rows';
    resolved.forEach(function (spec) {
      var row = makeRow(spec.name, 'st15e4-motion-row');
      var values = document.createElement('div');
      values.className = 'st15e4-measures';
      spec.values.forEach(function (field) {values.appendChild(makeMetric(field.label, field.node));});
      row.appendChild(values);
      rows.appendChild(row);
    });

    var travelRow = makeRow('TRAVEL', 'st15e4-travel');
    travelRow.appendChild(travel); // Keep its original MutationObserver/IDs intact.
    rows.appendChild(travelRow);
    cards.forEach(function (card, index) {
      var heading = card.querySelector('.st15-sensor-heading span');
      if (heading) heading.textContent = index === 0 ? 'AIR QUALITY' : 'AMBIENT LIGHT';
      card.setAttribute('aria-label', index === 0 ? 'Air quality sensor readings' : 'Ambient light sensor reading');
      card.classList.add('st15e4-row');
      rows.appendChild(card);
    });

    // Hide empty legacy wrappers and their old vertical rules; all live IDs were moved.
    grid.classList.add('st15e4-original-hidden');
    sensorGrid.classList.add('st15e4-original-hidden');
    title.insertAdjacentElement('afterend', rows);
    document.documentElement.setAttribute('data-st15e4-sensors', 'boxed-rows');
    done = true;
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {boot(0);}, {once: true});
  } else {
    boot(0);
  }
}());

</script>
<!-- SENSORTRAIN_15E5B_SENSORS_POLISH -->
<style>
/* SENSORS-only polish: identical text styling and one metric start column */
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows > .st15e4-row {
  justify-content: flex-start !important;
  gap: 8px !important;
}
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows .st15e4-label,
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows > .st15e4-row .st15-sensor-heading {
  flex: 0 0 100px !important;
  width: 100px !important;
  min-width: 100px !important;
  max-width: 100px !important;
  margin: 0 !important;
  padding: 0 !important;
}
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows > .st15e4-row .st15-sensor-heading > span:first-child {
  font-size: .67rem !important;
  font-weight: 800 !important;
  letter-spacing: .07em !important;
  line-height: 1.2 !important;
  color: inherit !important;
  white-space: nowrap !important;
}
/* Keep the status badges in the DOM for existing live updates; do not show them in rows. */
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows .st15-sensor-badge,
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows .st15-sensor-note {
  display: none !important;
}
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows .st15e4-measures,
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows #st47-travel-strip {
  display: flex !important;
  flex: 1 1 auto !important;
  align-items: baseline !important;
  justify-content: flex-start !important;
  gap: clamp(9px,1.7vw,22px) !important;
  margin: 0 !important;
  padding: 0 !important;
  min-width: 0 !important;
  overflow-x: visible !important;
  white-space: nowrap !important;
}
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows .st15e4-metric,
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows #st47-travel-strip .st47-metric {
  display: inline-flex !important;
  align-items: baseline !important;
  gap: 4px !important;
  margin: 0 !important;
  flex: 0 0 auto !important;
  white-space: nowrap !important;
  color: inherit !important;
}
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows .st15e4-metric-label,
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows #st47-travel-strip .st47-metric small {
  display: inline !important;
  font-size: .70rem !important;
  font-weight: 800 !important;
  letter-spacing: .03em !important;
  line-height: 1.2 !important;
  color: inherit !important;
}
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows .st15e4-metric-value,
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows .st15e4-metric-value strong,
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows #st47-travel-strip .st47-metric strong {
  font-size: .92rem !important;
  font-weight: 800 !important;
  line-height: 1.2 !important;
  color: inherit !important;
  font-variant-numeric: tabular-nums !important;
}
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows .st15e4-metric-value .axis-unit,
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows .st15e4-metric-value .st15e5b-unit {
  font-size: .63rem !important;
  font-weight: 600 !important;
  opacity: .85 !important;
}
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows sub {
  font-size: .70em !important;
  vertical-align: sub !important;
  line-height: 0 !important;
}
/* Eliminate stray horizontal scrollbar and status/pseudo markers. */
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows > .st15e4-row {
  overflow: visible !important;
}
body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows > .st15e4-row::after {
  content: none !important;
}
@media(max-width:700px) {
  body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows .st15e4-label,
  body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows > .st15e4-row .st15-sensor-heading {
    flex-basis: 100px !important;
    min-width: 100px !important;
    width: 100px !important;
  }
  body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows .st15e4-measures,
  body.st15-dashboard .combined-motion-panel #st15e4-sensor-rows #st47-travel-strip {
    overflow-x: auto !important;
    scrollbar-width: none;
  }
}

</style>
<script>
/* Preserve original live value IDs and update handlers; presentation only. */
(function () {
  'use strict';
  var finished = false;
  function metric(label, id, unit, subscript) {
    var existing = document.getElementById(id);
    if (!existing) return null;
    var m = document.createElement('span');
    m.className = 'st15e4-metric';
    var key = document.createElement('span');
    key.className = 'st15e4-metric-label';
    if (subscript) {
      key.appendChild(document.createTextNode('eCO'));
      var sub = document.createElement('sub');
      sub.textContent = '2';
      key.appendChild(sub);
    } else {
      key.textContent = label;
    }
    var value = document.createElement('span');
    value.className = 'st15e4-metric-value';
    value.appendChild(existing);
    if (unit) {
      var u = document.createElement('span');
      u.className = 'st15e5b-unit';
      u.textContent = unit;
      value.appendChild(u);
    }
    m.appendChild(key);
    m.appendChild(value);
    return m;
  }
  function boot(attempt) {
    if (finished || !document.body || !document.body.classList.contains('st15-dashboard')) return;
    var rows = document.getElementById('st15e4-sensor-rows');
    if (!rows) {
      if (attempt < 100) setTimeout(function () { boot(attempt + 1); }, 80);
      return;
    }
    if (rows.getAttribute('data-st15e5b') === '1') {finished = true; return;}
    var air = rows.querySelector('#st15-aqi')?.closest('.st15-sensor-card');
    var light = rows.querySelector('#st15-lux')?.closest('.st15-sensor-card');
    if (!air || !light || air === light || !air.querySelector('.st15-sensor-values') ||
        !light.querySelector('.st15-sensor-values') ||
        ['st15-aqi','st15-tvoc','st15-eco2','st15-lux'].some(function (id) {
          return !document.getElementById(id);
        })) {
      console.error('SensorTrain 1.5E-5B: expected SENSORS nodes missing; no changes');
      return;
    }
    function rebuild(card, specs) {
      var old = card.querySelector('.st15-sensor-values');
      var values = document.createElement('div');
      values.className = 'st15e4-measures st15e5b-measures';
      specs.forEach(function (s) { values.appendChild(metric(s[0],s[1],s[2],s[3])); });
      old.replaceWith(values);
    }
    rebuild(air, [
      ['AQI','st15-aqi','',false],
      ['TVOC','st15-tvoc','ppb',false],
      ['eCO2','st15-eco2','ppm',true]
    ]);
    rebuild(light, [['ILLUMINANCE','st15-lux','lux',false]]);
    rows.setAttribute('data-st15e5b', '1');
    finished = true;
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { boot(0); }, {once:true});
  } else boot(0);
}());

</script>
<!-- SENSORTRAIN_15E6_COMPACT_MIDDLE_ROW -->
<style>

/* SensorTrain 1.5E-6: desktop middle row follows the six boxed SENSORS rows.
   Both panels occupy the same CSS grid track; no fixed minimum row height. */
@media (min-width: 1120px) {
  body.st15-dashboard .page:has(.st47-middle) {
    grid-template-rows: auto 122px max-content minmax(435px,auto) !important;
  }
  body.st15-dashboard .st47-middle {
    height: auto !important;
    min-height: 0 !important;
    max-height: none !important;
    align-items: stretch !important;
  }
  body.st15-dashboard .st47-middle > .st47-motion,
  body.st15-dashboard .st47-middle > .st47-control {
    height: auto !important;
    min-height: 0 !important;
    max-height: none !important;
    align-self: stretch !important;
    box-sizing: border-box !important;
  }
  body.st15-dashboard .st47-middle .combined-motion-panel,
  body.st15-dashboard .st47-middle .fan-panel {
    min-height: 0 !important;
    max-height: none !important;
  }
}

</style>
<!-- SENSORTRAIN_15E7_COMPACT_CHARTS_HARDWARE -->
<style>

/* SensorTrain 1.5E-7 — compact, equal-width historical charts. */
body.st15-dashboard .mini-chart-grid {
  display: grid !important;
  grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
  grid-template-rows: none !important;
  grid-auto-flow: row !important;
  grid-auto-rows: 158px !important;
  height: auto !important;
  min-height: 0 !important;
  max-height: none !important;
  align-content: start !important;
  gap: 6px !important;
}
body.st15-dashboard .mini-chart-grid > .mini-chart {
  height: 158px !important;
  min-height: 0 !important;
  max-height: 158px !important;
  min-width: 0 !important;
  overflow: hidden !important;
  box-sizing: border-box !important;
}
body.st15-dashboard .mini-chart-grid > .mini-chart canvas.mini-canvas {
  display: block;
  width: 100% !important;
  max-width: 100% !important;
  height: calc(100% - 25px) !important;
  max-height: calc(100% - 25px) !important;
}
body.st15-dashboard .page:has(.st47-middle) {
  height: auto !important;
  min-height: 100vh !important;
  grid-template-rows: auto 122px max-content max-content !important;
}
body.st15-dashboard .fan-panel .panel-title {
  text-transform: uppercase;
}
/* Preserve original Fan Status graphics; only remove empty chart-grid space. */
body.st15-dashboard .fan-panel #chartCardFans {
  width: min(300px, 100%) !important;
  max-width: 100% !important;
  height: auto !important;
  min-height: 96px !important;
  max-height: none !important;
  align-self: start !important;
  margin: 6px 0 0 !important;
  box-sizing: border-box !important;
  overflow: visible !important;
}
body.st15-dashboard .fan-panel #chartCardFans .fan-light-area {
  display: flex !important;
  flex-flow: row nowrap !important;
  align-items: center !important;
  justify-content: space-around !important;
  height: auto !important;
  min-height: 72px !important;
  gap: 12px !important;
}
body.st15-dashboard .st47-middle .st47-control {
  overflow: visible !important;
}
body.st15-dashboard .st15e7-chart {
  display: flex;
  flex-direction: column;
  gap: 0;
}
body.st15-dashboard .st15e7-chart .mini-chart-header {
  flex: 0 0 23px;
  min-height: 23px;
  align-items: center;
}
body.st15-dashboard .st15e7-chart .st15e7-plot {
  position: relative;
  min-height: 0;
  flex: 1 1 auto;
  width: 100%;
}
body.st15-dashboard .st15e7-chart .st15e7-plot canvas {
  display: block;
  position: absolute;
  inset: 0;
  height: 100% !important;
  width: 100% !important;
}
body.st15-dashboard .st15e7-chart .st15e7-value {
  font-size: 10px;
  font-weight: 750;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
body.st15-dashboard .st15e7-chart sub {
  font-size: .72em;
  line-height: 0;
  vertical-align: sub;
}
@media (max-width: 1119px) {
  body.st15-dashboard .page:has(.st47-middle) {
    grid-template-rows: auto !important;
  }
  body.st15-dashboard .mini-chart-grid {
    grid-auto-rows: 155px !important;
  }
  body.st15-dashboard .mini-chart-grid > .mini-chart {
    height: 155px !important;
    min-height: 0 !important;
    max-height: 155px !important;
  }
}
@media (max-width: 700px) {
  body.st15-dashboard .mini-chart-grid {
    grid-template-columns: minmax(0,1fr) !important;
  }
}

</style>
<script>

/* SensorTrain 1.5E-7: relocate bound Fan Status, use existing history API. */
(function () {
  'use strict';
  var mounted = false;
  var busy = false;
  var charts = {};
  var specs = [
    {key:'aqi', title:'AQI', type:'line', color:'#7cc65c', digits:0,
     read:function(s){return s.air_quality && s.air_quality.aqi;}},
    {key:'lux', title:'AMBIENT LIGHT', type:'line', color:'#a68aec', digits:1,
     read:function(s){return s.light && s.light.lux;}},
    {key:'tvoc', title:'TVOC', type:'bar', color:'#e4a953', digits:0,
     read:function(s){return s.air_quality && s.air_quality.tvoc_ppb;}},
    {key:'eco2', title:'eCO<sub>2</sub>', type:'bar', color:'#70a9ed', digits:0,
     read:function(s){return s.air_quality && s.air_quality.eco2_ppm;}}
  ];

  function valid(v){return typeof v === 'number' && Number.isFinite(v) && v >= 0;}
  function bucketed(samples, spec){
    var total = spec.type === 'line' ? 60 : 30;
    var values = [];
    var labels = [];
    for (var i=0; i<total; i++) {
      var lo = Math.floor(i*samples.length/total);
      var hi = Math.max(lo+1, Math.floor((i+1)*samples.length/total));
      var good = [];
      for (var j=lo;j<hi && j<samples.length;j++) {
        var v = spec.read(samples[j] || {});
        if (valid(v)) good.push(v);
      }
      values.push(good.length ? good.reduce(function(a,b){return a+b;},0)/good.length : null);
      labels.push(i === 0 ? '-5m' : (i === total-1 ? 'now' : ''));
    }
    return {labels:labels, values:values};
  }

  function buildCard(grid, spec){
    var card = document.createElement('div');
    card.className = 'mini-chart st15e7-chart';
    card.id = 'st15e7-chart-' + spec.key;
    card.setAttribute('aria-label', spec.title.replace(/<[^>]*>/g,'') + ' five-minute history');
    var head = document.createElement('div');
    head.className = 'mini-chart-header';
    var title = document.createElement('div');
    title.className = 'mini-chart-title';
    title.innerHTML = spec.title;
    var value = document.createElement('span');
    value.className = 'st15e7-value';
    value.textContent = '—';
    value.setAttribute('data-st15e7-value', spec.key);
    head.appendChild(title);
    head.appendChild(value);
    var plot = document.createElement('div');
    plot.className = 'st15e7-plot';
    var canvas = document.createElement('canvas');
    canvas.setAttribute('role','img');
    canvas.setAttribute('aria-label',spec.key + ' ' + spec.type + ' chart');
    plot.appendChild(canvas);
    card.appendChild(head);
    card.appendChild(plot);
    grid.appendChild(card);

    var foreground = getComputedStyle(card).color || '#8495a6';
    var gridColor = 'rgba(128,145,160,0.18)';
    var y = {
      beginAtZero:spec.type === 'bar',
      grace:spec.type === 'line' ? '6%' : '10%',
      ticks:{color:foreground, maxTicksLimit:3, font:{size:9}},
      grid:{color:gridColor, drawTicks:false},
      border:{display:false}
    };
    var x = {
      ticks:{color:foreground,maxTicksLimit:2,font:{size:9}, autoSkip:true, maxRotation:0},
      grid:{display:false},
      border:{display:false}
    };
    charts[spec.key] = {
      node:value,
      chart:new Chart(canvas.getContext('2d'), {
        type:spec.type,
        data:{labels:[],datasets:[{
          label:spec.title.replace(/<[^>]*>/g,''), data:[],
          borderColor:spec.color,
          backgroundColor:spec.type === 'bar' ? spec.color : 'transparent',
          borderWidth:spec.type === 'line' ? 1.8 : 0,
          pointRadius:0,
          pointHoverRadius:2,
          tension:0.12,
          spanGaps:false,
          barPercentage:0.85,
          categoryPercentage:0.95
        }]},
        options:{
          responsive:true, maintainAspectRatio:false, animation:false,
          plugins:{legend:{display:false},tooltip:{enabled:true,intersect:false}},
          interaction:{mode:'index',intersect:false},
          scales:{x:x,y:y}
        }
      })
    };
  }

  function missing(){
    specs.forEach(function(spec){
      var obj = charts[spec.key];
      if (!obj) return;
      obj.node.textContent = '—';
      obj.chart.data.labels=[];
      obj.chart.data.datasets[0].data=[];
      obj.chart.update('none');
    });
  }

  async function refresh(){
    if (!mounted || busy) return;
    busy=true;
    var controller=new AbortController();
    var timeout=setTimeout(function(){controller.abort();},5000);
    try{
      var resp=await fetch('/api/history?minutes=5',{
        cache:'no-store',signal:controller.signal
      });
      if(!resp.ok) throw new Error('history HTTP '+resp.status);
      var payload=await resp.json();
      if (!payload || !Array.isArray(payload.samples) || !payload.samples.length)
        throw new Error('No historical samples');
      var samples=payload.samples;
      var newest=samples[samples.length-1];
      var now=Date.now()/1000;
      if (!newest || !valid(newest.received_at) || now-newest.received_at>10 ||
          now-newest.received_at < -5) throw new Error('History stale');
      specs.forEach(function(spec){
        var obj=charts[spec.key];
        if(!obj) return;
        var sample=spec.read(newest);
        obj.node.textContent=valid(sample) ? sample.toFixed(spec.digits) : '—';
        var values=bucketed(samples,spec);
        obj.chart.data.labels=values.labels;
        obj.chart.data.datasets[0].data=values.values;
        obj.chart.update('none');
      });
    }catch(err){missing();}
    finally{clearTimeout(timeout);busy=false;}
  }

  function boot(attempt){
    if(mounted || !document.body || !document.body.classList.contains('st15-dashboard')) return;
    if(typeof Chart !== 'function'){
      if(attempt<100) setTimeout(function(){boot(attempt+1);},75);
      return;
    }
    var panel=document.querySelector('.fan-panel');
    var title=panel && panel.querySelector('.panel-title');
    var fans=document.getElementById('chartCardFans');
    var wheel=document.getElementById('chartCardWheel');
    var history=document.getElementById('st15e-sensor-history');
    var row=document.getElementById('st15e-bottom-row');
    var grid=row && row.parentElement;
    if(!panel || !title || !fans || !wheel || !history || !row ||
       !grid || !grid.classList.contains('mini-chart-grid') ||
       !row.contains(fans) || !row.contains(wheel) || !row.contains(history)){
      if(attempt<100) setTimeout(function(){boot(attempt+1);},75);
      else console.error('SensorTrain 1.5E-7: expected chart nodes not found; existing layout retained');
      return;
    }

    // Construct charts first; preserve the old layout if Chart.js fails.
    try {
      specs.forEach(function(spec){buildCard(grid,spec);});
    }catch(error){
      Object.keys(charts).forEach(function(key){
        try{charts[key].chart.destroy();}catch(ignored){}
      });
      Array.prototype.slice.call(grid.querySelectorAll('.st15e7-chart'))
        .forEach(function(card){card.remove();});
      console.error('SensorTrain 1.5E-7 chart construction failed; existing layout retained',error);
      return;
    }

    // Existing fan DOM and its live updater stay intact; only move the node.
    grid.insertBefore(wheel,row);
    panel.appendChild(fans);
    title.textContent='Hardware Status';
    row.remove();
    document.documentElement.setAttribute('data-st15e7-dashboard','compact-charts');
    // Suppress legacy history fetches only after the new chart cards exist.
    window.__sensortrainE7HistoryMounted=true;
    mounted=true;
    refresh();
    setInterval(refresh,15000);
  }
  if(document.readyState==='loading'){
    document.addEventListener('DOMContentLoaded',function(){boot(0);},{once:true});
  }else boot(0);
}());

</script>
<!-- SENSORTRAIN_15E8_HEADER_HARDWARE_LINE_CHARTS -->
<style>

/* SENSORTRAIN 1.5E-8: Dashboard alignment and clean history layout. */
/* Replace the stretched/space-distributed page tracks with content-sized tracks. */
body.st15-dashboard {
  margin-top: 0 !important;
  padding-top: 0 !important;
  align-content: start !important;
  justify-content: normal !important;
  min-height: 0 !important;
  height: auto !important;
}
body.st15-dashboard .page:has(.st47-middle) {
  display: grid !important;
  grid-template-rows: auto auto auto auto !important;
  align-content: start !important;
  justify-content: stretch !important;
  row-gap: 6px !important;
  height: auto !important;
  min-height: 0 !important;
  padding-top: 5px !important;
  margin-top: 0 !important;
}
body.st15-dashboard .page:has(.st47-middle) > .header,
body.st15-dashboard .page:has(.st47-middle) > .gauges {
  margin-top: 0 !important;
  margin-bottom: 0 !important;
  align-self: start !important;
}
/* Each plot is an equal-width, fixed-height chart; older giant bottom-row rules lose. */
body.st15-dashboard .mini-chart-grid {
  display: grid !important;
  grid-template-columns: repeat(2,minmax(0,1fr)) !important;
  grid-template-rows: none !important;
  grid-auto-rows: 158px !important;
  grid-auto-flow: row !important;
  align-content: start !important;
  gap: 6px !important;
  min-height: 0 !important;
  height: auto !important;
  max-height: none !important;
}
body.st15-dashboard .mini-chart-grid > .mini-chart {
  height: 158px !important;
  min-height: 0 !important;
  max-height: 158px !important;
  box-sizing: border-box !important;
  min-width: 0 !important;
  overflow: hidden !important;
}
body.st15-dashboard .mini-chart-grid > .mini-chart canvas.mini-canvas {
  width: 100% !important;
  height: calc(100% - 24px) !important;
  max-height: calc(100% - 24px) !important;
}
/* Fan status: move the existing card and its original live graphic, do not rebuild it. */
body.st15-dashboard .fan-panel #chartCardFans.st15e8-relocated-fans {
  box-sizing: border-box !important;
  width: min(300px,100%) !important;
  max-width: 100% !important;
  min-height: 105px !important;
  height: 105px !important;
  max-height: 105px !important;
  flex: 0 0 auto !important;
  align-self: flex-start !important;
  margin: 5px 0 0 !important;
  overflow: hidden !important;
}
body.st15-dashboard .fan-panel #chartCardFans .fan-light-area {
  display: flex !important;
  flex-flow: row nowrap !important;
  justify-content: space-around !important;
  align-items: center !important;
  height: 76px !important;
  min-height: 0 !important;
  gap: 8px !important;
}
body.st15-dashboard .st47-middle .st47-control,
body.st15-dashboard .st47-middle .fan-panel {
  overflow: visible !important;
  min-height: 0 !important;
  height: auto !important;
  max-height: none !important;
}
/* New matching panels for independent sensor-history line charts. */
body.st15-dashboard .st15e8-line-card {
  display: flex !important;
  flex-direction: column !important;
  padding: 5px 7px !important;
  gap: 2px !important;
}
body.st15-dashboard .st15e8-line-card .mini-chart-header {
  display: flex;
  flex: 0 0 19px;
  align-items: center;
  justify-content: space-between;
  min-height: 19px;
}
body.st15-dashboard .st15e8-current {
  font-size: 10px;
  font-weight: 800;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
body.st15-dashboard .st15e8-aq-series {
  display: grid;
  grid-template-columns: 40px minmax(0,1fr) 64px;
  align-items: center;
  flex: 1 1 0;
  min-height: 0;
  column-gap: 6px;
}
body.st15-dashboard .st15e8-aq-series .label,
body.st15-dashboard .st15e8-aq-series .value {
  font-size: 10px;
  font-weight: 750;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
body.st15-dashboard .st15e8-aq-series .value { text-align: right; }
body.st15-dashboard .st15e8-aq-series svg {
  height: 29px;
  width: 100%;
  min-width: 0;
  display: block;
  overflow: hidden;
}
body.st15-dashboard .st15e8-aq-series sub {
  font-size: .72em;
  line-height: 0;
  vertical-align: sub;
}
body.st15-dashboard .st15e8-light-plot {
  display: flex;
  flex: 1 1 auto;
  min-height: 0;
  flex-direction: column;
}
body.st15-dashboard .st15e8-light-plot svg {
  flex: 1 1 auto;
  width: 100%;
  min-height: 0;
  display: block;
}
body.st15-dashboard .st15e8-light-plot .time-labels {
  display: flex;
  justify-content: space-between;
  font-size: 9px;
  opacity: .75;
  line-height: 1.2;
}
@media(max-width:700px) {
  body.st15-dashboard .mini-chart-grid {grid-template-columns:minmax(0,1fr) !important;}
}


</style>
<script>

/* SENSORTRAIN_15E8_CHARTS_JS: dashboard-only DOM move + historical line plots. */
(function () {
  'use strict';
  if (window.__sensortrainE8Started) return;
  window.__sensortrainE8Started = true;
  var booted = false;
  var busy = false;
  var timer = null;
  var NS = 'http://www.w3.org/2000/svg';
  var specs = [
    {key:'aqi', label:'AQI', color:'#79c85b', units:'', decimals:0,
     read:function(s){return s.air_quality && s.air_quality.aqi;}},
    {key:'tvoc', label:'TVOC', color:'#e5a84e', units:' ppb', decimals:0,
     read:function(s){return s.air_quality && s.air_quality.tvoc_ppb;}},
    {key:'eco2', label:'eCO<sub>2</sub>', color:'#70a8eb', units:' ppm', decimals:0,
     read:function(s){return s.air_quality && s.air_quality.eco2_ppm;}},
    {key:'lux', label:'LUX', color:'#a28deb', units:' lux', decimals:1,
     read:function(s){return s.light && s.light.lux;}}
  ];
  function ok(v){return typeof v==='number' && Number.isFinite(v) && v>=0;}
  function el(tag, cls){
    var node=document.createElement(tag);
    if(cls) node.className=cls;
    return node;
  }
  function svgEl(tag, attrs){
    var node=document.createElementNS(NS, tag);
    Object.keys(attrs).forEach(function(k){node.setAttribute(k,String(attrs[k]));});
    return node;
  }
  function renderPath(svg, numbers, color, w, h, withGuides){
    while(svg.firstChild) svg.removeChild(svg.firstChild);
    if(withGuides){
      [0.2,0.5,0.8].forEach(function(frac){
        svg.appendChild(svgEl('line', {
          x1:0,y1:(frac*h).toFixed(1),x2:w,y2:(frac*h).toFixed(1),
          stroke:'currentColor','stroke-opacity':'.13','stroke-width':'1',
          'vector-effect':'non-scaling-stroke'
        }));
      });
    }
    var finite=numbers.filter(ok);
    if(!finite.length) return;
    var low=Math.min.apply(null,finite),high=Math.max.apply(null,finite);
    var pad=Math.max((high-low)*0.12, high===low ? Math.max(1,high*0.02) : 0.01);
    low=Math.max(0,low-pad);
    high=high+pad;
    if(high<=low) high=low+1;
    var d='',count=numbers.length,wasGood=false;
    numbers.forEach(function(v,i){
      if(!ok(v)){wasGood=false;return;}
      var x=count>1 ? i*w/(count-1) : w/2;
      var y=4+(h-8)*(1-(v-low)/(high-low));
      d+=(wasGood?' L':' M')+x.toFixed(2)+' '+y.toFixed(2);
      wasGood=true;
    });
    svg.appendChild(svgEl('path', {
      d:d,fill:'none',stroke:color,'stroke-width':'2',
      'stroke-linejoin':'round','stroke-linecap':'round',
      'vector-effect':'non-scaling-stroke'
    }));
  }
  function buildCard(id, title){
    var card=el('div','mini-chart st15e8-line-card');
    card.id=id;
    var head=el('div','mini-chart-header');
    var h=el('div','mini-chart-title');
    h.textContent=title;
    head.appendChild(h);
    card.appendChild(head);
    return card;
  }
  function buildAir(){
    var card=buildCard('st15e8-air-chart','AIR QUALITY');
    specs.slice(0,3).forEach(function(spec){
      var row=el('div','st15e8-aq-series');
      row.setAttribute('data-st15e8-series',spec.key);
      var label=el('span','label');
      label.innerHTML=spec.label;
      var plot=svgEl('svg',{viewBox:'0 0 440 32',preserveAspectRatio:'none',
        role:'img','aria-label':spec.key+' 5 minute line graph'});
      var value=el('strong','value');
      value.textContent='—';
      row.appendChild(label);
      row.appendChild(plot);
      row.appendChild(value);
      card.appendChild(row);
    });
    return card;
  }
  function buildLux(){
    var card=buildCard('st15e8-light-chart','AMBIENT LIGHT');
    var current=el('strong','st15e8-current');
    current.textContent='—';
    card.querySelector('.mini-chart-header').appendChild(current);
    var wrap=el('div','st15e8-light-plot');
    var svg=svgEl('svg',{viewBox:'0 0 600 100',preserveAspectRatio:'none',
      role:'img','aria-label':'Ambient light in lux over five minutes'});
    wrap.appendChild(svg);
    var times=el('div','time-labels');
    times.innerHTML='<span>-5 min</span><span>now</span>';
    wrap.appendChild(times);
    card.appendChild(wrap);
    return card;
  }
  function missing(){
    specs.slice(0,3).forEach(function(spec){
      var row=document.querySelector('#st15e8-air-chart [data-st15e8-series="'+spec.key+'"]');
      if(!row) return;
      row.querySelector('.value').textContent='—';
      renderPath(row.querySelector('svg'),[],spec.color,440,32,false);
    });
    var lux=document.getElementById('st15e8-light-chart');
    if(lux){
      lux.querySelector('.st15e8-current').textContent='—';
      renderPath(lux.querySelector('svg'),[],specs[3].color,600,100,true);
    }
  }
  function downsample(samples,spec,count){
    var vals=[];
    for(var i=0;i<count;i++){
      var lo=Math.floor(i*samples.length/count);
      var hi=Math.max(lo+1,Math.floor((i+1)*samples.length/count));
      var bucket=[];
      for(var j=lo;j<hi && j<samples.length;j++){
        var v=spec.read(samples[j]||{});
        if(ok(v))bucket.push(v);
      }
      vals.push(bucket.length?bucket.reduce(function(a,b){return a+b;},0)/bucket.length:null);
    }
    return vals;
  }
  async function refresh(){
    if(!booted||busy)return;
    busy=true;
    var controller=new AbortController();
    var timeout=setTimeout(function(){controller.abort();},5000);
    try{
      var response=await fetch('/api/history?minutes=5',{
        cache:'no-store',signal:controller.signal
      });
      if(!response.ok) throw new Error('history HTTP '+response.status);
      var payload=await response.json();
      if(!payload||!Array.isArray(payload.samples)||!payload.samples.length)
        throw new Error('history missing');
      var samples=payload.samples;
      var newest=samples[samples.length-1];
      if(!newest || !ok(newest.received_at) ||
          Date.now()/1000-newest.received_at>10 ||
          newest.received_at-Date.now()/1000>5)
        throw new Error('history stale');
      specs.forEach(function(spec){
        var fresh=spec.read(newest);
        var text=ok(fresh)?fresh.toFixed(spec.decimals)+spec.units:'—';
        var series=downsample(samples,spec,75);
        if(spec.key==='lux'){
          var card=document.getElementById('st15e8-light-chart');
          if(!card)return;
          card.querySelector('.st15e8-current').textContent=text;
          renderPath(card.querySelector('svg'),series,spec.color,600,100,true);
        } else {
          var row=document.querySelector('#st15e8-air-chart [data-st15e8-series="'+spec.key+'"]');
          if(!row)return;
          row.querySelector('.value').textContent=text;
          renderPath(row.querySelector('svg'),series,spec.color,440,32,false);
        }
      });
    }catch(err){missing();}
    finally{clearTimeout(timeout);busy=false;}
  }
  function boot(attempt){
    if(booted || !document.body || !document.body.classList.contains('st15-dashboard'))return;
    var panel=document.querySelector('.fan-panel');
    var title=panel && panel.querySelector('.panel-title');
    var fans=document.getElementById('chartCardFans');
    var wheel=document.getElementById('chartCardWheel');
    var grid=document.querySelector('.mini-chart-grid');
    var oldHistory=document.getElementById('st15e-sensor-history');
    var oldRow=document.getElementById('st15e-bottom-row');
    if(!panel||!title||!fans||!wheel||!grid||(!oldRow&&!oldHistory)){
      if(attempt<120)setTimeout(function(){boot(attempt+1);},100);
      else console.error('SensorTrain 1.5E-8: Dashboard elements unavailable; no layout changes');
      return;
    }
    var lux=buildLux(),air=buildAir();
    // Relocate the ORIGINAL fan card. Its existing DOM bindings keep updating it.
    fans.classList.add('st15e8-relocated-fans');
    panel.appendChild(fans);
    title.textContent='Hardware Status';
    // Each chart is a normal, half-width mini-chart, the same width as Orientation.
    if(oldRow && oldRow.parentElement===grid){
      grid.insertBefore(wheel,oldRow);
      grid.insertBefore(lux,oldRow);   // Where Fan Status formerly sat, beside Wheel Speed.
      grid.insertBefore(air,oldRow);
      oldRow.remove();
    }else{
      grid.appendChild(wheel);
      grid.appendChild(lux);
      grid.appendChild(air);
      if(oldHistory)oldHistory.remove();
      if(oldRow)oldRow.remove();
    }
    // If an earlier chart attempt mounted, don't leave duplicate orphan panels.
    Array.prototype.slice.call(grid.querySelectorAll('.st15e7-chart')).forEach(function(node){
      try{
        var canvas=node.querySelector('canvas');
        if(canvas && typeof Chart==='function' && typeof Chart.getChart==='function'){
          var instance=Chart.getChart(canvas);
          if(instance)instance.destroy();
        }
      }catch(ignored){}
      node.remove();
    });
    // Suppress the old histogram fetch timer; existing non-sensor charts are unaffected.
    window.__sensortrainE7HistoryMounted=true;
    document.documentElement.setAttribute('data-st15e8-layout','hardware-and-line-charts');
    booted=true;
    refresh();
    timer=setInterval(refresh,15000);
  }
  if(document.readyState==='loading'){
    document.addEventListener('DOMContentLoaded',function(){boot(0);},{once:true});
  }else boot(0);
}());


</script>






</body>
</html>
"""



def check_cluster_ready():

    global cluster_check_at
    global cluster_ready
    global cluster_error

    now = time.time()

    # Cache the Kubernetes result briefly so browser
    # polling does not hammer the API server.
    if (
        cluster_check_at > 0
        and now - cluster_check_at
            < CLUSTER_CACHE_SECONDS
    ):

        return (
            cluster_ready,
            cluster_error,
            now - cluster_check_at
        )


    cluster_check_at = now


    url = (
        "https://kubernetes.default.svc/"
        "readyz"
    )

    token_path = (
        "/var/run/secrets/kubernetes.io/"
        "serviceaccount/token"
    )

    ca_path = (
        "/var/run/secrets/kubernetes.io/"
        "serviceaccount/ca.crt"
    )


    try:

        with open(token_path) as f:
            token = f.read().strip()


        context = ssl.create_default_context(
            cafile=ca_path
        )


        request = urllib.request.Request(
            url,
            headers={
                "Authorization":
                    "Bearer " + token
            }
        )


        with urllib.request.urlopen(
            request,
            context=context,
            timeout=2
        ) as response:

            body = (
                response.read()
                .decode()
                .strip()
            )


            cluster_ready = (
                response.status == 200
                and body == "ok"
            )


            if cluster_ready:

                cluster_error = None

            else:

                cluster_error = (
                    "unexpected readiness response"
                )


    except Exception as exc:

        cluster_ready = False
        cluster_error = str(exc)


    return (
        cluster_ready,
        cluster_error,
        0.0
    )



# SENSORTRAIN_TABLET_V1

# SENSORTRAIN_ADMIN_V1

ADMIN_PAGE = (r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport"
      content="width=device-width,initial-scale=1">

<title>SensorTrain Admin</title>

<style>
  :root {
    color-scheme: dark;
    --bg: #070b0d;
    --panel: #11181c;
    --panel2: #172126;
    --line: #29373d;
    --text: #eef5f1;
    --muted: #89979d;
    --green: #9cff00;
    --green2: #72bb00;
    --red: #ff5252;
    --amber: #ffba45;
  }

  * {
    box-sizing: border-box;
  }

  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family:
      -apple-system,
      BlinkMacSystemFont,
      "Segoe UI",
      sans-serif;
  }

  .page {
    width: min(1100px, calc(100% - 28px));
    margin: 0 auto;
    padding: 24px 0 50px;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 20px;
    margin-bottom: 22px;
  }

  h1 {
    margin: 0;
    font-size: clamp(1.6rem, 5vw, 2.4rem);
    letter-spacing: -.03em;
  }

  h1 span {
    color: var(--green);
  }

  h2 {
    margin: 0 0 18px;
    font-size: 1rem;
    text-transform: uppercase;
    letter-spacing: .09em;
    color: var(--muted);
  }

  a {
    color: var(--green);
    text-decoration: none;
    font-weight: 700;
  }

  .card {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 16px;
    padding: 20px;
    margin-bottom: 18px;
  }

  .health-grid {
    display: grid;
    grid-template-columns:
      repeat(auto-fit, minmax(125px, 1fr));
    gap: 10px;
  }

  .health {
    background: var(--panel2);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 14px;
  }

  .health-name {
    color: var(--muted);
    font-size: .72rem;
    text-transform: uppercase;
    letter-spacing: .08em;
    margin-bottom: 8px;
  }

  .health-state {
    display: flex;
    align-items: center;
    gap: 8px;
    font-weight: 800;
  }

  .dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--amber);
  }

  .health.ok .dot {
    background: var(--green);
    box-shadow: 0 0 10px rgba(156,255,0,.55);
  }

  .health.bad .dot {
    background: var(--red);
  }

  .health.ok .health-value {
    color: var(--green);
  }

  .health.bad .health-value {
    color: var(--red);
  }

  .control-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 18px;
  }

  .subpanel {
    background: var(--panel2);
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 18px;
  }

  .sub-title {
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: .08em;
    font-size: .75rem;
    font-weight: 800;
    margin-bottom: 14px;
  }

  .motor-readout {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    margin-bottom: 16px;
  }

  #motorDirection {
    font-size: 1.35rem;
    font-weight: 900;
  }

  #motorSpeed {
    color: var(--green);
    font-size: 2rem;
    font-weight: 900;
  }

  .button-row {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
  }

  button {
    appearance: none;
    border: 1px solid #3a4a51;
    border-radius: 10px;
    padding: 11px 15px;
    background: #202b30;
    color: var(--text);
    font: inherit;
    font-weight: 800;
    cursor: pointer;
  }

  button:hover:not(:disabled) {
    border-color: var(--green);
  }

  button:disabled {
    opacity: .35;
    cursor: not-allowed;
  }

  .go {
    background: #20331b;
    border-color: #4c792d;
  }

  .stop {
    background: #482020;
    border-color: #8e3b3b;
  }

  .fan-button.active {
    background: #294313;
    color: var(--green);
    border-color: var(--green2);
  }

  .slider-row {
    margin-top: 18px;
  }

  .slider-row input {
    width: 100%;
    accent-color: var(--green);
  }

  .slider-labels {
    display: flex;
    justify-content: space-between;
    color: var(--muted);
    margin-top: 5px;
    font-size: .8rem;
  }

  .status-line {
    margin-top: 15px;
    color: var(--muted);
    min-height: 1.4em;
  }

  .fan-row {
    margin-bottom: 18px;
  }

  .fan-row:last-child {
    margin-bottom: 0;
  }

  .fan-name {
    display: flex;
    justify-content: space-between;
    margin-bottom: 9px;
    font-weight: 800;
  }

  .fan-value {
    color: var(--green);
  }

  .danger {
    border-color: #643333;
  }

  .danger h2 {
    color: #ff9090;
  }

  .shutdown-button {
    width: 100%;
    padding: 16px;
    background: #491b1b;
    border-color: #a13a3a;
    color: #fff;
    font-size: 1rem;
  }

  .confirm {
    display: none;
    margin-top: 15px;
    background: #291515;
    border: 1px solid #703232;
    border-radius: 12px;
    padding: 16px;
  }

  .confirm.visible {
    display: block;
  }

  .confirm-text {
    margin-bottom: 14px;
    line-height: 1.45;
  }

  .confirm-shutdown {
    background: #a52424;
    border-color: #e15353;
  }

  #shutdownStatus {
    margin-top: 14px;
    font-weight: 700;
  }

  @media (max-width: 720px) {
    .control-grid {
      grid-template-columns: 1fr;
    }

    header {
      align-items: flex-start;
      flex-direction: column-reverse;
    }
  }


  /* SENSORTRAIN_ADMIN_BRAND_V1 */

  .admin-brand-row {
    display: flex;
    align-items: center;
    gap: 18px;
    min-width: 0;
    margin: 0;
  }

  .admin-brand-logo {
    display: block;
    width: clamp(145px, 25vw, 220px);
    max-width: 100%;
    height: auto;
    object-fit: contain;
    flex-shrink: 0;
  }

  .admin-brand-row .admin-control-title {
    font-size: clamp(1.15rem, 2.7vw, 1.85rem);
    font-weight: 850;
    letter-spacing: -0.025em;
    line-height: 1.15;
    color: var(--text);
    white-space: nowrap;
  }

  @media (max-width: 580px) {

    .page > header {
      flex-wrap: wrap;
    }

    .admin-brand-row {
      gap: 10px;
    }

    .admin-brand-logo {
      width: 135px;
    }

    .admin-brand-row .admin-control-title {
      font-size: 1.15rem;
    }
  }


  /* SENSORTRAIN_V12_DUAL_ADMIN_V1 */
  .admin-dual-summary {
    display: grid;
    gap: 8px;
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 12px;
    margin-bottom: 16px;
    background: #10191b;
  }
  .admin-pair { font-weight: 900; color: var(--amber); letter-spacing: .05em; }
  .admin-pair.ready { color: var(--green); }
  .admin-hub {
    display: grid;
    grid-template-columns: minmax(0,1fr) auto auto;
    gap: 12px;
    align-items: center;
    font-size: .83rem;
  }
  .admin-hub strong { color: var(--muted); font-size: .69rem; }
  .admin-hub span:last-child { font-variant-numeric: tabular-nums; }
  .admin-safety { color: var(--amber); font-size: .78rem; font-weight: 850; }
  .admin-safety.clear { color: var(--green); }
  #stopButton { border-color: #bf5b5b; }
  @media (max-width: 420px) {
    .admin-hub { grid-template-columns: 1fr auto; }
    .admin-hub span:last-child { grid-column: 2; }
  }

</style>
</head>

<body>

<div class="page">

<header>
  <div>
    <h1 class="admin-brand-row">
<img
          class="admin-brand-logo"
          src="data:image/webp;base64,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"
          alt="SensorTrain">
<span class="admin-control-title">Admin Control Panel</span>
</h1>
  </div>

  <a href="/">← Dashboard</a>
</header>


<section class="card">

  <h2>System Health</h2>

  <div class="health-grid">

    <div id="healthPico"
         class="health">
      <div class="health-name">Pico</div>
      <div class="health-state">
        <span class="dot"></span>
        <span class="health-value">CHECKING</span>
      </div>
    </div>

    <div id="healthBridge"
         class="health">
      <div class="health-name">Pico Bridge</div>
      <div class="health-state">
        <span class="dot"></span>
        <span class="health-value">CHECKING</span>
      </div>
    </div>

    <div id="healthCluster"
         class="health">
      <div class="health-name">MicroShift</div>
      <div class="health-state">
        <span class="dot"></span>
        <span class="health-value">CHECKING</span>
      </div>
    </div>

    <div id="healthMotor"
         class="health">
      <div class="health-name">Motor Bridge</div>
      <div class="health-state">
        <span class="dot"></span>
        <span class="health-value">CHECKING</span>
      </div>
    </div>

    <div id="healthBle"
         class="health">
      <div class="health-name">Both BLE Links</div>
      <div class="health-state">
        <span class="dot"></span>
        <span class="health-value">CHECKING</span>
      </div>
    </div>

    <div id="healthHub"
         class="health">
      <div class="health-name">Locomotive</div>
      <div class="health-state">
        <span class="dot"></span>
        <span class="health-value">CHECKING</span>
      </div>
    </div>

    <div id="healthGatt"
         class="health">
      <div class="health-name">Caboose</div>
      <div class="health-state">
        <span class="dot"></span>
        <span class="health-value">CHECKING</span>
      </div>
    </div>

    <div id="healthFault" class="health">
      <div class="health-name">Safety Clear</div>
      <div class="health-state">
        <span class="dot"></span>
        <span class="health-value">CHECKING</span>
      </div>
    </div>
  </div>

</section>


<section class="card">

  <h2>Train Control</h2>

  <div class="control-grid">

    <div class="subpanel">

      <div class="sub-title">Dual Motor Control</div>

      <div class="admin-dual-summary">
        <div id="adminPairStatus" class="admin-pair">CHECKING DUAL DRIVE</div>
        <div class="admin-hub">
          <strong>LOCOMOTIVE</strong>
          <span id="adminLocoState">CHECKING</span>
          <span id="adminLocoBattery">--</span>
        </div>
        <div class="admin-hub">
          <strong>CABOOSE</strong>
          <span id="adminCabooseState">CHECKING</span>
          <span id="adminCabooseBattery">--</span>
        </div>
        <div id="adminSafetyState" class="admin-safety">SAFETY: CHECKING</div>
      </div>

      <div class="motor-readout">
        <span id="motorDirection">WAITING</span>
        <span id="motorSpeed">--</span>
      </div>

      <div class="button-row">
        <button id="reverseButton"
                class="motor-button go">
          REVERSE
        </button>

        <button id="stopButton"
                class="motor-button stop">
          STOP
        </button>

        <button id="forwardButton"
                class="motor-button go">
          FORWARD
        </button>
      </div>

      <div class="slider-row">

        <input id="motorPower"
               type="range"
               min="10"
               max="100"
               step="5"
               value="20">

        <div class="slider-labels">
          <span>10%</span>
          <strong id="motorPowerValue">20%</strong>
          <span>100%</span>
        </div>

      </div>

      <div id="motorMessage"
           class="status-line">
        Waiting for motor bridge...
      </div>

    </div>


    <div class="subpanel">

      <div class="sub-title">Cooling Fans</div>

      <div class="fan-row">

        <div class="fan-name">
          <span>Intake</span>
          <span id="intakeValue"
                class="fan-value">--</span>
        </div>

        <div id="intakeButtons"
             class="button-row">
        </div>

      </div>


      <div class="fan-row">

        <div class="fan-name">
          <span>Exhaust</span>
          <span id="exhaustValue"
                class="fan-value">--</span>
        </div>

        <div id="exhaustButtons"
             class="button-row">
        </div>

      </div>

      <div class="button-row">
        <button id="fansOff"
                class="stop">
          BOTH OFF
        </button>
      </div>

      <div id="fanMessage"
           class="status-line">
        Waiting for Pico...
      </div>

    </div>

  </div>

</section>


<section class="card danger">

  <h2>System Power</h2>

  <p>
    Cleanly stop SensorTrain and shut down the Raspberry Pi 5.
  </p>

  <button id="armShutdown"
          class="shutdown-button">
    SHUT DOWN SENSORTRAIN
  </button>

  <div id="shutdownConfirm"
       class="confirm">

    <div class="confirm-text">
      This will command the train to STOP, turn both external
      fans OFF, and initiate a clean Pi shutdown.
    </div>

    <div class="button-row">

      <button id="cancelShutdown">
        CANCEL
      </button>

      <button id="confirmShutdown"
              class="confirm-shutdown">
        CONFIRM SHUTDOWN
      </button>

    </div>

  </div>

  <div id="shutdownStatus"></div>

</section>

</div>


<script>
  const el = id => document.getElementById(id);

  let latestMotor = null;

  let fanState = {
    intake_pct: 0,
    exhaust_pct: 0
  };

  let shutdownStarted = false;


  function setHealth(id, good) {

    const node = el(id);

    node.classList.remove(
      "ok",
      "bad"
    );

    node.classList.add(
      good ? "ok" : "bad"
    );

    node.querySelector(
      ".health-value"
    ).textContent =
      good ? "UP" : "DOWN";
  }


  function hubReady(data, role) {
    const h = data?.hubs?.[role];
    return Boolean(
      h && h.connected === true &&
      h.gatt === "READY" && h.notify === "READY" &&
      h.motor0 === true && (h.sign === 1 || h.sign === -1)
    );
  }

  function motorReady(data) {
    return Boolean(
      data && data.fresh === true &&
      data.serial_connected === true &&
      data.control_source_ok === true &&
      data.dual_status_fresh === true &&
      data.pair_ready === true &&
      data.fault_latched === false &&
      hubReady(data, "locomotive") &&
      hubReady(data, "caboose")
    );
  }

  function refreshMotorButtons() {
    const allowDrive = motorReady(latestMotor) && !shutdownStarted;
    el("forwardButton").disabled = !allowDrive;
    el("reverseButton").disabled = !allowDrive;
    // STOP is always offered; the server still decides if delivery is possible.
    el("stopButton").disabled = false;
  }

  async function refreshHealth() {

    try {

      const response =
        await fetch(
          "/api/system",
          {cache: "no-store"}
        );

      const data =
        await response.json();

      setHealth(
        "healthPico",
        data.pico?.ok === true
      );

      setHealth(
        "healthBridge",
        data.bridge?.ok === true
      );

      setHealth(
        "healthCluster",
        data.cluster?.ok === true
      );

    }
    catch (err) {

      setHealth("healthPico", false);
      setHealth("healthBridge", false);
      setHealth("healthCluster", false);
    }


    try {

      const response =
        await fetch(
          "/api/motor/status",
          {cache: "no-store"}
        );

      const data =
        await response.json();

      latestMotor = data;
      const bridgeOk = data.fresh === true &&
        data.serial_connected === true &&
        data.control_source_ok === true;
      const locoOk = data.fresh === true && hubReady(data, "locomotive");
      const cabooseOk = data.fresh === true && hubReady(data, "caboose");
      const safetyClear = data.fresh === true && data.fault_latched === false;
      const driveReady = motorReady(data);

      setHealth("healthMotor", bridgeOk);
      setHealth("healthBle", data.fresh === true && data.dual_status_fresh === true && data.pair_ready === true);
      setHealth("healthHub", locoOk);
      setHealth("healthGatt", cabooseOk);
      setHealth("healthFault", safetyClear);

      const batteryText = h => Number.isInteger(h?.battery_pct) && h.battery_pct >= 0
        ? h.battery_pct + "%" : "--";
      el("adminLocoState").textContent = locoOk ? "READY" : "NOT READY";
      el("adminCabooseState").textContent = cabooseOk ? "READY" : "NOT READY";
      el("adminLocoBattery").textContent = batteryText(data.hubs?.locomotive);
      el("adminCabooseBattery").textContent = batteryText(data.hubs?.caboose);
      const pair = el("adminPairStatus");
      pair.textContent = driveReady ? "DUAL DRIVE READY" : "DUAL DRIVE NOT READY";
      pair.classList.toggle("ready", driveReady);
      const safety = el("adminSafetyState");
      safety.textContent = data.fresh !== true ? "SAFETY: STATUS STALE" :
        data.fault_latched === true ? "SAFETY: FAULT LATCHED" :
        data.fault_latched === false ? "SAFETY: CLEAR" : "SAFETY: UNKNOWN";
      safety.classList.toggle("clear", safetyClear);

      const speed = Number(data.speed || 0);
      el("motorSpeed").textContent = Math.abs(speed) + "%";
      el("motorDirection").textContent = speed > 0 ? "FORWARD" :
        speed < 0 ? "REVERSE" : "STOPPED";
      el("motorMessage").textContent = driveReady ? "Both hubs ready" :
        data.fault_latched === true ? "Safety fault latched" :
        "Powered commands disabled until both hubs are ready";
      refreshMotorButtons();

    }
    catch (err) {

      latestMotor = null;

      setHealth("healthMotor", false);
      setHealth("healthBle", false);
      setHealth("healthHub", false);
      setHealth("healthGatt", false);
      setHealth("healthFault", false);
      el("adminPairStatus").textContent = "DUAL DRIVE UNKNOWN";
      el("adminPairStatus").classList.remove("ready");
      el("adminLocoState").textContent = "UNKNOWN";
      el("adminCabooseState").textContent = "UNKNOWN";
      el("adminLocoBattery").textContent = "--";
      el("adminCabooseBattery").textContent = "--";
      el("adminSafetyState").textContent = "SAFETY: UNKNOWN";
      el("adminSafetyState").classList.remove("clear");

      el("motorDirection").textContent =
        "OFFLINE";

      el("motorSpeed").textContent =
        "--";

      el("motorMessage").textContent =
        "Motor API unavailable";

      refreshMotorButtons();
    }
  }


  function buildFanButtons() {

    const levels =
      [0, 25, 50, 75, 100];

    for (const fan of
      ["intake", "exhaust"]) {

      const container =
        el(fan + "Buttons");

      for (const level of levels) {

        const button =
          document.createElement(
            "button"
          );

        button.textContent =
          level + "%";

        button.className =
          "fan-button";

        button.dataset.level =
          String(level);

        button.onclick = () =>
          setFan(
            fan,
            level
          );

        container.appendChild(
          button
        );
      }
    }
  }


  function renderFans() {

    el("intakeValue").textContent =
      fanState.intake_pct + "%";

    el("exhaustValue").textContent =
      fanState.exhaust_pct + "%";


    for (const fan of
      ["intake", "exhaust"]) {

      const value =
        fanState[
          fan + "_pct"
        ];

      el(fan + "Buttons")
        .querySelectorAll(
          ".fan-button"
        )
        .forEach(button => {

          button.classList.toggle(
            "active",
            Number(
              button.dataset.level
            ) === value
          );

          button.disabled =
            shutdownStarted;
        });
    }

    el("fansOff").disabled =
      shutdownStarted;
  }


  async function refreshFans() {

    try {

      const response =
        await fetch(
          "/api/telemetry",
          {cache: "no-store"}
        );

      const data =
        await response.json();

      if (data.fans) {

        fanState = {
          intake_pct:
            Number(
              data.fans.intake_pct || 0
            ),

          exhaust_pct:
            Number(
              data.fans.exhaust_pct || 0
            )
        };

        renderFans();

        el("fanMessage").textContent =
          "Ready";
      }

    }
    catch (err) {

      el("fanMessage").textContent =
        "Telemetry unavailable";
    }
  }


  async function sendFans(
    intake,
    exhaust
  ) {

    el("fanMessage").textContent =
      "Sending...";

    const response =
      await fetch(
        "/api/control",
        {
          method: "POST",

          headers: {
            "Content-Type":
              "application/json"
          },

          body: JSON.stringify({
            intake_pct: intake,
            exhaust_pct: exhaust
          })
        }
      );

    if (!response.ok) {
      throw new Error(
        await response.text()
      );
    }

    fanState = {
      intake_pct: intake,
      exhaust_pct: exhaust
    };

    renderFans();

    el("fanMessage").textContent =
      "Command accepted";

    setTimeout(
      refreshFans,
      700
    );
  }


  async function setFan(
    fan,
    level
  ) {

    try {

      let intake =
        fanState.intake_pct;

      let exhaust =
        fanState.exhaust_pct;

      if (fan === "intake") {
        intake = level;
      }
      else {
        exhaust = level;
      }

      await sendFans(
        intake,
        exhaust
      );

    }
    catch (err) {

      el("fanMessage").textContent =
        "Control failed";
    }
  }


  async function sendMotor(speed) {

    if (
      speed !== 0 &&
      (!motorReady(latestMotor) || shutdownStarted)
    ) {
      return;
    }

    el("motorMessage").textContent =
      speed === 0
        ? "Sending STOP..."
        : "Sending command...";

    try {

      const response =
        await fetch(
          "/api/motor/control",
          {
            method: "POST",

            headers: {
              "Content-Type":
                "application/json"
            },

            body: JSON.stringify({
              speed: speed
            })
          }
        );

      if (!response.ok) {
        throw new Error(
          await response.text()
        );
      }

      el("motorMessage").textContent =
        speed === 0
          ? "STOP requested"
          : "Command accepted";

      setTimeout(
        refreshHealth,
        400
      );

    }
    catch (err) {

      el("motorMessage").textContent =
        "Control failed";
    }
  }


  function selectedPower() {

    return Math.max(
      10,
      Math.min(
        100,
        Number(
          el("motorPower").value
        ) || 20
      )
    );
  }


  async function requestShutdown() {

    shutdownStarted = true;

    refreshMotorButtons();
    renderFans();

    el("armShutdown").disabled =
      true;

    el("cancelShutdown").disabled =
      true;

    el("confirmShutdown").disabled =
      true;

    el("shutdownStatus").textContent =
      "Requesting clean shutdown...";

    try {

      const response =
        await fetch(
          "/api/admin/shutdown",
          {
            method: "POST",

            headers: {
              "Content-Type":
                "application/json"
            },

            body: JSON.stringify({
              confirm:
                "SHUTDOWN SENSORTRAIN"
            })
          }
        );

      if (!response.ok) {
        throw new Error(
          await response.text()
        );
      }

      el("shutdownStatus").textContent =
        "Shutdown request accepted. " +
        "Motor STOP and fan OFF commands issued.";

    }
    catch (err) {

      shutdownStarted = false;

      refreshMotorButtons();
      renderFans();

      el("armShutdown").disabled =
        false;

      el("cancelShutdown").disabled =
        false;

      el("confirmShutdown").disabled =
        false;

      el("shutdownStatus").textContent =
        "Shutdown request failed.";
    }
  }


  buildFanButtons();


  el("motorPower").oninput =
    event => {

      el("motorPowerValue").textContent =
        event.target.value + "%";
    };


  el("reverseButton").onclick =
    () => sendMotor(
      -selectedPower()
    );


  el("stopButton").onclick =
    () => sendMotor(0);


  el("forwardButton").onclick =
    () => sendMotor(
      selectedPower()
    );


  el("fansOff").onclick =
    () => sendFans(0, 0);


  el("armShutdown").onclick =
    () => {

      el("shutdownConfirm")
        .classList.add(
          "visible"
        );
    };


  el("cancelShutdown").onclick =
    () => {

      el("shutdownConfirm")
        .classList.remove(
          "visible"
        );
    };


  el("confirmShutdown").onclick =
    requestShutdown;


  refreshHealth();
  refreshFans();

  setInterval(
    refreshHealth,
    1500
  );

  setInterval(
    refreshFans,
    1500
  );
</script>

</body>
</html>
""").replace("</body>", '\n<!-- SENSORTRAIN_ADMIN_CLEAR_FAULT_UI_V1 -->\n<style id="st-admin-clear-fault-style">\n  #st-admin-fault-recovery { margin: 1rem 0; }\n  #st-admin-fault-recovery h2 { margin-top: 0; }\n  #st-admin-fault-recovery .st-fault-controls {\n    display: flex; align-items: center; flex-wrap: wrap; gap: 0.8rem;\n    margin-top: 0.85rem;\n  }\n  #st-admin-clear-fault {\n    font: inherit; font-weight: 700; border-radius: 7px;\n    padding: 0.72rem 1.1rem; cursor: pointer;\n    border: 1px solid #b88733; color: #fff; background: #a36217;\n  }\n  #st-admin-clear-fault:not(:disabled):hover { filter: brightness(1.13); }\n  #st-admin-clear-fault:disabled { cursor: not-allowed; opacity: .52; }\n  #st-admin-fault-status, #st-admin-fault-result { font-size: .92em; }\n  #st-admin-fault-result { margin-top: 0.7rem; white-space: normal; overflow-wrap: anywhere; }\n  #st-admin-fault-result[data-kind="error"] { color: #cb443d; }\n  #st-admin-fault-result[data-kind="ok"] { color: #21804e; }\n  #st-admin-fault-recovery .st-fault-note { opacity: .8; font-size: .88em; margin-top: .7rem; }\n</style>\n<script id="st-admin-clear-fault-script">\n(function(){\n  \'use strict\';\n  if (window.__sensortrainAdminClearFaultV1) return;\n  window.__sensortrainAdminClearFaultV1 = true;\n\n  var button = null;\n  var statusLine = null;\n  var resultLine = null;\n  var busy = false;\n  // SENSORTRAIN_ADMIN_CLEAR_FAULT_RESULT_LIVE_V2\n  var awaitingReset = false;\n  var resetRequestedAt = 0;\n\n  function result(text, kind) {\n    if (!resultLine) return;\n    resultLine.textContent = text || \'\';\n    resultLine.dataset.kind = kind || \'\';\n  }\n\n  function createUI(){\n    if (document.getElementById(\'st-admin-fault-recovery\')) return true;\n    var shutdownButton = document.getElementById(\'armShutdown\');\n    var anchor = shutdownButton &&\n      (shutdownButton.closest(\'.card\') || shutdownButton.closest(\'section\'));\n    var host = (anchor && anchor.parentNode) || document.querySelector(\'main\') || document.body;\n    if (!host) return false;\n\n    var card = document.createElement(\'section\');\n    card.id = \'st-admin-fault-recovery\';\n    card.className = \'card\';\n    card.innerHTML =\n      \'<h2>Motor Fault Recovery</h2>\' +\n      \'<p>Clear a latched locomotive or caboose fault. The train must be stationary. \' +\n      \'This does not command the train to move or resume a previous speed.</p>\' +\n      \'<div class="st-fault-controls">\' +\n        \'<button type="button" id="st-admin-clear-fault" disabled>Clear Fault</button>\' +\n        \'<span id="st-admin-fault-status" role="status" aria-live="polite">Checking train status…</span>\' +\n      \'</div>\' +\n      \'<div id="st-admin-fault-result" role="status" aria-live="polite"></div>\' +\n      \'<p class="st-fault-note">The server requires fresh telemetry, both motors stopped, \' +\n      \'and an explicit confirmation before accepting a fault reset.</p>\';\n\n    if (anchor) host.insertBefore(card, anchor);\n    else host.appendChild(card);\n    button = document.getElementById(\'st-admin-clear-fault\');\n    statusLine = document.getElementById(\'st-admin-fault-status\');\n    resultLine = document.getElementById(\'st-admin-fault-result\');\n    if (!button || !statusLine || !resultLine) return false;\n    button.addEventListener(\'click\', requestReset);\n    return true;\n  }\n\n  async function getJson(url) {\n    var res = await fetch(url, {credentials:\'same-origin\', cache:\'no-store\'});\n    var data = null;\n    try { data = await res.json(); } catch (_) { /* explain via status */ }\n    if (!res.ok) throw new Error((data && (data.error || data.message)) ||\n                                 (\'HTTP \' + res.status));\n    return data || {};\n  }\n\n  async function refresh() {\n    if (!button || !statusLine || busy) return;\n    try {\n      var results = await Promise.all([\n        getJson(\'/api/motor/status\'),\n        getJson(\'/api/admin/fault-reset/status\')\n      ]);\n      var motor = results[0];\n      var recovery = results[1];\n      var fresh = motor.fresh === true &&\n                  Number(motor.age_s) >= 0 && Number(motor.age_s) <= 3;\n      var stopped = Number(motor.speed) === 0 &&\n                    Number(motor.locomotive_power) === 0 &&\n                    Number(motor.caboose_power) === 0;\n      var latched = motor.fault_latched === true;\n      var pending = recovery.locked === true ||\n                    [\'REQUESTED\',\'RUNNING\',\'PENDING\'].indexOf(String(recovery.phase || \'\').toUpperCase()) !== -1;\n\n      if (!fresh) {\n        statusLine.textContent = \'Telemetry is stale; reset unavailable\';\n      } else if (!stopped) {\n        statusLine.textContent = \'Motors are not stopped; reset unavailable\';\n      } else if (!latched) {\n        statusLine.textContent = \'No latched motor fault\';\n      } else if (pending) {\n        statusLine.textContent = \'Recovery \' + (recovery.phase || \'in progress\');\n      } else {\n        statusLine.textContent = \'Motor fault latched — ready for confirmation\';\n      }\n      button.disabled = !(fresh && stopped && latched && !pending);\n\n      // An accepted POST only queues recovery. The result line must then be\n      // reconciled with fresh motor telemetry, not left at "Waiting..." forever.\n      if (awaitingReset) {\n        if (fresh && stopped && !latched) {\n          awaitingReset = false;\n          result(\'Fault cleared — both motors remain stopped.\', \'ok\');\n        } else if (fresh && latched && !pending && Date.now() - resetRequestedAt > 5000) {\n          awaitingReset = false;\n          result(\'Reset processed, but fault remains latched. Check both hub connections and try again.\', \'error\');\n        } else if (Date.now() - resetRequestedAt > 15000) {\n          result(pending\n            ? \'Recovery is still pending in the controller; verify the hub connections.\'\n            : \'Still waiting for fault-free motor telemetry; check the hub connections.\', \'error\');\n        }\n      }\n    } catch (err) {\n      button.disabled = true;\n      statusLine.textContent = \'Fault status unavailable: \' + (err.message || \'error\');\n      if (awaitingReset && Date.now() - resetRequestedAt > 15000) {\n        result(\'Unable to confirm recovery: motor status is unavailable.\', \'error\');\n      }\n    }\n  }\n\n  async function requestReset() {\n    if (!button || button.disabled || busy) return;\n    // The backend independently checks the current train state and the\n    // exact confirmation fields. This browser check is not the safety gate.\n    var confirmed = window.confirm(\n      \'I have verified that SensorTrain is stationary and both motors are stopped. \' +\n      \'Request a reset of the latched motor fault? The train will NOT restart automatically.\'\n    );\n    if (!confirmed) return;\n\n    busy = true;\n    button.disabled = true;\n    statusLine.textContent = \'Submitting fault reset…\';\n    result(\'\', \'\');\n    try {\n      var response = await fetch(\'/api/admin/fault-reset\', {\n        method: \'POST\',\n        credentials: \'same-origin\',\n        cache: \'no-store\',\n        headers: {\'Content-Type\': \'application/json\'},\n        body: JSON.stringify({\n          confirm: \'TRAIN STATIONARY - RESET FAULT\',\n          stationary_verified: true\n        })\n      });\n      var data = null;\n      try { data = await response.json(); } catch (_) { /* handled below */ }\n      if (!response.ok) {\n        throw new Error((data && (data.error || data.message)) ||\n                        (\'Fault reset refused (HTTP \' + response.status + \')\'));\n      }\n      if (response.status !== 202 || !data || data.accepted !== true) {\n        throw new Error(\'Unexpected reset response; verify Admin recovery status\');\n      }\n      awaitingReset = true;\n      resetRequestedAt = Date.now();\n      result(\'Reset accepted — checking live motor status…\', \'ok\');\n    } catch (err) {\n      result(\'Reset not accepted: \' + (err.message || \'request failed\'), \'error\');\n    } finally {\n      busy = false;\n      await refresh();\n    }\n  }\n\n  function start() {\n    if (!createUI()) return;\n    refresh();\n    window.setInterval(refresh, 2500);\n  }\n  if (document.readyState === \'loading\') {\n    document.addEventListener(\'DOMContentLoaded\', start, {once:true});\n  } else { start(); }\n})();\n</script>\n\n</body>', 1)


TABLET_PAGE = r"""<!DOCTYPE html>
<html>
<head>

  <meta charset="utf-8">

  <meta name="viewport"
        content="width=device-width,
                 initial-scale=1,
                 maximum-scale=1,
                 user-scalable=no">

  <meta name="apple-mobile-web-app-capable"
        content="yes">

  <title>SensorTrain Control Panel</title>

  <style>

    :root {
      font-family:
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;

      color-scheme: dark;
    }

    * {
      box-sizing: border-box;
      -webkit-tap-highlight-color: transparent;
    }

    html,
    body {
      margin: 0;

      width: 100%;
      min-height: 100%;

      background: #0d1216;
      color: #edf2f6;
    }

    body {
      min-height: 100dvh;
    }

    button,
    input {
      font: inherit;
    }

    .tablet {
      min-height: 100dvh;

      width: min(900px, 100%);
      margin: 0 auto;

      padding:
        max(14px, env(safe-area-inset-top))
        16px
        max(16px, env(safe-area-inset-bottom));

      display: flex;
      flex-direction: column;
      gap: 14px;
    }


    /* -------------------------------------------
       Header
       ------------------------------------------- */

    .header {
      display: flex;
      align-items: center;
      justify-content: space-between;

      gap: 12px;

      min-height: 46px;
    }

    .title {
      font-size: clamp(1.2rem, 4vw, 1.75rem);
      font-weight: 900;
      letter-spacing: -0.03em;
    }

    .subtitle {
      margin-top: 2px;

      color: #7e8d99;

      font-size: 0.68rem;
      font-weight: 800;
      letter-spacing: 0.09em;
      text-transform: uppercase;
    }

    .overall {
      padding: 7px 12px;

      border-radius: 999px;

      background: #3a2c29;
      color: #eea49c;

      font-size: 0.72rem;
      font-weight: 900;
      letter-spacing: 0.07em;
    }

    .overall.ready {
      background: #173c2c;
      color: #76dda7;
    }


    /* -------------------------------------------
       Card
       ------------------------------------------- */

    .card {
      background: #171e24;

      border: 1px solid #2c3740;
      border-radius: 15px;

      padding: 14px;
    }

    .card-title {
      color: #9daab4;

      font-size: 0.72rem;
      font-weight: 900;
      letter-spacing: 0.10em;
      text-transform: uppercase;

      margin-bottom: 11px;
    }


    /* -------------------------------------------
       Connection status
       ------------------------------------------- */

    .status-grid {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 7px;
    }

    .status-pill {
      min-width: 0;

      padding: 10px 4px;

      border: 1px solid #46515a;
      border-radius: 9px;

      background: #222a31;
      color: #8997a2;

      text-align: center;

      font-size: clamp(0.55rem, 2vw, 0.72rem);
      font-weight: 900;
      letter-spacing: 0.04em;
    }

    .status-pill::before {
      content: "";

      display: inline-block;

      width: 8px;
      height: 8px;

      margin-right: 5px;

      border-radius: 50%;

      background: #676f76;
    }

    .status-pill.ok {
      background: #18382a;
      border-color: #3a805d;
      color: #c9f1dc;
    }

    .status-pill.ok::before {
      background: #5ed494;

      box-shadow:
        0 0 7px
        rgba(94,212,148,0.8);
    }

    .status-pill.bad {
      background: #382421;
      border-color: #75423d;
      color: #edaaa4;
    }

    .status-pill.bad::before {
      background: #dd6c64;

      box-shadow:
        0 0 7px
        rgba(221,108,100,0.7);
    }


    /* -------------------------------------------
       Motor readout
       ------------------------------------------- */


    /* SensorTrain dual-hub compact indicators */
    .st15-dual-pill {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 3px;
      white-space: nowrap;
    }
    .st15-dual-pill::before { display: none; }
    .st15-dual-label {
      flex: 0 1 auto;
      min-width: 0;
      margin-right: 6px;
    }
    .st15-dual-unit {
      display: inline-flex;
      align-items: center;
      gap: 2px;
      flex: 0 0 auto;
      font-size: inherit;
      font-weight: inherit;
      letter-spacing: inherit;
      color: #c8d1d9;
    }
    .st15-dual-unit::before {
      content: "";
      display: block;
      width: 5px;
      height: 5px;
      border-radius: 50%;
      background: #85929c;
    }
    .st15-dual-unit.st15-ok::before {
      background: #58c98d;
    }
    .st15-dual-unit.st15-warn::before {
      background: #edbf59;
    }
    .st15-dual-unit.st15-bad::before {
      background: #db6c65;
    }
    html[data-st15-theme="light"] .st15-dual-unit {
      color: #425f6e;
    }

    .motor-main {
      display: grid;
      grid-template-columns:
        minmax(110px, 0.55fr)
        1.45fr;

      gap: 12px;

      margin-top: 13px;
    }

    .readout {
      min-height: 98px;

      display: flex;
      flex-direction: column;
      justify-content: center;
      align-items: center;

      border: 1px solid #34424d;
      border-radius: 12px;

      background: #0f151a;
    }

    .direction {
      color: #95a3ae;

      font-size: 0.72rem;
      font-weight: 900;
      letter-spacing: 0.10em;
    }

    .speed {
      margin-top: 5px;

      font-size: clamp(2rem, 8vw, 3rem);
      line-height: 1;
      font-weight: 900;
      letter-spacing: -0.05em;
      font-variant-numeric: tabular-nums;
    }


    /* -------------------------------------------
       Power
       ------------------------------------------- */

    .power {
      display: grid;
      grid-template-rows: auto 1fr;

      gap: 8px;

      padding: 10px 12px;

      border: 1px solid #34424d;
      border-radius: 12px;

      background: #11181d;
    }

    .power-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .power-label {
      color: #8998a4;

      font-size: 0.68rem;
      font-weight: 900;
      letter-spacing: 0.08em;
    }

    .power-value {
      font-size: 1.15rem;
      font-weight: 900;
      font-variant-numeric: tabular-nums;
    }

    .power-slider {
      width: 100%;
      min-height: 42px;

      accent-color: #6aa7e3;
    }


    /* -------------------------------------------
       Drive buttons
       ------------------------------------------- */

    .drive-buttons {
      display: grid;
      grid-template-columns:
        1fr 1.15fr 1fr;

      gap: 9px;

      margin-top: 12px;
    }

    .drive-button {
      min-height: 72px;

      appearance: none;

      border: 1px solid #455562;
      border-radius: 12px;

      background: #25313a;
      color: #eef3f6;

      font-size: clamp(0.75rem, 3vw, 1rem);
      font-weight: 900;
      letter-spacing: 0.04em;

      touch-action: manipulation;
    }

    .drive-button:active:not(:disabled) {
      transform: scale(0.98);
    }

    .drive-button.stop {
      background: #622f2b;
      border-color: #a2534b;
      color: #ffd6d2;

      font-size: clamp(0.9rem, 3.5vw, 1.15rem);
    }

    .drive-button:disabled {
      opacity: 0.35;
    }

    .motor-message {
      min-height: 20px;

      margin-top: 8px;

      color: #82909b;

      text-align: center;

      font-size: 0.69rem;
      font-weight: 750;
    }


    /* -------------------------------------------
       Fans
       ------------------------------------------- */

    .fan-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;

      gap: 12px;
    }

    .fan {
      border: 1px solid #35424c;
      border-radius: 12px;

      padding: 12px;

      background: #11171c;

      display: grid;
      gap: 11px;
    }

    .fan-top {
      display: flex;
      align-items: center;
      justify-content: space-between;

      gap: 8px;
    }

    .fan-name {
      font-size: 0.75rem;
      font-weight: 900;
      letter-spacing: 0.09em;
    }

    .fan-state-wrap {
      display: flex;
      align-items: center;
      gap: 7px;
    }

    .fan-lamp {
      width: 17px;
      height: 17px;

      border-radius: 50%;

      background: #bb5049;
      border: 1px solid #e17a73;

      box-shadow:
        0 0 7px
        rgba(225,122,115,0.55);
    }

    .fan-lamp.on {
      background: #4ac07e;
      border-color: #78dda5;

      box-shadow:
        0 0 8px
        rgba(74,192,126,0.75);
    }

    .fan-state {
      min-width: 27px;

      color: #a9b5be;

      font-size: 0.72rem;
      font-weight: 900;
    }

    .fan-buttons {
      display: grid;
      grid-template-columns: 1fr 1fr;

      gap: 8px;
    }

    .fan-button {
      min-height: 56px;

      border: 1px solid #46545f;
      border-radius: 10px;

      background: #253039;
      color: #dce4ea;

      font-weight: 900;

      touch-action: manipulation;
    }

    .fan-button.active {
      background: #184c38;
      border-color: #43a074;
      color: #daf5e8;
    }

    .fan-button:disabled {
      opacity: 0.4;
    }

    .all-off {
      width: 100%;
      min-height: 54px;

      margin-top: 10px;

      border: 1px solid #5a4c4a;
      border-radius: 10px;

      background: #292525;
      color: #d9cdcb;

      font-size: 0.77rem;
      font-weight: 900;
      letter-spacing: 0.07em;
    }

    .fan-message {
      min-height: 18px;

      margin-top: 7px;

      color: #82909b;
      text-align: center;

      font-size: 0.67rem;
    }


    /* -------------------------------------------
       Narrow / portrait tablets
       ------------------------------------------- */

    @media (max-width: 650px) {

      .status-grid {
        grid-template-columns: 1fr 1fr;
      }

      .motor-main {
        grid-template-columns: 1fr 1fr;
      }

      .drive-buttons {
        grid-template-columns: 1fr;
      }

      .drive-button {
        min-height: 62px;
      }

      .fan-grid {
        grid-template-columns: 1fr;
      }
    }


    /* SENSORTRAIN_V12_DUAL_WEB_V1 */

    .st-dual-strip {
      display: grid;
      gap: 5px;
      padding: 7px;
      border: 1px solid #384d42;
      border-radius: 9px;
      background: #102019;
      min-width: 0;
    }

    .st-dual-heading {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      font-size: 0.68rem;
      font-weight: 850;
      letter-spacing: 0.045em;
      color: #b5dac3;
    }

    .st-dual-result {
      color: #9ee6b3;
      text-align: right;
    }

    .st-dual-result.fault {
      color: #ffa5a5;
    }

    .st-dual-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 6px;
    }

    .st-dual-hub {
      min-width: 0;
      padding: 6px 7px;
      background: #192a22;
      border: 1px solid #304b3a;
      border-radius: 7px;
    }

    .st-dual-hub strong {
      display: block;
      color: #d8eade;
      font-size: 0.68rem;
      letter-spacing: 0.045em;
    }

    .st-dual-hub span {
      display: block;
      margin-top: 3px;
      font-size: 0.66rem;
      line-height: 1.35;
      color: #a9c3b1;
      overflow-wrap: anywhere;
    }

</style>


  <style>
    /* SENSORTRAIN_HUB_BATTERY_UI_V1 */

    .st-hub-battery {
      display: inline-flex;
      align-items: center;
      gap: 7px;

      min-height: 28px;
      padding: 5px 9px;

      border: 1px solid #46535e;
      border-radius: 999px;

      background: #151c22;
      color: #b9c4cc;

      font-size: 0.68rem;
      font-weight: 800;
      letter-spacing: 0.045em;

      white-space: nowrap;
    }

    .st-hub-battery.floating {
      position: fixed;
      top: 12px;
      right: 12px;
      z-index: 9999;

      box-shadow: 0 4px 18px
        rgba(0, 0, 0, 0.38);
    }

    .st-battery-shell {
      position: relative;

      width: 27px;
      height: 14px;

      border: 2px solid #687680;
      border-radius: 3px;

      padding: 2px;

      box-sizing: border-box;
    }

    .st-battery-shell::after {
      content: "";

      position: absolute;
      top: 3px;
      right: -5px;

      width: 3px;
      height: 6px;

      border-radius: 0 2px 2px 0;
      background: #687680;
    }

    .st-battery-fill {
      display: block;

      width: 0%;
      height: 100%;

      border-radius: 1px;
      background: #687680;

      transition:
        width 0.25s ease,
        background 0.25s ease;
    }

    .st-hub-battery.full
      .st-battery-fill,
    .st-hub-battery.good
      .st-battery-fill {
      background: #48c98a;
    }

    .st-hub-battery.low
      .st-battery-fill {
      background: #e4aa42;
    }

    .st-hub-battery.critical
      .st-battery-fill {
      background: #e16060;
    }

    .st-hub-battery-value {
      min-width: 34px;

      color: #f0f4f7;
      text-align: right;

      font-size: 0.77rem;
      font-variant-numeric: tabular-nums;
    }

    /* SENSORTRAIN_TABLET_REFRESH_V1 */

    .tablet .header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }

    .tablet-brand-row {
      display: flex;
      align-items: center;
      gap: 18px;
      min-width: 0;
    }

    .tablet-brand-logo {
      display: block;
      width: clamp(145px, 25vw, 220px);
      max-width: 100%;
      height: auto;
      object-fit: contain;
      flex-shrink: 0;
    }

    .tablet-panel-title {
      font-size: clamp(1.15rem, 2.7vw, 1.85rem);
      font-weight: 850;
      letter-spacing: -0.025em;
      color: #e6edf3;
      white-space: nowrap;
    }

    #allFansOff[data-mode="on"] {
      background: #174c36;
      border-color: #40976e;
      color: #d9f5e7;
    }

    @media (max-width: 550px) {
      .tablet .header {
        flex-wrap: wrap;
      }

      .tablet-brand-row {
        gap: 10px;
      }

      .tablet-brand-logo {
        width: 135px;
      }

      .tablet-panel-title {
        font-size: 1.15rem;
      }
    }

</style>

<!-- SENSORTRAIN_15A1_CONTROL_STYLE -->
<style>
 .st15-control-nav {display:flex;justify-content:flex-end;gap:7px;margin-bottom:8px;}
 .st15-control-nav a {
   color:#c8d7e1; background:#24313a;border:1px solid #4a606b;
   border-radius:8px;text-decoration:none;padding:8px 11px;
   font:800 .8rem system-ui,sans-serif;
 }
 .st15-control-nav a[aria-current="page"] {
   background:#19432e;color:#aef5c7;border-color:#3c8658;
 }
 .st15-control-nav a:focus-visible {outline:2px solid #82daa4;outline-offset:3px;}
</style>
</head>

<body>

  <main class="tablet">


    <header class="header">

      <div class="tablet-brand-row">
<img
          class="tablet-brand-logo"
          src="data:image/webp;base64,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"
          alt="SensorTrain">
<div class="tablet-panel-title">Control Panel</div>
</div>


      <div id="overall"
           class="overall">

        WAITING

      </div>

    </header>


    <section class="card">

      <div class="card-title">
        Train Control
      </div>


      <div class="status-grid">

        <div id="bridgePill"
             class="status-pill">
          BRIDGE
        </div>

        <div id="blePill"
             class="status-pill">
          BLE
        </div>

        <div id="hubPill" class="status-pill st15-dual-pill">
              <span class="st15-dual-label">TRAIN</span>
              <span class="st15-dual-unit st15-unknown" data-role="locomotive" title="Locomotive status unavailable">L</span>
              <span class="st15-dual-unit st15-unknown" data-role="caboose" title="Caboose status unavailable">C</span>
            </div>

        <div id="linkPill" class="status-pill st15-dual-pill">
              <span class="st15-dual-label">LEGO LINK</span>
              <span class="st15-dual-unit st15-unknown" data-role="locomotive" title="Locomotive status unavailable">L</span>
              <span class="st15-dual-unit st15-unknown" data-role="caboose" title="Caboose status unavailable">C</span>
            </div>

      </div>



          <div class="st-dual-strip">
            <div class="st-dual-heading">
              <span>BOTH MOTORS · SYNCHRONIZED</span>
              <span class="st-dual-result"
                    id="stTabletPair">WAITING</span>
            </div>

            <div class="st-dual-grid">
              <div class="st-dual-hub">
                <strong>LOCOMOTIVE</strong>
                <span id="stTabletLoco">Waiting...</span>
              </div>

              <div class="st-dual-hub">
                <strong>CABOOSE</strong>
                <span id="stTabletCaboose">Waiting...</span>
              </div>
            </div>
          </div>

      <div class="motor-main">

        <div class="readout">

          <div id="direction"
               class="direction">
            STOPPED
          </div>

          <div id="speed"
               class="speed">
            0%
          </div>

        </div>


        <div class="power">

          <div class="power-header">

            <div class="power-label">
              MOTOR POWER
            </div>

            <div id="powerValue"
                 class="power-value">
              20%
            </div>

          </div>


          <input
            id="power"
            class="power-slider"
            type="range"
            min="10"
            max="100"
            step="10"
            value="20"
            oninput="updatePower(this.value)">

        </div>

      </div>


      <div class="drive-buttons">

        <button
          id="reverseButton"
          class="drive-button"
          type="button"
          disabled
          onclick="drive(-1)">

          ◀ REVERSE

        </button>


        <button
          id="stopButton"
          class="drive-button stop"
          type="button"
          disabled
          onclick="sendMotor(0)">

          STOP

        </button>


        <button
          id="forwardButton"
          class="drive-button"
          type="button"
          disabled
          onclick="drive(1)">

          FORWARD ▶

        </button>

      </div>


      <div id="motorMessage"
           class="motor-message">

        Waiting for motor bridge...

      </div>

    </section>


    <section class="card">

      <div class="card-title">
        Cooling Fans
      </div>


      <div class="fan-grid">


        <div class="fan">

          <div class="fan-top">

            <div class="fan-name">
              INTAKE
            </div>

            <div class="fan-state-wrap">

              <div id="intakeLamp"
                   class="fan-lamp">
              </div>

              <div id="intakeState"
                   class="fan-state">
                OFF
              </div>

            </div>

          </div>


          <div class="fan-buttons">

            <button
              id="intakeOff"
              class="fan-button"
              type="button"
              onclick="setFan('intake',0)">

              OFF

            </button>

            <button
              id="intakeOn"
              class="fan-button"
              type="button"
              onclick="setFan('intake',100)">

              ON

            </button>

          </div>

        </div>


        <div class="fan">

          <div class="fan-top">

            <div class="fan-name">
              EXHAUST
            </div>

            <div class="fan-state-wrap">

              <div id="exhaustLamp"
                   class="fan-lamp">
              </div>

              <div id="exhaustState"
                   class="fan-state">
                OFF
              </div>

            </div>

          </div>


          <div class="fan-buttons">

            <button
              id="exhaustOff"
              class="fan-button"
              type="button"
              onclick="setFan('exhaust',0)">

              OFF

            </button>

            <button
              id="exhaustOn"
              class="fan-button"
              type="button"
              onclick="setFan('exhaust',100)">

              ON

            </button>

          </div>

        </div>

      </div>


      <button
        id="allFansOff"
        class="all-off"
        type="button"
        onclick="toggleAllFans()">
        ALL FANS ON
      </button>


      <div id="fanMessage"
           class="fan-message">

        Waiting for Pico...

      </div>

    </section>

  </main>


  <script>

    let motorStatus = null;

    let motorBusy = false;
    let fanBusy = false;

    let fanState = {
      intake_pct: 0,
      exhaust_pct: 0
    };


    function el(id) {
      return document.getElementById(id);
    }



    // Individual locomotive/caboose status lights.
    function renderStDualPill(node, data, mode) {
      if (!node) return;
      const live = Boolean(
        data && data.fresh === true &&
        data.dual_status_fresh === true &&
        data.serial_connected === true &&
        data.control_source_ok === true &&
        data.hubs
      );
      for (const role of ["locomotive", "caboose"]) {
        const lamp = node.querySelector(
          '[data-role="' + role + '"]'
        );
        if (!lamp) continue;
        const hub = live ? data.hubs[role] : null;
        let state = "unknown";
        if (hub && typeof hub.connected === "boolean") {
          if (!hub.connected) {
            state = "bad";
          } else if (mode === "link") {
            state = hub.gatt === "READY" &&
              hub.notify === "READY" ? "ok" : "warn";
          } else {
            // Per-hub readiness; global fault warning remains separate.
            state = sensorHubReady(hub) ? "ok" : "warn";
          }
        }
        lamp.className = "st15-dual-unit st15-" + state;
        const label = role === "locomotive"
          ? "Locomotive" : "Caboose";
        const meaning = state === "ok" ? "ready" :
          state === "warn" ? "connecting/not ready" :
          state === "bad" ? "disconnected or faulted" :
          "unavailable";
        lamp.title = label + " " + mode + ": " + meaning;
        lamp.setAttribute("aria-label", lamp.title);
      }
    }

    function setPill(
      id,
      good
    ) {

      const node =
        el(id);

      if (node.classList.contains("st15-dual-pill")) {
        renderStDualPill(node, motorStatus,
          id === "linkPill" ? "link" : "train");
        return;
      }

      node.classList.remove(
        "ok",
        "bad"
      );

      node.classList.add(
        good ? "ok" : "bad"
      );
    }



    // SENSORTRAIN_V12_DUAL_WEB_V1

    function sensorBridgeReady(data) {
      return Boolean(
        data &&
        data.fresh === true &&
        data.serial_connected === true &&
        data.control_source_ok === true
      );
    }

    function sensorHubReady(hub) {
      return Boolean(
        hub &&
        hub.connected === true &&
        hub.gatt === "READY" &&
        hub.notify === "READY" &&
        hub.motor0 === true &&
        (hub.sign === 1 || hub.sign === -1)
      );
    }

    function sensorPairReady(data) {
      return Boolean(
        sensorBridgeReady(data) &&
        data.dual_status_fresh === true &&
        data.pair_ready === true &&
        data.fault_latched === false &&
        data.hubs &&
        sensorHubReady(data.hubs.locomotive) &&
        sensorHubReady(data.hubs.caboose)
      );
    }

    function renderSensorPair(prefix, data) {
      const pair = document.getElementById(
        prefix + "Pair"
      );

      const loco = document.getElementById(
        prefix + "Loco"
      );

      const caboose = document.getElementById(
        prefix + "Caboose"
      );

      if (!pair || !loco || !caboose) {
        return;
      }

      const live = Boolean(
        data &&
        data.fresh === true &&
        data.dual_status_fresh === true
      );

      const fault = Boolean(
        live && data.fault_latched === true
      );

      const ready = sensorPairReady(data);

      pair.textContent = fault
        ? "FAULT LATCHED"
        : ready
          ? "BOTH READY"
          : "WAITING";

      pair.classList.toggle("fault", fault);

      function hubLine(role, powerKey) {
        if (!live || !data.hubs) {
          return "STATUS UNAVAILABLE";
        }

        const hub = data.hubs[role];

        if (!hub) {
          return "STATUS UNAVAILABLE";
        }

        const status = sensorHubReady(hub)
          ? "READY"
          : hub.connected
            ? "MOTOR/LINK NOT READY"
            : "DISCONNECTED";

        const battery = Number.isFinite(
          hub.battery_pct
        )
          ? hub.battery_pct + "%"
          : "--";

        const commanded = Number.isFinite(
          data[powerKey]
        )
          ? data[powerKey] + "%"
          : "--";

        return status +
          " · BAT " + battery +
          " · CMD " + commanded;
      }

      loco.textContent = hubLine(
        "locomotive",
        "locomotive_power"
      );

      caboose.textContent = hubLine(
        "caboose",
        "caboose_power"
      );
    }

    function motorReady(data) {
      return sensorPairReady(data);
    }


    function bridgeReady(data) {

      return (
        data &&
        data.fresh === true &&
        data.serial_connected === true &&
        data.control_source_ok === true
      );
    }


    function updatePower(value) {

      const power =
        Math.max(
          10,
          Math.min(
            100,
            Number(value) || 20
          )
        );

      el("powerValue").textContent =
        power + "%";
    }


    function refreshMotorButtons() {

      const ready =
        motorReady(
          motorStatus
        );

      const bridge =
        bridgeReady(
          motorStatus
        );


      el("reverseButton").disabled =
        motorBusy ||
        !ready;

      el("forwardButton").disabled =
        motorBusy ||
        !ready;

      el("stopButton").disabled =
        !bridge;
    }


    function renderMotor(data) {

      motorStatus = data;
      renderSensorPair("stTablet", data);

      const fresh =
        data.fresh === true;

      const bridge =
        bridgeReady(data);

      const ble =
        fresh &&
        data.ble === "READY";

      const hub =
        fresh &&
        data.hub === "CONNECTED";

      const link =
        fresh &&
        data.gatt === "READY" &&
        data.notify === "READY";


      setPill(
        "bridgePill",
        bridge
      );

      setPill(
        "blePill",
        ble
      );

      setPill(
        "hubPill",
        hub
      );

      setPill(
        "linkPill",
        link
      );


      const ready =
        motorReady(data);


      el("overall").textContent =
        ready
          ? "READY"
          : "WAITING";

      el("overall").className =
        ready
          ? "overall ready"
          : "overall";


      const speed =
        Number(
          data.speed ?? 0
        );


      el("speed").textContent =
        Math.abs(speed) + "%";


      if (speed > 0) {

        el("direction").textContent =
          "FORWARD";
      }
      else if (speed < 0) {

        el("direction").textContent =
          "REVERSE";
      }
      else {

        el("direction").textContent =
          "STOPPED";
      }


      if (!fresh) {

        el("motorMessage").textContent =
          "Motor status stale";
      }
      else if (!bridge) {

        el("motorMessage").textContent =
          "Motor bridge unavailable";
      }
      else if (!hub) {

        el("motorMessage").textContent =
          "Waiting for SensorTrain hub";
      }
      else if (!link) {

        el("motorMessage").textContent =
          "Establishing LEGO link";
      }
      else if (!motorBusy) {

        el("motorMessage").textContent =
          motorReady(data)
            ? "Both motors ready"
            : data.fault_latched === true
              ? "Safety fault latched"
              : "Waiting for dual-motor readiness";
      }


      refreshMotorButtons();
    }


    async function updateMotor() {

      try {

        const response =
          await fetch(
            "/api/motor/status",
            {
              cache: "no-store"
            }
          );

        if (!response.ok) {
          throw new Error(
            "HTTP " + response.status
          );
        }

        const data =
          await response.json();

        renderMotor(data);

      }
      catch (err) {

        motorStatus = null;
        renderSensorPair("stTablet", null);

        setPill(
          "bridgePill",
          false
        );

        setPill(
          "blePill",
          false
        );

        setPill(
          "hubPill",
          false
        );

        setPill(
          "linkPill",
          false
        );

        el("overall").textContent =
          "OFFLINE";

        el("overall").className =
          "overall";

        el("direction").textContent =
          "OFFLINE";

        el("speed").textContent =
          "--";

        el("motorMessage").textContent =
          "Motor API unavailable";

        refreshMotorButtons();
      }
    }


    async function sendMotor(speed) {

      if (
        (speed !== 0 && motorBusy) ||
        !bridgeReady(motorStatus)
      ) {
        return;
      }

      if (
        speed !== 0 &&
        !motorReady(motorStatus)
      ) {
        return;
      }


      motorBusy = true;
      refreshMotorButtons();

      el("motorMessage").textContent =
        speed === 0
          ? "Sending STOP..."
          : "Sending command...";


      try {

        const response =
          await fetch(
            "/api/motor/control",
            {
              method: "POST",

              headers: {
                "Content-Type":
                  "application/json"
              },

              body: JSON.stringify({
                speed: speed
              })
            }
          );

        if (!response.ok) {

          throw new Error(
            await response.text()
          );
        }

        await response.json();

        el("motorMessage").textContent =
          speed === 0
            ? "STOP requested"
            : "Command accepted";

      }
      catch (err) {

        el("motorMessage").textContent =
          "Motor control failed";
      }
      finally {

        motorBusy = false;

        setTimeout(
          updateMotor,
          250
        );

        setTimeout(
          updateMotor,
          800
        );
      }
    }


    function drive(direction) {

      const power =
        Math.max(
          10,
          Math.min(
            100,
            Number(
              el("power").value
            ) || 20
          )
        );

      sendMotor(
        direction < 0
          ? -power
          : power
      );
    }


    function renderFan(
      name,
      value
    ) {

      const on =
        Number(value) >= 100;

      const lamp =
        el(name + "Lamp");

      const state =
        el(name + "State");

      lamp.classList.toggle(
        "on",
        on
      );

      state.textContent =
        on ? "ON" : "OFF";


      el(
        name + "Off"
      ).classList.toggle(
        "active",
        !on
      );

      el(
        name + "On"
      ).classList.toggle(
        "active",
        on
      );
    }


    function refreshFanButtons() {

      for (const button of
        document.querySelectorAll(
          ".fan-button, .all-off"
        )) {

        button.disabled =
          fanBusy;
      }
    }


    function renderFans(data) {

      const intake =
        Number(
          data.fans?.intake_pct ?? 0
        );

      const exhaust =
        Number(
          data.fans?.exhaust_pct ?? 0
        );

      fanState = {
        intake_pct: intake,
        exhaust_pct: exhaust
      };


      renderFan(
        "intake",
        intake
      );

      renderFan(
        "exhaust",
        exhaust
      );


      if (!fanBusy) {

        el("fanMessage").textContent =
          "Confirmed by Pico";
      }
    }


    async function updateFans() {

      try {

        const response =
          await fetch(
            "/api/telemetry",
            {
              cache: "no-store"
            }
          );

        if (!response.ok) {

          throw new Error(
            "HTTP " + response.status
          );
        }

        const data =
          await response.json();

        renderFans(data);

      }
      catch (err) {

        el("fanMessage").textContent =
          "Pico telemetry unavailable";
      }
    }


    async function setFan(
      fan,
      value
    ) {

      let intake =
        fanState.intake_pct;

      let exhaust =
        fanState.exhaust_pct;


      if (fan === "intake") {

        intake = value;
      }
      else {

        exhaust = value;
      }


      await sendFans(
        intake,
        exhaust
      );
    }


    async function sendFans(
      intake,
      exhaust
    ) {

      if (fanBusy) {
        return;
      }


      fanBusy = true;
      refreshFanButtons();

      el("fanMessage").textContent =
        "Sending...";


      try {

        const response =
          await fetch(
            "/api/control",
            {
              method: "POST",

              headers: {
                "Content-Type":
                  "application/json"
              },

              body: JSON.stringify({
                intake_pct: intake,
                exhaust_pct: exhaust
              })
            }
          );

        if (!response.ok) {

          throw new Error(
            await response.text()
          );
        }

        await response.json();

        el("fanMessage").textContent =
          "Waiting for Pico...";

      }
      catch (err) {

        el("fanMessage").textContent =
          "Fan control failed";
      }
      finally {

        fanBusy = false;
        refreshFanButtons();

        setTimeout(
          updateFans,
          300
        );

        setTimeout(
          updateFans,
          900
        );
      }
    }


    updateMotor();
    updateFans();

    setInterval(
      updateMotor,
      1000
    );

    setInterval(
      updateFans,
      1000
    );

  </script>


  <script>
  (() => {
    "use strict";

    const BATTERY_ID =
      "sensortrainHubBattery";

    function findLegoLinkAnchor() {

      const nodes =
        document.querySelectorAll(
          "span, div, strong"
        );

      for (const node of nodes) {

        if (node.children.length !== 0) {
          continue;
        }

        const text =
          (node.textContent || "")
            .trim()
            .replace(/\s+/g, " ");

        if (text === "LEGO LINK") {
          return node;
        }
      }

      return null;
    }


    function mountBattery() {

      let pill =
        document.getElementById(
          BATTERY_ID
        );

      if (pill) {
        return pill;
      }

      pill =
        document.createElement("div");

      pill.id = BATTERY_ID;

      pill.className =
        "st-hub-battery unknown";


        pill.innerHTML = `
          <span style="font-weight:700">
            BATTERIES
          </span>

          <span data-role="locomotive"
                style="display:inline-flex;
                       align-items:center;
                       gap:5px;
                       white-space:nowrap">

            <span class="st-battery-shell"
                  aria-hidden="true">
              <span class="st-battery-fill"></span>
            </span>

            <span>LOCO</span>
            <span class="st-hub-battery-value">—</span>
          </span>

          <span data-role="caboose"
                style="display:inline-flex;
                       align-items:center;
                       gap:5px;
                       white-space:nowrap">

            <span class="st-battery-shell"
                  aria-hidden="true">
              <span class="st-battery-fill"></span>
            </span>

            <span>CABOOSE</span>
            <span class="st-hub-battery-value">—</span>
          </span>
        `;

      const anchor =
        findLegoLinkAnchor();

      if (anchor) {

        const target =
          anchor.closest(
            ".status-pill, " +
            ".health-pill, " +
            ".pill"
          )
          || anchor.parentElement
          || anchor;

        target.insertAdjacentElement(
          "afterend",
          pill
        );
      }
      else {

        pill.classList.add(
          "floating"
        );

        document.body.appendChild(
          pill
        );
      }

      return pill;
    }



    function updateBattery(data) {

      const pill = mountBattery();

      pill.classList.remove(
        "unknown", "full", "good",
        "low", "critical"
      );

      pill.style.flexWrap = "wrap";

      const fresh =
        data && data.fresh === true;

      const hubs =
        data && data.hubs
          ? data.hubs
          : {};

      const roles = [
        ["locomotive", "Locomotive"],
        ["caboose", "Caboose"]
      ];

      for (const [role, label] of roles) {

        const item = pill.querySelector(
          '[data-role="' + role + '"]'
        );

        if (!item) {
          continue;
        }

        const hub = hubs[role] || {};
        const pct = hub.battery_pct;

        const valid =
          fresh &&
          hub.connected === true &&
          Number.isInteger(pct) &&
          pct >= 0 &&
          pct <= 100;

        const valueNode = item.querySelector(
          ".st-hub-battery-value"
        );

        const fill = item.querySelector(
          ".st-battery-fill"
        );

        valueNode.textContent =
          valid ? pct + "%" : "—";

        fill.style.width =
          valid ? pct + "%" : "0%";

        fill.style.backgroundColor =
          !valid ? "transparent" :
          pct < 20 ? "#e55353" :
          pct < 40 ? "#f0a331" :
          "#83d13d";

        item.title = valid
          ? label + " battery: " + pct + "%"
          : label + " battery unavailable";
      }
    }

    async function pollHubBattery() {

      try {

        const response =
          await fetch(
            "/api/motor/status",
            {
              cache: "no-store"
            }
          );

        if (!response.ok) {
          throw new Error(
            "HTTP " +
            response.status
          );
        }

        const data =
          await response.json();

        updateBattery(data);

      }
      catch (error) {

        updateBattery(null);
      }
    }


    mountBattery();

    pollHubBattery();

    setInterval(
      pollHubBattery,
      1000
    );

  })();
  </script>


<script>
  // SENSORTRAIN_TABLET_FAN_TOGGLE_V1

  let tabletFanState = null;
  let tabletFanToggleBusy = false;

  function renderAllFansToggle() {
    const button = document.getElementById("allFansOff");
    if (!button) return;

    if (!tabletFanState) {
      button.textContent = "ALL FANS --";
      button.disabled = true;
      button.dataset.mode = "unknown";
      return;
    }

    const bothOff =
      tabletFanState.intake_pct === 0 &&
      tabletFanState.exhaust_pct === 0;

    button.textContent =
      bothOff ? "ALL FANS ON" : "ALL FANS OFF";

    button.dataset.mode = bothOff ? "on" : "off";
    button.disabled = tabletFanToggleBusy;
  }

  async function readTabletFans() {
    const response = await fetch(
      "/api/telemetry",
      { cache: "no-store" }
    );

    if (!response.ok) {
      throw new Error("Telemetry unavailable");
    }

    const data = await response.json();
    const fans = data.fans;

    if (
      !fans ||
      !Number.isFinite(fans.intake_pct) ||
      !Number.isFinite(fans.exhaust_pct) ||
      !Number.isFinite(data.age_s) ||
      data.age_s < 0 ||
      data.age_s > 8
    ) {
      throw new Error("Fan telemetry unavailable or stale");
    }

    return {
      intake_pct: fans.intake_pct,
      exhaust_pct: fans.exhaust_pct
    };
  }

  async function refreshAllFansToggle() {
    try {
      tabletFanState = await readTabletFans();
    } catch (error) {
      tabletFanState = null;
    }

    renderAllFansToggle();
  }

  async function toggleAllFans() {
    if (tabletFanToggleBusy) return;

    tabletFanToggleBusy = true;
    renderAllFansToggle();

    try {
      // Check current telemetry again before sending.
      const current = await readTabletFans();

      const bothOff =
        current.intake_pct === 0 &&
        current.exhaust_pct === 0;

      const target = bothOff ? 100 : 0;

      await sendFans(target, target);

    } catch (error) {
      const message = document.getElementById("fanMessage");

      if (message) {
        message.textContent =
          "Unable to confirm fan state; no new toggle command.";
      }
    } finally {
      tabletFanToggleBusy = false;
      await refreshAllFansToggle();
    }
  }

  refreshAllFansToggle();
  setInterval(refreshAllFansToggle, 2000);
</script>

<!-- SENSORTRAIN_134I_LIGHT_UI_V1 -->
<style>
  #st-headlight-controls {
    display: inline-flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
    padding: 7px 11px;
    border: 1px solid rgba(141, 209, 61, .45);
    border-radius: 12px;
    background: rgba(141, 209, 61, .08);
    color: inherit;
    font: inherit;
  }

  #st-headlight-controls .st-light-label {
    font-weight: 700;
    white-space: nowrap;
  }

  #st-headlight-controls .st-light-buttons {
    display: inline-flex;
    gap: 5px;
  }

  #st-headlight-controls button {
    border: 1px solid rgba(141, 209, 61, .6);
    border-radius: 7px;
    padding: 5px 9px;
    background: transparent;
    color: inherit;
    font: inherit;
    cursor: pointer;
  }

  #st-headlight-controls button[aria-pressed="true"] {
    background: #8dd13d;
    border-color: #8dd13d;
    color: #172310;
    font-weight: 700;
  }

  #st-headlight-controls button:disabled {
    opacity: .4;
    cursor: not-allowed;
  }

  #st-headlight-controls [data-light-info] {
    font-size: .78em;
    opacity: .85;
  }
</style>

<div id="st-headlight-controls"
     role="group"
     aria-label="Locomotive headlights">

  <span class="st-light-label">
    HEADLIGHTS
  </span>

  <span class="st-light-buttons">
    <button type="button"
            data-light-level="0"
            disabled>OFF</button>

    <button type="button"
            data-light-level="50"
            disabled>50%</button>

    <button type="button"
            data-light-level="100"
            disabled>100%</button>
  </span>

  <span data-light-info aria-live="polite">
    Checking headlight status...
  </span>
</div>

<script>
/* SENSORTRAIN_134I_LIGHT_WIDGET_V1 */
(() => {
  "use strict";

  const panel =
    document.getElementById("st-headlight-controls");

  if (!panel) return;

  const buttons = Array.from(
    panel.querySelectorAll("button[data-light-level]")
  );

  const info =
    panel.querySelector("[data-light-info]");

  let busy = false;
  let polling = false;
  let canSend = false;
  let notice = "";
  let noticeUntil = 0;

  function putNextToBattery() {
    const battery =
      document.getElementById("sensortrainHubBattery");

    if (
      battery &&
      battery.parentNode &&
      battery.nextElementSibling !== panel
    ) {
      battery.insertAdjacentElement("afterend", panel);
    }
  }

  function lightRecent(status) {
    return (
      typeof status.headlight_status_at === "number" &&
      typeof status.received_at === "number" &&
      status.received_at >= status.headlight_status_at &&
      status.received_at - status.headlight_status_at <= 3
    );
  }

  function freshLightAvailable(status, control) {
    if (!status || !control) return false;

    const hubs = status.hubs || {};

    return (
      status.fresh === true &&
      status.serial_connected === true &&
      status.control_source_ok === true &&
      status.dual_status_fresh === true &&
      status.pair_ready === true &&
      status.headlight_portb_ready === true &&
      status.fault_latched === false &&
      typeof status.dual_drive_active === "boolean" &&
      typeof status.drive_l_pending === "boolean" &&
      typeof status.drive_c_pending === "boolean" &&
      Number.isInteger(status.speed) && Math.abs(status.speed) <= 100 &&
      Number.isInteger(status.locomotive_power) && Math.abs(status.locomotive_power) <= 100 &&
      Number.isInteger(status.caboose_power) && Math.abs(status.caboose_power) <= 100 &&
      control.command === "speed" &&
      Number.isInteger(control.speed) && Math.abs(control.speed) <= 100 &&
      lightRecent(status) &&
      ["locomotive", "caboose"].every(role => {
        const hub = hubs[role] || {};

        return (
          hub.connected === true &&
          hub.gatt === "READY" &&
          hub.notify === "READY" &&
          hub.motor0 === true
        );
      })
    );
  }

  function setButtons(selectedLevel) {
    for (const button of buttons) {
      const selected =
        Number(button.dataset.lightLevel) === selectedLevel;

      button.setAttribute(
        "aria-pressed",
        selected ? "true" : "false"
      );

      button.disabled = !canSend || busy;
    }
  }

  async function refresh() {
    if (polling) return;

    polling = true;
    putNextToBattery();

    try {
      const responses = await Promise.all([
        fetch("/api/motor/status", {cache: "no-store"}),
        fetch("/api/motor/control", {cache: "no-store"})
      ]);

      if (responses.some(response => !response.ok)) {
        throw new Error("Status API unavailable");
      }

      const [status, control] = await Promise.all(
        responses.map(response => response.json())
      );

      canSend = freshLightAvailable(status, control);

      const commanded = status.headlight_commanded_pct;

      const validLevel =
        status.headlight_portb_ready === true &&
        status.fresh === true &&
        lightRecent(status) &&
        [0, 50, 100].includes(commanded);

      let label = validLevel
        ? "Commanded " + commanded +
          "% (not measured light output)"
        : "Headlights unavailable";

      if (
        validLevel &&
        status.headlight_last_write === "ACKNOWLEDGED"
      ) {
        label += " · BLE write accepted";
      } else if (
        validLevel &&
        status.headlight_last_write === "FAILED"
      ) {
        label += " · Last BLE write failed";
      }

      if (!canSend) {
        label += " · Controls unavailable: check link, telemetry, or safety";
      }

      if (Date.now() < noticeUntil) {
        label = notice + " · " + label;
      }

      info.textContent = label;
      setButtons(validLevel ? commanded : null);

    } catch (error) {
      canSend = false;
      info.textContent = "Headlight status unavailable";
      setButtons(null);

    } finally {
      polling = false;
    }
  }

  for (const button of buttons) {
    button.addEventListener("click", async () => {
      if (busy || !canSend) return;

      const level = Number(button.dataset.lightLevel);

      if (![0, 50, 100].includes(level)) return;

      busy = true;
      setButtons(null);

      try {
        const response = await fetch(
          "/api/headlight/control",
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json"
            },
            body: JSON.stringify({level})
          }
        );

        const payload = await response.json();

        if (
          !response.ok ||
          payload.accepted !== true
        ) {
          throw new Error(
            payload.error || "Request rejected"
          );
        }

        notice = "Requested " + level + "%";

      } catch (error) {
        notice = "Not sent: " + error.message;

      } finally {
        noticeUntil = Date.now() + 6000;
        busy = false;
        await refresh();
      }
    });
  }

  refresh();
  setInterval(refresh, 2500);
})();
</script>

<!-- SENSORTRAIN_134I_46_LAYOUT_ONLY -->
<style>
  #st-layout-headlight-row {
    display: flex;
    align-items: center;
    justify-content: flex-start;
    flex-wrap: wrap;
    gap: 8px;
    width: 100%;
    min-height: 38px;
    padding: 7px 0 2px;
    margin-top: 5px;
    border-top: 1px solid rgba(125,150,160,.22);
    box-sizing: border-box;
    grid-column: 1 / -1;
  }
  #st-layout-headlight-row #st-headlight-controls {
    position: static !important;
    top: auto !important;
    right: auto !important;
    bottom: auto !important;
    left: auto !important;
    transform: none !important;
    margin: 0 !important;
    box-shadow: none !important;
    max-width: 100%;
  }
</style>
<script>
(function () {
  'use strict';
  var scheduled = false;
  var expanded = false;

  function labelOf(el) {
    if (!el) return '';
    return (el.textContent || '').replace(/\s+/g, ' ').trim().toUpperCase();
  }

  function findControlPanel() {
    var nodes = document.querySelectorAll('h1,h2,h3,h4,span,div,strong');
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      if (node.children.length || labelOf(node) !== 'TRAIN CONTROL') continue;
      var parent = node.parentElement;
      while (parent && parent !== document.body &&
             !parent.classList.contains('page')) {
        var buttons = Array.from(parent.querySelectorAll('button'));
        var hasStop = buttons.some(function (b) {return labelOf(b) === 'STOP';});
        var hasForward = buttons.some(function (b) {return labelOf(b).indexOf('FORWARD') >= 0;});
        if (hasStop && hasForward) return parent;
        parent = parent.parentElement;
      }
    }
    return null;
  }

  function hideDuplicateBatteryChip(panel) {
    var nodes = document.querySelectorAll('span,div,strong,small,label');
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      if (node.children.length || labelOf(node) !== 'BATTERIES') continue;
      if (panel && panel.contains(node)) continue;
      var target = node.parentElement;
      for (var depth = 0; target && depth < 5; depth++, target = target.parentElement) {
        if (panel && panel.contains(target)) break;
        var txt = labelOf(target);
        if (txt.length > 260) break;
        if (txt.indexOf('LOCO') >= 0 && txt.indexOf('CABOOSE') >= 0) {
          target.style.display = 'none';
          break;
        }
      }
    }
  }

  function growPanelIfNeeded(panel) {
    if (expanded) return;
    var page = panel.closest('.page');
    if (!page || window.innerWidth < 1100) return;
    var rows = getComputedStyle(page).gridTemplateRows.split(/\s+/);
    if (rows.length !== 4 || !rows.every(function (v) {return /^\d+(\.\d+)?px$/.test(v);} )) return;
    var deficit = panel.scrollHeight - panel.clientHeight;
    if (deficit < 5) return;
    var add = Math.min(Math.ceil(deficit + 10), 75);
    if (parseFloat(rows[3]) - add < 240) return;
    page.style.gridTemplateRows = rows[0] + ' ' + rows[1] + ' ' +
       (parseFloat(rows[2]) + add) + 'px minmax(240px,1fr)';
    expanded = true;
  }

  function arrange() {
    var panel = findControlPanel();
    if (!panel) return;
    hideDuplicateBatteryChip(panel);
    var lights = document.getElementById('st-headlight-controls');
    if (!lights) return;
    var slot = document.getElementById('st-layout-headlight-row');
    if (!slot) {
      slot = document.createElement('div');
      slot.id = 'st-layout-headlight-row';
      panel.appendChild(slot);
    }
    if (lights.parentElement !== slot) slot.appendChild(lights);
    document.documentElement.setAttribute('data-st-layout', 'train-controls');
    requestAnimationFrame(function () {growPanelIfNeeded(panel);});
  }

  function schedule() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(function () {
      scheduled = false;
      arrange();
    });
  }

  function start() {
    arrange();
    new MutationObserver(schedule).observe(document.body, {
      childList: true, subtree: true
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, {once:true});
  } else {
    start();
  }
}());
</script>


<!-- SENSORTRAIN_134I_47_COMPACT_MIDDLE -->
<style>
  @media (min-width: 1120px) {
    /* The 3-column middle band becomes 2 columns: motion+travel / control. */
    .page:has(.st47-middle) {
      grid-template-rows: auto 122px 284px minmax(245px, 1fr) !important;
    }
    .st47-middle {
      display: grid !important;
      grid-template-columns: minmax(0, 44fr) minmax(0, 56fr) !important;
      grid-template-rows: minmax(0, 1fr) !important;
      gap: 9px !important;
      align-items: stretch !important;
      min-width: 0 !important;
      min-height: 0 !important;
    }
    .st47-middle > .st47-motion {
      grid-column: 1 !important;
      grid-row: 1 !important;
      min-width: 0 !important;
      min-height: 0 !important;
      overflow: hidden !important;
      display: flex !important;
      flex-direction: column !important;
      gap: 4px !important;
    }
    .st47-middle > .st47-control {
      grid-column: 2 !important;
      grid-row: 1 !important;
      min-width: 0 !important;
      min-height: 0 !important;
      overflow: hidden !important;
    }
    .st47-motion .st47-travel-source { display: none !important; }
    #st47-travel-strip {
      display: flex !important;
      align-items: center;
      justify-content: flex-start;
      flex-wrap: nowrap;
      gap: 7px;
      width: 100%;
      margin-top: auto;
      padding: 7px 0 0;
      border-top: 1px solid rgba(139,160,170,.25);
      min-width: 0;
      flex: 0 0 auto;
      font-variant-numeric: tabular-nums;
    }
    .st47-travel-title {
      color: #96a8b4;
      font-size: .65rem;
      font-weight: 850;
      letter-spacing: .10em;
      padding-right: 5px;
      flex: 0 0 auto;
    }
    .st47-metric {
      display: flex;
      align-items: baseline;
      flex: 1 1 0;
      gap: 4px;
      white-space: nowrap;
      min-width: 0;
    }
    .st47-metric small {
      color: #95a8b6;
      font-size: .59rem;
      font-weight: 800;
      letter-spacing: .035em;
    }
    .st47-metric strong {
      color: #edf4f8;
      font-size: .75rem;
      font-weight: 800;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    #st-layout-headlight-row {
      margin-top: auto !important;
      padding: 6px 2px 2px !important;
      min-height: 42px !important;
      border-top: 1px solid rgba(139,160,170,.25) !important;
      flex: 0 0 auto !important;
      display: flex !important;
      align-items: center !important;
      justify-content: flex-start !important;
      gap: 10px !important;
    }
    #st-layout-headlight-row #st-headlight-controls {
      position: static !important;
      transform: none !important;
      margin: 0 !important;
      flex-wrap: wrap;
      max-width: 100%;
    }
  }
  @media (min-width: 1120px) and (max-width: 1330px) {
    .st47-travel-title { display: none; }
    #st47-travel-strip { gap: 3px; }
    .st47-metric { gap: 3px; }
    .st47-metric strong { font-size: .67rem; }
  }
  @media (max-width: 1119px) {
    /* Tablet and narrow layouts keep their existing safe arrangement. */
    #st47-travel-strip { display: none !important; }
    .st47-travel-source { display: initial; }
  }
</style>
<script>
(function () {
  'use strict';
  var initialized = false;

  function cleanText(node) {
    return (node && node.textContent || '').replace(/\s+/g, ' ').trim();
  }
  function heading(label) {
    var nodes = document.querySelectorAll('h1,h2,h3,h4,h5,span,div,strong');
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].children.length === 0 &&
          cleanText(nodes[i]).toUpperCase() === label) return nodes[i];
    }
    return null;
  }
  function ancestorPath(node) {
    var a = [];
    while (node) { a.push(node); node = node.parentElement; }
    return a;
  }
  function directChild(parent, descendant) {
    while (descendant && descendant.parentElement !== parent) {
      descendant = descendant.parentElement;
    }
    return descendant && descendant.parentElement === parent ? descendant : null;
  }
  function commonParent(a, b, c) {
    var path = ancestorPath(a);
    for (var i = 0; i < path.length; i++) {
      if (path[i].contains(b) && path[i].contains(c)) return path[i];
    }
    return null;
  }
  function value(source, regex, suffix) {
    var result = source.match(regex);
    return result ? result[1] + (suffix || '') : '\u2014';
  }
  function createTravelStrip(travelPanel) {
    var strip = document.createElement('div');
    strip.id = 'st47-travel-strip';
    strip.setAttribute('aria-label', 'Travel telemetry');
    strip.innerHTML =
      '<span class="st47-travel-title">TRAVEL</span>' +
      '<span class="st47-metric"><small>RPM</small><strong data-st47="rpm">\u2014</strong></span>' +
      '<span class="st47-metric"><small>MPH</small><strong data-st47="mph">\u2014</strong></span>' +
      '<span class="st47-metric"><small>REVS</small><strong data-st47="revs">\u2014</strong></span>' +
      '<span class="st47-metric"><small>DIST</small><strong data-st47="dist">\u2014</strong></span>' +
      '<span class="st47-metric"><small>IR</small><strong data-st47="ir">\u2014</strong></span>';

    function set(key, text) {
      var target = strip.querySelector('[data-st47="' + key + '"]');
      if (target && target.textContent !== text) target.textContent = text;
    }
    function render() {
      // textContent joins adjacent HTML nodes without separators;
      // join text nodes explicitly so labels and values remain parseable.
      var tokens = [];
      var walker = document.createTreeWalker(travelPanel, NodeFilter.SHOW_TEXT);
      var node;
      while ((node = walker.nextNode())) {
        var token = cleanText(node);
        if (token) tokens.push(token);
      }
      var text = tokens.join(' ');
      set('rpm', value(text, /(-?\d+(?:\.\d+)?)\s*RPM\b/i));
      set('mph', value(text, /(-?\d+(?:\.\d+)?)\s*MPH\b/i));
      set('revs', value(text, /\bREVS\s*([\d,]+)/i));
      set('dist', value(text, /\bDIST\s*(-?\d+(?:\.\d+)?)/i));
      var ir = text.match(/\bIR\s*([\d.]+)\s*(?:\u00b5s|\u03bcs|us)\b/i);
      var state = text.match(/\b(CLEAR|BLOCKED|DETECTED)\b/i);
      set('ir', state ? state[1].toUpperCase() :
          (ir ? ir[1] + ' \u00b5s' : '\u2014'));
    }
    var pending = false;
    new MutationObserver(function () {
      if (pending) return;
      pending = true;
      requestAnimationFrame(function () { pending = false; render(); });
    }).observe(travelPanel, {subtree: true, childList: true, characterData: true});
    render();
    return strip;
  }
  function setup() {
    if (initialized || window.innerWidth < 1120) return;
    var motionTitle = heading('MOTION & ORIENTATION');
    var travelTitle = heading('TRAVEL');
    var controlTitle = heading('TRAIN CONTROL');
    if (!motionTitle || !travelTitle || !controlTitle) return;
    var middle = commonParent(motionTitle, travelTitle, controlTitle);
    if (!middle || !middle.classList || middle.classList.contains('page')) return;
    var motion = directChild(middle, motionTitle);
    var travel = directChild(middle, travelTitle);
    var control = directChild(middle, controlTitle);
    if (!motion || !travel || !control ||
        motion === travel || travel === control || control === motion) return;
    if (!control.querySelector('button') ||
        !document.getElementById('st-layout-headlight-row')) return;
    if (!middle.closest('.page')) return;

    // Real telemetry nodes and their existing update bindings stay in the DOM.
    // Do not clone or re-create any interactive motor/headlight elements.
    middle.classList.add('st47-middle');
    motion.classList.add('st47-motion');
    control.classList.add('st47-control');
    travel.classList.add('st47-travel-source');
    var strip = createTravelStrip(travel);
    motion.appendChild(travel);
    motion.appendChild(strip);
    initialized = true;
    document.documentElement.setAttribute('data-st47-layout', 'compact-middle');
  }
  function boot() {
    setup();
    if (initialized) return;
    // Some dashboard sections are populated after DOMContentLoaded.
    // Retry when their DOM nodes appear, without touching controls.
    var observer = new MutationObserver(function () {
      if (!initialized) setup();
      if (initialized) observer.disconnect();
    });
    observer.observe(document.body, {childList: true, subtree: true});
    window.addEventListener('resize', setup);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, {once: true});
  } else {
    boot();
  }
}());
</script>

<!-- SENSORTRAIN_15A1_CONTROL_SCRIPT -->
<script>
(function () {
 'use strict';
 function mount() {
   if (document.querySelector('.st15-control-nav')) return;
   document.documentElement.setAttribute('data-st15-release','1.5A-1');
   var shell = document.querySelector('main.tablet');
   if (!shell) return;
   var nav = document.createElement('nav');
   nav.className='st15-control-nav';
   nav.setAttribute('aria-label','SensorTrain pages');
   nav.innerHTML='<a href="/">Dashboard</a><a href="/tablet" aria-current="page">Control Panel</a>';
   shell.insertBefore(nav,shell.firstChild);
 }
 if (document.readyState === 'loading')document.addEventListener('DOMContentLoaded',mount,{once:true});
 else mount();
}());
</script>
</body>
</html>
"""



class Handler(BaseHTTPRequestHandler):

    def send_body(
        self,
        status,
        content_type,
        body
    ):
        self.send_response(status)

        self.send_header(
            "Content-Type",
            content_type
        )

        self.send_header(
            "Cache-Control",
            "no-store"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.end_headers()
        self.wfile.write(body)


    def do_GET(self):

        # SENSORTRAIN_ADMIN_AUTHENTICATION_ACTIVE
        _st_demo_path = self.path.split("?", 1)[0].rstrip("/")
        if (
            _st_demo_path == "/admin"
            or _st_demo_path.startswith("/admin/")
            or _st_demo_path == "/api/admin"
            or (
                _st_demo_path.startswith("/api/admin/")
                and self.path != "/api/admin/shutdown/request"
            )
        ):
            if not sensortrain_admin_gate(self):
                return
        global latest
        global received_at
        global latest_control
        global latest_motor_control
        global latest_motor_status
        global motor_status_at
        global history
        global bridge_heartbeat_at

        parsed = urlparse(
            self.path
        )

        path = parsed.path



        # SENSORTRAIN_ADMIN_GET_V1

        if path == "/api/admin/fault-reset/status":
            if not sensortrain_admin_gate(self):
                return

            sensortrain_admin_reconcile_recovery()
            with recovery_lock:
                data = dict(recovery_request)

            body = json.dumps(data).encode()

            self.send_body(
                200, "application/json", body
            )
            return

        if path == "/admin":
            if not sensortrain_admin_gate(self):
                return

            self.send_body(
                200,
                "text/html",
                ADMIN_PAGE.encode()
            )

            return


        if path == "/api/admin/shutdown/request":

            import os
            import hmac
            agent_secret = os.environ.get(
                "SENSORTRAIN_SHUTDOWN_AGENT_TOKEN", ""
            )
            agent_supplied = self.headers.get(
                "X-SensorTrain-Agent-Token", ""
            )
            if (
                len(agent_secret) < 43
                or not agent_secret.isascii()
                or not agent_supplied.isascii()
                or not hmac.compare_digest(
                    agent_supplied, agent_secret
                )
            ):
                self.send_body(
                    403,
                    "application/json",
                    b'{"error":"agent_auth_required"}'
                )
                return

            data = dict(
                latest_shutdown_request
            )

            requested_at = data.get(
                "requested_at"
            )

            if requested_at is None:

                data["age_s"] = None
                data["fresh"] = False

            else:

                age = (
                    time.time()
                    -
                    requested_at
                )

                data["age_s"] = round(
                    age,
                    1
                )

                data["fresh"] = (
                    age >= 0
                    and age <= 15.0
                )

            body = json.dumps(
                data
            ).encode()

            self.send_body(
                200,
                "application/json",
                body
            )

            return


        if path == "/api/history":

            try:

                params = parse_qs(
                    parsed.query
                )

                raw_minutes = params.get(
                    "minutes",
                    ["5"]
                )[0]

                minutes = int(
                    raw_minutes
                )

                allowed = (
                    1,
                    5,
                    10,
                    20,
                    30
                )

                if minutes not in allowed:
                    raise ValueError(
                        "minutes must be one of "
                        "1, 5, 10, 20, 30"
                    )


                cutoff = (
                    time.time()
                    -
                    minutes * 60
                )


                samples = [
                    sample
                    for sample in history
                    if sample["received_at"] >= cutoff
                ]


                response = {
                    "minutes": minutes,
                    "count": len(samples),
                    "samples": samples
                }


                body = json.dumps(
                    response
                ).encode()


                self.send_body(
                    200,
                    "application/json",
                    body
                )

            except Exception as exc:

                body = json.dumps({
                    "error": str(exc)
                }).encode()

                self.send_body(
                    400,
                    "application/json",
                    body
                )

            return


        if path == "/api/system":

            now = time.time()


            pico_age = (
                None
                if received_at is None
                else now - received_at
            )


            bridge_age = (
                None
                if bridge_heartbeat_at is None
                else now - bridge_heartbeat_at
            )


            pico_ok = (
                pico_age is not None
                and pico_age
                    <= PICO_FRESH_SECONDS
            )


            bridge_ok = (
                bridge_age is not None
                and bridge_age
                    <= BRIDGE_FRESH_SECONDS
            )


            (
                cluster_ok,
                cluster_problem,
                cluster_age
            ) = check_cluster_ready()


            response = {

                "pico": {
                    "ok": pico_ok,

                    "age_s": (
                        None
                        if pico_age is None
                        else round(
                            pico_age,
                            1
                        )
                    )
                },

                "bridge": {
                    "ok": bridge_ok,

                    "age_s": (
                        None
                        if bridge_age is None
                        else round(
                            bridge_age,
                            1
                        )
                    )
                },

                "cluster": {
                    "ok": cluster_ok,

                    "check_age_s": round(
                        cluster_age,
                        1
                    ),

                    "error": cluster_problem
                }
            }


            body = json.dumps(
                response
            ).encode()


            self.send_body(
                200,
                "application/json",
                body
            )

            return


        if path == "/api/control":

            body = json.dumps(
                latest_control
            ).encode()

            self.send_body(
                200,
                "application/json",
                body
            )

            return


        if path == "/api/motor/control":
            sensortrain_admin_reconcile_recovery()

            with recovery_lock:
                body = json.dumps(
                    {**latest_motor_control, "headlight": dict(latest_headlight_control), "recovery": dict(recovery_request)}
                ).encode()

            self.send_body(
                200,
                "application/json",
                body
            )

            return


        if path == "/api/motor/status":

            data = dict(
                latest_motor_status
            )

            if motor_status_at is None:

                data["received_at"] = None
                data["age_s"] = None
                data["fresh"] = False

            else:

                age = (
                    time.time()
                    -
                    motor_status_at
                )

                data["received_at"] = motor_status_at
                data["age_s"] = round(age, 1)
                data["fresh"] = age <= 3.0

            body = json.dumps(
                data
            ).encode()

            self.send_body(
                200,
                "application/json",
                body
            )

            return


        if path == "/api/telemetry":

            data = (
                dict(latest)
                if latest is not None
                else empty_telemetry()
            )

            if latest is not None:

                data["received_at"] = (
                    received_at
                )

                data["age_s"] = round(
                    time.time() -
                    received_at,
                    1
                )

            body = json.dumps(
                data
            ).encode()

            self.send_body(
                200,
                "application/json",
                body
            )


        elif path == "/tablet":

            self.send_body(
                200,
                "text/html",
                TABLET_PAGE.encode()
            )


        elif path == "/healthz":

            self.send_body(
                200,
                "text/plain",
                b"ok\n"
            )


        else:

            self.send_body(
                200,
                "text/html",
                PAGE.encode()
            )


    def do_POST(self):

        # SENSORTRAIN_ADMIN_AUTHENTICATION_ACTIVE
        _st_demo_path = self.path.split("?", 1)[0].rstrip("/")
        if (
            _st_demo_path == "/admin"
            or _st_demo_path.startswith("/admin/")
            or _st_demo_path == "/api/admin"
            or _st_demo_path.startswith("/api/admin/")
        ):
            if not sensortrain_admin_gate(self):
                return
        global latest
        global received_at
        global latest_control
        global latest_motor_control
        global latest_motor_status
        global motor_status_at
        global history
        global bridge_heartbeat_at



        # ------------------------------------------
        # SensorTrain administrative shutdown
        # ------------------------------------------

        # SENSORTRAIN_ADMIN_POST_AUTH_V2
        if self.path in (
            "/api/admin/shutdown",
            "/api/admin/fault-reset",
        ):
            if not sensortrain_admin_gate(self):
                return

        if self.path == "/api/admin/fault-reset":
            try:
                if not sensortrain_same_origin(self):
                    raise ValueError(
                        "same-origin HTTPS required"
                    )

                length = int(
                    self.headers.get(
                        "Content-Length", "0"
                    )
                )

                if not 2 <= length <= 512:
                    raise ValueError(
                        "invalid content length"
                    )

                payload = json.loads(
                    self.rfile.read(length)
                )

                if not isinstance(payload, dict):
                    raise ValueError(
                        "JSON object required"
                    )

                if (
                    payload.get("confirm") !=
                    "TRAIN STATIONARY - RESET FAULT"
                    or payload.get(
                        "stationary_verified"
                    ) is not True
                ):
                    raise ValueError(
                        "stationary confirmation required"
                    )

                now = time.time()

                with recovery_lock:

                    if recovery_request["locked"]:
                        raise ValueError(
                            "recovery already pending"
                        )

                    # SENSORTRAIN_DEMO_MANUAL_FAULT_RESET_V1: operator-initiated reset is permitted even if pair_ready is false.

                    stamp = time.time_ns()

                    latest_motor_control = {
                        "id": stamp,
                        "command": "speed",
                        "speed": 0
                    }

                    recovery_request.update({
                        "id": stamp,
                        "phase": "REQUESTED",
                        "locked": True,
                        "requested_at": now,
                        "result": "PENDING"
                    })

                body = json.dumps({
                    "accepted": True,
                    "id": stamp,
                    "phase": "REQUESTED",
                    "motor_locked": True
                }).encode()

                self.send_body(
                    202, "application/json", body
                )

            except (ValueError, TypeError) as exc:
                body = json.dumps({
                    "error": str(exc)
                }).encode()

                self.send_body(
                    400, "application/json", body
                )

            return

        if self.path == "/api/admin/shutdown":

            try:

                if not sensortrain_same_origin(self):
                    raise ValueError(
                        "same-origin HTTPS required"
                    )

                length = int(
                    self.headers.get(
                        "Content-Length",
                        "0"
                    )
                )

                if length <= 0 or length > 4096:

                    raise ValueError(
                        "invalid content length"
                    )

                body = self.rfile.read(
                    length
                )

                data = json.loads(
                    body
                )

                if data.get("confirm") != \
                        "SHUTDOWN SENSORTRAIN":

                    raise ValueError(
                        "shutdown confirmation required"
                    )


                stamp = int(
                    time.time() * 1000
                )


                # Command the train motor to stop.

                latest_motor_control = {
                    "id": stamp,
                    "command": "speed",
                    "speed": 0
                }


                # Command both external fans off.

                latest_control = {
                    "id": stamp,
                    "command": "fans",
                    "intake_pct": 0,
                    "exhaust_pct": 0
                }


                # Publish a short-lived request for the
                # host-side shutdown agent.

                latest_shutdown_request.clear()

                latest_shutdown_request.update({
                    "id": stamp,
                    "requested": True,
                    "requested_at": time.time()
                })


                response = json.dumps({
                    "accepted": True,
                    "id": stamp,
                    "message":
                        "SensorTrain shutdown requested"
                }).encode()


                self.send_body(
                    202,
                    "application/json",
                    response
                )


            except Exception as exc:

                response = json.dumps({
                    "error": str(exc)
                }).encode()

                self.send_body(
                    400,
                    "application/json",
                    response
                )


            return



        # ------------------------------------------
        # Motor-control endpoint
        # ------------------------------------------


            # SENSORTRAIN_134I_HEADLIGHT_POST_V2


        if self.path == "/api/headlight/control":
            with recovery_lock:
                try:
                    length = int(
                        self.headers.get(
                            "Content-Length", "0"
                        )
                    )

                    if not 2 <= length <= 512:
                        raise ValueError(
                            "invalid content length"
                        )

                    payload = json.loads(
                        self.rfile.read(length)
                    )

                    if not isinstance(payload, dict):
                        raise ValueError(
                            "JSON object required"
                        )

                    level = payload.get("level")

                    if (
                        type(level) is not int
                        or level not in (0, 50, 100)
                    ):
                        raise ValueError(
                            "level must be 0, 50, or 100"
                        )

                    if not headlight_request_allowed(
                        latest_motor_status,
                        latest_motor_control,
                        motor_status_at,
                        time.time()
                    ):
                        raise ValueError(
                            "headlight unavailable: hub, telemetry, or safety gate"
                        )

                    command_id = time.time_ns()

                    latest_headlight_control.update({
                        "id": command_id,
                        "command": "light",
                        "level": level
                    })

                    body = json.dumps({
                        "accepted": True,
                        "id": command_id,
                        "level": level
                    }).encode()

                    self.send_body(
                        202,
                        "application/json",
                        body
                    )

                except (ValueError, TypeError) as exc:
                    body = json.dumps({
                        "error": str(exc)
                    }).encode()

                    self.send_body(
                        400,
                        "application/json",
                        body
                    )

                return

        if self.path == "/api/motor/control":

            with recovery_lock:
                try:

                    length = int(
                        self.headers.get(
                            "Content-Length",
                            "0"
                        )
                    )

                    if length <= 0 or length > 4096:
                        raise ValueError(
                            "invalid content length"
                        )

                    body = self.rfile.read(
                        length
                    )

                    data = json.loads(
                        body
                    )

                    speed = data.get(
                        "speed"
                    )

                    if type(speed) is not int:
                        raise ValueError(
                            "speed must be an integer"
                        )

                    if speed < -100 or speed > 100:
                        raise ValueError(
                            "speed must be between -100 and 100"
                        )


                    # SENSORTRAIN_V12_DUAL_WEB_GATE_V1
                    # STOP is always accepted by the API.
                    # Nonzero commands require fresh status
                    # from both attached, healthy hubs.

                    if speed != 0:
                        if recovery_request["locked"]:
                            raise ValueError(
                                "motor locked for Admin recovery"
                            )


                        status = latest_motor_status

                        if (
                            motor_status_at is None
                            or time.time() - motor_status_at > 2.5
                        ):
                            raise ValueError(
                                "motor bridge status stale"
                            )

                        if (
                            status.get("serial_connected") is not True
                            or status.get("control_source_ok") is not True
                            or status.get("dual_status_fresh") is not True
                            or status.get("pair_ready") is not True
                            or status.get("fault_latched") is not False
                        ):
                            raise ValueError(
                                "dual motor pair not ready"
                            )

                        hubs = status.get("hubs")

                        if not isinstance(hubs, dict):
                            raise ValueError(
                                "dual hub status unavailable"
                            )

                        for role in ("locomotive", "caboose"):

                            hub = hubs.get(role)

                            if not isinstance(hub, dict):
                                raise ValueError(
                                    role + " status unavailable"
                                )

                            if (
                                hub.get("connected") is not True
                                or hub.get("gatt") != "READY"
                                or hub.get("notify") != "READY"
                                or hub.get("motor0") is not True
                                or hub.get("sign") not in (-1, 1)
                            ):
                                raise ValueError(
                                    role + " motor not ready"
                                )

                    latest_motor_control = {
                        "id": max(
                            int(time.time() * 1000),
                            int(latest_motor_control.get("id") or 0) + 1
                        ),
                        "command": "speed",
                        "speed": speed
                    }

                    response = json.dumps(
                        latest_motor_control
                    ).encode()

                    self.send_body(
                        202,
                        "application/json",
                        response
                    )

                except Exception as exc:

                    response = json.dumps({
                        "error": str(exc)
                    }).encode()

                    self.send_body(
                        400,
                        "application/json",
                        response
                    )

                return


        # ------------------------------------------
        # Raspberry Pi host telemetry
        # ------------------------------------------

        if self.path == "/api/host-telemetry":

            try:

                length = int(
                    self.headers.get(
                        "Content-Length",
                        "0"
                    )
                )

                if length <= 0 or length > 4096:
                    raise ValueError(
                        "invalid content length"
                    )

                body = self.rfile.read(
                    length
                )

                data = json.loads(
                    body
                )

                pi_cpu_c = data.get(
                    "pi_cpu_c"
                )

                if not isinstance(
                    pi_cpu_c,
                    (int, float)
                ):
                    raise ValueError(
                        "pi_cpu_c must be numeric"
                    )

                if (
                    pi_cpu_c < -40 or
                    pi_cpu_c > 150
                ):
                    raise ValueError(
                        "pi_cpu_c out of range"
                    )

                host_temperature[
                    "pi_cpu_c"
                ] = float(pi_cpu_c)

                host_temperature[
                    "received_at"
                ] = time.time()

                self.send_response(204)
                self.end_headers()

            except Exception as exc:

                response = json.dumps({
                    "error": str(exc)
                }).encode()

                self.send_body(
                    400,
                    "application/json",
                    response
                )

            return


        # ------------------------------------------
        # Fan-control endpoint
        # ------------------------------------------

        if self.path == "/api/control":

            try:

                length = int(
                    self.headers.get(
                        "Content-Length",
                        "0"
                    )
                )

                if length <= 0 or length > 4096:
                    raise ValueError(
                        "invalid content length"
                    )

                body = self.rfile.read(
                    length
                )

                data = json.loads(
                    body
                )

                intake_pct = data.get(
                    "intake_pct"
                )

                exhaust_pct = data.get(
                    "exhaust_pct"
                )

                valid_levels = (
                    0,
                    25,
                    50,
                    75,
                    100
                )

                if type(intake_pct) is not int:
                    raise ValueError(
                        "intake_pct must be an integer"
                    )

                if type(exhaust_pct) is not int:
                    raise ValueError(
                        "exhaust_pct must be an integer"
                    )

                if intake_pct not in valid_levels:
                    raise ValueError(
                        "intake_pct must be one of "
                        "0, 25, 50, 75, 100"
                    )

                if exhaust_pct not in valid_levels:
                    raise ValueError(
                        "exhaust_pct must be one of "
                        "0, 25, 50, 75, 100"
                    )

                latest_control = {
                    "id": int(
                        time.time() * 1000
                    ),
                    "command": "fans",
                    "intake_pct": intake_pct,
                    "exhaust_pct": exhaust_pct
                }

                response = json.dumps(
                    latest_control
                ).encode()

                self.send_body(
                    202,
                    "application/json",
                    response
                )

            except Exception as exc:

                response = json.dumps({
                    "error": str(exc)
                }).encode()

                self.send_body(
                    400,
                    "application/json",
                    response
                )

            return


        # ------------------------------------------
        # Telemetry endpoint
        # ------------------------------------------

        # ------------------------------------------
        # Motor-status endpoint
        # ------------------------------------------

        if self.path == "/api/motor/status":

            try:

                length = int(
                    self.headers.get(
                        "Content-Length",
                        "0"
                    )
                )

                if length <= 0 or length > 16384:
                    raise ValueError(
                        "invalid content length"
                    )

                body = self.rfile.read(
                    length
                )

                data = json.loads(
                    body
                )

                if data.get("schema") != \
                        "sensortrain.motor.v1":

                    raise ValueError(
                        "unsupported motor status schema"
                    )

                latest_motor_status = data
                motor_status_at = time.time()

                self.send_body(
                    204,
                    "text/plain",
                    b""
                )

            except Exception as exc:

                response = json.dumps({
                    "error": str(exc)
                }).encode()

                self.send_body(
                    400,
                    "application/json",
                    response
                )

            return


        # ------------------------------------------
        # Bridge heartbeat endpoint
        # ------------------------------------------

        if self.path == "/api/bridge-heartbeat":

            bridge_heartbeat_at = time.time()

            self.send_response(204)
            self.end_headers()

            return


        if self.path != "/api/telemetry":

            self.send_body(
                404,
                "text/plain",
                b"not found\n"
            )

            return


        try:

            length = int(
                self.headers.get(
                    "Content-Length",
                    "0"
                )
            )

            if length <= 0 or length > 65536:

                raise ValueError(
                    "invalid content length"
                )


            body = self.rfile.read(
                length
            )

            data = json.loads(
                body
            )


            if data.get("schema") != \
                    "sensortrain.telemetry.v1":

                raise ValueError(
                    "unsupported telemetry schema"
                )


            # Merge fresh Raspberry Pi CPU
            # temperature into the Pico telemetry
            # without changing the Pico firmware schema.
            temperatures = dict(
                data.get("temperatures") or {}
            )

            host_temp_at = host_temperature.get(
                "received_at"
            )

            if (
                host_temp_at is not None and
                host_temperature.get(
                    "pi_cpu_c"
                ) is not None and
                time.time() - host_temp_at
                    <= HOST_TEMP_FRESH_SECONDS
            ):

                temperatures["pi_cpu_c"] = (
                    host_temperature["pi_cpu_c"]
                )

            else:

                temperatures["pi_cpu_c"] = None

            data["temperatures"] = temperatures

            latest = data
            received_at = time.time()

            history_sample = dict(
                data
            )

            history_sample[
                "received_at"
            ] = received_at

            history.append(
                history_sample
            )


            self.send_body(
                204,
                "text/plain",
                b""
            )


        except Exception as exc:

            body = json.dumps({
                "error": str(exc)
            }).encode()


            self.send_body(
                400,
                "application/json",
                body
            )


    def log_message(
        self,
        format,
        *args
    ):
        pass




import os

class _SensorTrainTLSServer(HTTPServer):
    """Handle TLS separately from the HTTP listener."""

    def __init__(self, address, handler_class, certfile, keyfile):
        self._tls_context = ssl.SSLContext(
            ssl.PROTOCOL_TLS_SERVER
        )
        self._tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._tls_context.load_cert_chain(certfile, keyfile)
        super().__init__(address, handler_class)

    def get_request(self):
        raw, peer = self.socket.accept()
        raw.settimeout(2.0)

        try:
            secure = self._tls_context.wrap_socket(
                raw,
                server_side=True
            )
            secure.settimeout(6.0)
            return secure, peer
        except Exception:
            raw.close()
            raise


class _SensorTrainSerialHandler(Handler):
    """Serialize application handlers across both listeners."""

    _shared_lock = threading.RLock()

    def do_GET(self):
        with self._shared_lock:
            return super().do_GET()

    def do_POST(self):
        with self._shared_lock:
            return super().do_POST()


if os.environ.get("SENSORTRAIN_TLS_LISTENER_ENABLED") == "1":
    _cert = os.environ.get(
        "SENSORTRAIN_TLS_CERT_FILE",
        "/etc/sensortrain/tls/tls.crt"
    )
    _key = os.environ.get(
        "SENSORTRAIN_TLS_KEY_FILE",
        "/etc/sensortrain/tls/tls.key"
    )

    # Validate TLS certificate before opening HTTP.
    _https = _SensorTrainTLSServer(
        ("0.0.0.0", 8443),
        _SensorTrainSerialHandler,
        _cert,
        _key
    )

    try:
        _http = HTTPServer(
            ("0.0.0.0", 8080),
            _SensorTrainSerialHandler
        )
    except Exception:
        _https.server_close()
        raise

    # HTTP runs independently of TLS handshakes.
    # The shared handler lock protects application state.
    _http_worker = threading.Thread(
        target=_http.serve_forever,
        kwargs={"poll_interval": 0.2},
        daemon=True,
        name="sensortrain-http"
    )

    try:
        _http_worker.start()

        # TLS remains on the main thread so unexpected
        # listener failure cannot silently go unnoticed.
        _https.serve_forever(poll_interval=0.2)

    finally:
        _http.shutdown()
        _http_worker.join(timeout=8.0)
        _http.server_close()
        _https.server_close()

else:
    # Preserve original single-listener operation.
    HTTPServer(
        ("0.0.0.0", 8080),
        Handler
    ).serve_forever()
