#!/usr/bin/python3

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path


URL = (
    "http://10.43.180.47:8080"
    "/api/admin/shutdown/request"
)

POLL_SECONDS = 1.0
REQUEST_TIMEOUT = 2.0
SHUTDOWN_DELAY = 3.0

STATE_FILE = Path(
    "/run/sensortrain-shutdown-agent.last"
)

DRY_RUN = (
    os.environ.get(
        "SENSORTRAIN_SHUTDOWN_DRY_RUN",
        "1"
    ) == "1"
)


def read_last_id():
    try:
        return int(
            STATE_FILE.read_text().strip()
        )
    except Exception:
        return 0


def save_last_id(request_id):
    STATE_FILE.write_text(
        str(request_id)
    )


last_id = read_last_id()

print(
    "SensorTrain shutdown agent starting",
    flush=True
)

print(
    f"Shutdown endpoint: {URL}",
    flush=True
)

print(
    f"Dry run: {DRY_RUN}",
    flush=True
)


while True:

    try:

        request = urllib.request.Request(
            URL,
            headers={
                "Cache-Control": "no-cache"
            }
        )

        with urllib.request.urlopen(
            request,
            timeout=REQUEST_TIMEOUT
        ) as response:

            data = json.load(response)


        request_id = int(
            data.get("id", 0) or 0
        )

        requested = (
            data.get("requested") is True
        )

        fresh = (
            data.get("fresh") is True
        )


        if (
            requested
            and fresh
            and request_id > 0
            and request_id != last_id
        ):

            # Record it before doing anything so
            # restarting this service cannot replay
            # the same shutdown request.

            save_last_id(
                request_id
            )

            last_id = request_id


            print(
                "Fresh shutdown request received: "
                f"id={request_id}",
                flush=True
            )


            if DRY_RUN:

                print(
                    "DRY RUN: poweroff suppressed",
                    flush=True
                )

                continue


            print(
                "Waiting for motor STOP and "
                "fan OFF commands to propagate...",
                flush=True
            )

            time.sleep(
                SHUTDOWN_DELAY
            )


            print(
                "Initiating clean Pi shutdown",
                flush=True
            )


            subprocess.run(
                [
                    "/usr/bin/systemctl",
                    "poweroff"
                ],
                check=False
            )

            break


    except Exception as exc:

        # API availability can legitimately disappear
        # during MicroShift startup/shutdown.
        print(
            f"Poll unavailable: {exc}",
            flush=True
        )


    time.sleep(
        POLL_SECONDS
    )
