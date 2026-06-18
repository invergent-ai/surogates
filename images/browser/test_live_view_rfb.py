"""Build the browser image and assert :8080 speaks RFB-over-WebSocket.

Run from the surogates repo root. Requires docker and the ``websocket-client``
package (``pip install websocket-client``)::

    python -m pytest images/browser/test_live_view_rfb.py -v

The image disables neko and runs x11vnc + websockify, so the live-view port no
longer serves the neko web shell — connecting a binary WebSocket to :8080 must
yield an RFB ProtocolVersion banner relayed from x11vnc.
"""
import os
import subprocess
import time
import uuid

import pytest
import websocket  # websocket-client

IMAGE = "surogates-agent-browser:rfb-test"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run(cmd, **kwargs):
    return subprocess.run(cmd, cwd=ROOT, **kwargs)


def _build():
    _run(
        ["docker", "build", "-f", "images/browser/Dockerfile", "-t", IMAGE, "."],
        check=True,
    )


def _diag(name):
    """Dump container + service logs to aid debugging on failure."""
    print("\n===== docker logs =====")
    print(_run(["docker", "logs", name], capture_output=True, text=True).stdout[-4000:])
    for svc in ("xorg", "x11vnc", "websockify"):
        out = _run(
            ["docker", "exec", name, "sh", "-c", f"cat /var/log/supervisord/{svc} 2>/dev/null"],
            capture_output=True, text=True,
        ).stdout
        print(f"===== {svc} log =====\n{out[-2000:]}")


@pytest.mark.skipif(
    subprocess.run(["docker", "info"], capture_output=True).returncode != 0,
    reason="docker is not available",
)
def test_live_view_serves_rfb_over_ws():
    _build()
    name = f"rfb-test-{uuid.uuid4().hex[:8]}"
    _run(
        ["docker", "run", "-d", "--rm", "--name", name, "-p", "18080:8080", IMAGE],
        check=True,
    )
    try:
        deadline = time.time() + 90
        banner = None
        last_err = None
        while time.time() < deadline:
            try:
                ws = websocket.create_connection(
                    "ws://127.0.0.1:18080/", subprotocols=["binary"], timeout=4,
                )
                raw = ws.recv()
                ws.close()
                banner = raw.encode() if isinstance(raw, str) else raw
                if banner.startswith(b"RFB 00"):
                    break
                last_err = f"got non-RFB first frame: {banner!r}"
            except Exception as exc:  # noqa: BLE001 — retry until the stack is up
                last_err = repr(exc)
            time.sleep(2)
        if banner is None or not banner.startswith(b"RFB 00"):
            _diag(name)
            pytest.fail(f"no RFB banner on :8080 within 90s (last: {last_err})")
    finally:
        _run(["docker", "rm", "-f", name], check=False)
