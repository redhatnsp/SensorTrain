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

latest_control = {
    "id": 0,
    "command": None
}


latest_motor_control = {
    "id": 0,
    "command": None
}

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
  </style>

</head>

<body>

  <div class="page">

    <header class="header">


      <div class="brand sensortrain-brand">
        <img
          class="sensortrain-brand-logo"
          src="data:image/webp;base64,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"
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

      <div class="panel motion-panel">

        <div class="panel-title">
          Motion
        </div>

        <div class="motion-columns">

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

        </div>

      </div>


      <div class="panel orientation-panel">

        <div class="panel-title">
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

            <div id="motorHubPill"
                 class="motor-pill">TRAIN</div>

            <div id="motorLinkPill"
                 class="motor-pill">
              LEGO LINK
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

        ctx.fillStyle = "#667681";
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


        ctx.strokeStyle = "#263039";
        ctx.lineWidth = 1;

        ctx.beginPath();
        ctx.moveTo(left, y);
        ctx.lineTo(
          left + plotWidth,
          y
        );
        ctx.stroke();


        ctx.fillStyle = "#687783";
        ctx.textAlign = "right";
        ctx.textBaseline = "middle";

        ctx.fillText(
          value.toFixed(1),
          left - 4,
          y
        );
      }


      ctx.fillStyle = "#687783";
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


    function motorStatusGood(data) {

      return (
        data !== null &&
        data.fresh === true &&
        data.serial_connected === true &&
        data.control_source_ok === true &&
        data.ble === "READY" &&
        data.hub === "CONNECTED" &&
        data.gatt === "READY" &&
        data.notify === "READY"
      );
    }


    function setMotorPill(
      id,
      good
    ) {

      const node = el(id);

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
          motorControlBusy ||
          !ready;
      }
    }


    function renderMotorStatus(data) {

      latestMotorStatus = data;

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
          "Ready";
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
        motorControlBusy ||
        !motorStatusGood(
          latestMotorStatus
        )
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
        <span
          class="st-battery-shell"
          aria-hidden="true">
          <span
            class="st-battery-fill">
          </span>
        </span>

        <span>
          TRAIN BATTERY
        </span>

        <span
          class="st-hub-battery-value">
          —
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

      const pill =
        mountBattery();

      const valueNode =
        pill.querySelector(
          ".st-hub-battery-value"
        );

      const fill =
        pill.querySelector(
          ".st-battery-fill"
        );

      const raw =
        data
          ? data.battery_pct
          : null;

      const value =
        Number(raw);

      const valid =
        data &&
        data.fresh === true &&
        data.hub === "CONNECTED" &&
        raw !== null &&
        raw !== undefined &&
        Number.isFinite(value) &&
        value >= 0 &&
        value <= 100;

      pill.classList.remove(
        "unknown",
        "full",
        "good",
        "low",
        "critical"
      );

      if (!valid) {

        pill.classList.add(
          "unknown"
        );

        valueNode.textContent =
          "—";

        fill.style.width =
          "0%";

        pill.title =
          "LEGO hub battery unavailable";

        return;
      }

      const pct =
        Math.round(value);

      valueNode.textContent =
        pct + "%";

      fill.style.width =
        pct + "%";

      if (pct >= 75) {

        pill.classList.add(
          "full"
        );

      }
      else if (pct >= 40) {

        pill.classList.add(
          "good"
        );

      }
      else if (pct >= 20) {

        pill.classList.add(
          "low"
        );

      }
      else {

        pill.classList.add(
          "critical"
        );
      }

      pill.title =
        "LEGO hub battery: " +
        pct +
        "%";
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
       * Put TRAIN BATTERY immediately
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

ADMIN_PAGE = r"""<!DOCTYPE html>
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
      <div class="health-name">BLE</div>
      <div class="health-state">
        <span class="dot"></span>
        <span class="health-value">CHECKING</span>
      </div>
    </div>

    <div id="healthHub"
         class="health">
      <div class="health-name">LEGO Hub</div>
      <div class="health-state">
        <span class="dot"></span>
        <span class="health-value">CHECKING</span>
      </div>
    </div>

    <div id="healthGatt"
         class="health">
      <div class="health-name">GATT</div>
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

      <div class="sub-title">Motor</div>

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


  function motorReady(data) {

    return (
      data &&
      data.fresh === true &&
      data.serial_connected === true &&
      data.control_source_ok === true &&
      data.ble === "READY" &&
      data.hub === "CONNECTED" &&
      data.gatt === "READY" &&
      data.notify === "READY"
    );
  }


  function refreshMotorButtons() {

    const ready =
      motorReady(latestMotor);

    document
      .querySelectorAll(
        ".motor-button"
      )
      .forEach(button => {
        button.disabled =
          !ready ||
          shutdownStarted;
      });
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

      const gattOk =
        fresh &&
        data.gatt === "READY" &&
        data.notify === "READY";

      setHealth(
        "healthMotor",
        bridgeOk
      );

      setHealth(
        "healthBle",
        bleOk
      );

      setHealth(
        "healthHub",
        hubOk
      );

      setHealth(
        "healthGatt",
        gattOk
      );


      const speed =
        Number(data.speed || 0);

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


      if (motorReady(data)) {
        el("motorMessage").textContent =
          "Ready";
      }
      else {
        el("motorMessage").textContent =
          "Motor path unavailable";
      }

      refreshMotorButtons();

    }
    catch (err) {

      latestMotor = null;

      setHealth("healthMotor", false);
      setHealth("healthBle", false);
      setHealth("healthHub", false);
      setHealth("healthGatt", false);

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
      !motorReady(latestMotor)
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
"""


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

  <title>SensorTrain Control</title>

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

        <div id="hubPill"
             class="status-pill">TRAIN</div>

        <div id="linkPill"
             class="status-pill">
          LEGO LINK
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


    function setPill(
      id,
      good
    ) {

      const node =
        el(id);

      node.classList.remove(
        "ok",
        "bad"
      );

      node.classList.add(
        good ? "ok" : "bad"
      );
    }


    function motorReady(data) {

      return (
        data &&
        data.fresh === true &&
        data.serial_connected === true &&
        data.control_source_ok === true &&
        data.ble === "READY" &&
        data.hub === "CONNECTED" &&
        data.gatt === "READY" &&
        data.notify === "READY"
      );
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
        motorBusy ||
        !bridge;
    }


    function renderMotor(data) {

      motorStatus = data;

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
          "Ready";
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
        motorBusy ||
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
        <span
          class="st-battery-shell"
          aria-hidden="true">
          <span
            class="st-battery-fill">
          </span>
        </span>

        <span>
          TRAIN BATTERY
        </span>

        <span
          class="st-hub-battery-value">
          —
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

      const pill =
        mountBattery();

      const valueNode =
        pill.querySelector(
          ".st-hub-battery-value"
        );

      const fill =
        pill.querySelector(
          ".st-battery-fill"
        );

      const raw =
        data
          ? data.battery_pct
          : null;

      const value =
        Number(raw);

      const valid =
        data &&
        data.fresh === true &&
        data.hub === "CONNECTED" &&
        raw !== null &&
        raw !== undefined &&
        Number.isFinite(value) &&
        value >= 0 &&
        value <= 100;

      pill.classList.remove(
        "unknown",
        "full",
        "good",
        "low",
        "critical"
      );

      if (!valid) {

        pill.classList.add(
          "unknown"
        );

        valueNode.textContent =
          "—";

        fill.style.width =
          "0%";

        pill.title =
          "LEGO hub battery unavailable";

        return;
      }

      const pct =
        Math.round(value);

      valueNode.textContent =
        pct + "%";

      fill.style.width =
        pct + "%";

      if (pct >= 75) {

        pill.classList.add(
          "full"
        );

      }
      else if (pct >= 40) {

        pill.classList.add(
          "good"
        );

      }
      else if (pct >= 20) {

        pill.classList.add(
          "low"
        );

      }
      else {

        pill.classList.add(
          "critical"
        );
      }

      pill.title =
        "LEGO hub battery: " +
        pct +
        "%";
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

        if path == "/admin":

            self.send_body(
                200,
                "text/html",
                ADMIN_PAGE.encode()
            )

            return


        if path == "/api/admin/shutdown/request":

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

            body = json.dumps(
                latest_motor_control
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

        if self.path == "/api/admin/shutdown":

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

        if self.path == "/api/motor/control":

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

                latest_motor_control = {
                    "id": int(
                        time.time() * 1000
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


HTTPServer(
    ("0.0.0.0", 8080),
    Handler
).serve_forever()
