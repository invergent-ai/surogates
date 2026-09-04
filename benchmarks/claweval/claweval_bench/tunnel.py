"""Expose the local MCP adapter to a remote harness.

The mcp-proxy dereferences a registered server's URL from inside the
platform's own network, so against prod a ``127.0.0.1`` adapter URL is
unreachable by construction — the adapter needs a public HTTPS front.
Three ways to get one, in precedence order:

1. ``CLAWEVAL_ADAPTER_PUBLIC_URL`` — a base URL you already operate
   (your own tunnel, a reverse proxy) that forwards to the adapter port.
2. A **cloudflared quick tunnel** started automatically for the run
   when the harness base URL is not local and ``cloudflared`` is on
   PATH. One tunnel serves the whole run: the adapter restarts per task
   behind the same local port.
3. Nothing, when the harness base URL is local — the proxy and the
   adapter share a loopback.

Quick-tunnel URLs are public and unauthenticated for the duration of
the run; the adapter only fronts the task's mock services (synthetic
fixture data), which is an accepted exposure for a benchmark.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

_QUICK_TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_TUNNEL_REGISTERED_RE = re.compile(r"Registered tunnel connection")
_TUNNEL_START_TIMEOUT_S = 60.0

DOH_URL = "https://cloudflare-dns.com/dns-query"
DNS_APPEAR_TIMEOUT_S = 120.0

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


class TunnelError(RuntimeError):
    """The adapter cannot be exposed to the harness."""


def is_local_base(base_url: str) -> bool:
    return (urlparse(base_url).hostname or "") in _LOCAL_HOSTS


@dataclass
class Exposure:
    """Where the harness should reach the adapter, plus the process
    keeping that address alive (``None`` when nothing was started)."""

    public_base: str | None
    _proc: subprocess.Popen | None = None

    def mcp_url(self, port: int) -> str:
        base = self.public_base or f"http://127.0.0.1:{port}"
        return f"{base}/mcp"

    def health_url(self, port: int) -> str:
        base = self.public_base or f"http://127.0.0.1:{port}"
        return f"{base}/healthz"

    def is_alive(self) -> bool:
        """True while the process holding the tunnel open is still running.

        A ``None`` proc (a loopback exposure, or one never started) counts
        as alive -- there is nothing to keep up.
        """
        return self._proc is None or self._proc.poll() is None

    def restart(self, adapter_port: int) -> None:
        """Re-provision a dropped quick tunnel.

        A fresh quick tunnel gets a NEW hostname, so ``public_base``
        changes; callers must re-read it (the runner registers the MCP row
        per task from ``mcp_url``, so it picks the new host up on the next
        task). No-op for a loopback exposure (nothing to restart) or one
        the operator supplied via ``CLAWEVAL_ADAPTER_PUBLIC_URL`` (we do
        not own that process).
        """
        if self._proc is None:
            return
        self.close()
        url, self._proc = _start_quick_tunnel(adapter_port)
        self.public_base = url
        try:
            wait_for_dns(urlparse(url).hostname or "", timeout_s=30)
        except TunnelError:
            pass  # best-effort; the platform resolves it independently

    def close(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None


def _start_quick_tunnel(
    port: int, attempts: int = 3,
) -> tuple[str, subprocess.Popen]:
    """Start a quick tunnel, retrying transient provisioning failures.

    ``api.trycloudflare.com`` intermittently times out handing out a
    quick tunnel ("context deadline exceeded"); a whole run should not
    die on one blip. Each attempt is a fresh cloudflared process (a
    failed one is killed first).
    """
    last: TunnelError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _start_quick_tunnel_once(port)
        except TunnelError as exc:
            last = exc
            if attempt < attempts:
                time.sleep(3)
    raise last  # type: ignore[misc]


def _start_quick_tunnel_once(port: int) -> tuple[str, subprocess.Popen]:
    """Start cloudflared and wait for URL *and* an established connection.

    The URL is printed before the tunnel actually connects, so waiting
    for it alone would accept a tunnel that never establishes (e.g. a
    network blocking cloudflared's outbound connections).
    """
    proc = subprocess.Popen(
        [
            "cloudflared", "tunnel", "--no-autoupdate",
            "--url", f"http://127.0.0.1:{port}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )

    found: list[str] = []
    registered = threading.Event()
    tail: list[str] = []
    progress = threading.Event()

    def _scan() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            tail.append(line.rstrip())
            del tail[:-30]
            match = _QUICK_TUNNEL_URL_RE.search(line)
            if match and not found:
                found.append(match.group(0))
            if _TUNNEL_REGISTERED_RE.search(line):
                registered.set()
            progress.set()
        progress.set()

    threading.Thread(target=_scan, daemon=True).start()

    deadline = time.monotonic() + _TUNNEL_START_TIMEOUT_S
    while not (found and registered.is_set()):
        if proc.poll() is not None or time.monotonic() > deadline:
            proc.terminate()
            raise TunnelError(
                "cloudflared quick tunnel did not come up; last output:\n"
                + "\n".join(tail[-10:])
            )
        progress.wait(timeout=0.5)
        progress.clear()
    return found[0], proc


def wait_for_dns(host: str, timeout_s: float = DNS_APPEAR_TIMEOUT_S) -> list[str]:
    """Poll Cloudflare DoH until the tunnel hostname has A records.

    Deliberately bypasses the OS resolver: asking getaddrinfo for the
    name before its record exists caches an NXDOMAIN for the zone's
    negative TTL (30 minutes for trycloudflare.com), which then fails
    every later probe on this machine even though the tunnel is fine.
    DoH goes straight to Cloudflare and cannot poison the local cache.
    """
    deadline = time.monotonic() + timeout_s
    with httpx.Client(timeout=5.0) as http:
        while True:
            try:
                resp = http.get(
                    DOH_URL,
                    params={"name": host, "type": "A"},
                    headers={"accept": "application/dns-json"},
                )
                answers = [
                    a["data"]
                    for a in (resp.json().get("Answer") or [])
                    if a.get("type") == 1
                ]
                if answers:
                    return answers
            except (httpx.HTTPError, ValueError):
                pass
            if time.monotonic() > deadline:
                raise TunnelError(
                    f"quick-tunnel hostname {host} never appeared in DNS"
                )
            time.sleep(2)


def expose_adapter(
    harness_base_url: str,
    adapter_port: int,
    *,
    public_url: str | None = None,
) -> Exposure:
    """Decide (and if needed create) the adapter's public address."""
    if public_url:
        return Exposure(public_base=public_url.rstrip("/"))
    if is_local_base(harness_base_url):
        return Exposure(public_base=None)
    if shutil.which("cloudflared") is None:
        raise TunnelError(
            f"harness at {harness_base_url} cannot reach a 127.0.0.1 adapter "
            "and no way to expose it: install cloudflared "
            "(brew install cloudflared) for an automatic quick tunnel, or "
            "set CLAWEVAL_ADAPTER_PUBLIC_URL to a base URL that forwards "
            f"to 127.0.0.1:{adapter_port}"
        )
    url, proc = _start_quick_tunnel(adapter_port)
    host = urlparse(url).hostname or ""
    # Best-effort: give the public hostname a head start in DNS so the
    # first task's health check does not race propagation. Never fatal --
    # a *registered* tunnel is the real readiness signal, the platform
    # resolves the name with its own resolvers, and the per-task public
    # check (runner._wait_public) already tolerates a local NXDOMAIN.
    # A slow or flaky DoH lookup must not sink an otherwise-live run.
    try:
        wait_for_dns(host, timeout_s=30)
    except TunnelError:
        pass
    return Exposure(public_base=url, _proc=proc)
