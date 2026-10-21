#!/usr/bin/python3

import glob
import json
import os
import time
import urllib.request
from pathlib import Path


BASE_URL = os.environ.get(
    "SENSORTRAIN_BASE_URL",
    "http://10.43.180.47:8080"
)

INTERVAL = 2.0


def find_cpu_temp_file():

    zones = glob.glob(
        "/sys/class/thermal/thermal_zone*"
    )

    for zone in zones:

        zone = Path(zone)

        try:
            kind = (
                zone.joinpath("type")
                .read_text()
                .strip()
                .lower()
            )
        except Exception:
            continue

        if "cpu" in kind:
            return zone / "temp"

    fallback = Path(
        "/sys/class/thermal/thermal_zone0/temp"
    )

    if fallback.exists():
        return fallback

    return None


TEMP_FILE = find_cpu_temp_file()


def read_cpu_temp_c():

    if TEMP_FILE is None:
        return None

    try:

        value = float(
            TEMP_FILE
            .read_text()
            .strip()
        )

        if abs(value) > 1000:
            value /= 1000.0

        if not -40 <= value <= 150:
            return None

        return round(
            value,
            2
        )

    except Exception:
        return None


def post_temperature(value):

    body = json.dumps({
        "pi_cpu_c": value
    }).encode()

    request = urllib.request.Request(
        BASE_URL + "/api/host-telemetry",
        data=body,
        headers={
            "Content-Type":
                "application/json"
        },
        method="POST"
    )

    with urllib.request.urlopen(
        request,
        timeout=2
    ) as response:

        return response.status


def main():

    print(
        "SensorTrain host telemetry"
    )

    print(
        "CPU thermal source:",
        TEMP_FILE
    )

    while True:

        value = read_cpu_temp_c()

        if value is not None:

            try:

                post_temperature(
                    value
                )

            except Exception as exc:

                print(
                    "Host telemetry error:",
                    exc
                )

        time.sleep(
            INTERVAL
        )


if __name__ == "__main__":
    main()
