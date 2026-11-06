#!/usr/bin/python3

import json
import os
import re
import stat
import subprocess
import time
import urllib.request
import urllib.parse
import http.client
import socket
import ssl
from pathlib import Path


URL = (
    "https://sensortrain-web-tls.sensortrain.svc:8443"
    "/api/admin/shutdown/request"
)

POLL_SECONDS = 1.0
REQUEST_TIMEOUT = 2.0
SHUTDOWN_DELAY = 3.0


# SENSORTRAIN_AGENT_VERIFIED_TLS_V1_BEGIN

SERVICE_IP = "10.43.33.216"
SERVICE_HOST = "sensortrain-web-tls.sensortrain.svc"
SERVICE_PORT = 8443
CA_FILE = "/etc/sensortrain/trust/sensortrain-ca.crt"


class SensorTrainHTTPSConnection(http.client.HTTPSConnection):
    """Connect by Service IP; verify TLS using Service DNS identity."""

    def connect(self):
        if (
            self.host != SERVICE_HOST
            or self.port != SERVICE_PORT
            or self._tunnel_host
        ):
            raise RuntimeError("Unexpected shutdown-agent destination")

        raw = socket.create_connection(
            (SERVICE_IP, SERVICE_PORT),
            self.timeout,
            self.source_address
        )

        try:
            self.sock = self._context.wrap_socket(
                raw,
                server_hostname=SERVICE_HOST
            )
        except BaseException:
            raw.close()
            raise


class SensorTrainHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, allowed_path):
        self.allowed_path = allowed_path

        context = ssl.create_default_context(
            cafile=CA_FILE
        )
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED

        super().__init__(context=context)

    def https_open(self, request):
        target = urllib.parse.urlsplit(request.full_url)

        if (
            target.scheme != "https"
            or target.hostname != SERVICE_HOST
            or target.port != SERVICE_PORT
            or target.path != self.allowed_path
            or target.query
            or target.fragment
            or target.username is not None
            or target.password is not None
        ):
            raise ValueError("Unexpected shutdown-agent HTTPS URL")

        return self.do_open(
            SensorTrainHTTPSConnection,
            request,
            context=self._context
        )


class SensorTrainDenyHTTP(urllib.request.HTTPHandler):
    def http_open(self, request):
        raise RuntimeError(
            "Plain HTTP prohibited for shutdown agent"
        )


TLS_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    SensorTrainDenyHTTP(),
    SensorTrainHTTPSHandler(
        "/api/admin/shutdown/request"
    )
)

# SENSORTRAIN_AGENT_VERIFIED_TLS_V1_END


STATE_FILE = Path(
    "/run/sensortrain-shutdown-agent.last"
)

DRY_RUN = (
    os.environ.get(
        "SENSORTRAIN_SHUTDOWN_DRY_RUN",
        "1"
    ) == "1"
)



TOKEN_FILE = Path(
    "/etc/sensortrain/shutdown-agent-token"
)


def read_agent_token():
    try:
        metadata = TOKEN_FILE.lstat()

        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_mode & 0o077
        ):
            raise ValueError("Unsafe token file")

        token = TOKEN_FILE.read_text(
            encoding="ascii"
        ).strip()

    except (OSError, UnicodeError, ValueError):
        raise RuntimeError(
            "Shutdown agent token unavailable or unsafe"
        ) from None

    if not re.fullmatch(
        r"[A-Za-z0-9_-]{43}", token
    ):
        raise RuntimeError(
            "Shutdown agent token invalid"
        )

    return token


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
                "Cache-Control": "no-cache",
                "X-SensorTrain-Agent-Token": read_agent_token()
            }
        )

        with TLS_OPENER.open(
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
