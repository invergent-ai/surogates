import httpx
import pytest
import respx

from claweval_bench import tunnel as tunnel_mod
from claweval_bench.tunnel import (
    DOH_URL,
    Exposure,
    TunnelError,
    expose_adapter,
    is_local_base,
    wait_for_dns,
)


class _FakeProc:
    """Minimal stand-in for a cloudflared subprocess."""

    def __init__(self, alive: bool = True) -> None:
        self._alive = alive
        self.terminated = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self._alive = False


def test_local_bases_are_recognized():
    assert is_local_base("http://localhost:8000")
    assert is_local_base("http://127.0.0.1:8000")
    assert not is_local_base("https://cloud.surogate.ai")


def test_is_alive_tracks_process_state():
    # Loopback exposure (no proc) is always "alive" -- nothing to keep up.
    assert Exposure(public_base=None).is_alive() is True
    assert Exposure(public_base="https://x.trycloudflare.com",
                    _proc=_FakeProc(alive=True)).is_alive() is True
    assert Exposure(public_base="https://x.trycloudflare.com",
                    _proc=_FakeProc(alive=False)).is_alive() is False


def test_restart_mints_new_host_and_replaces_proc(monkeypatch):
    old, new = _FakeProc(alive=True), _FakeProc(alive=True)
    monkeypatch.setattr(
        tunnel_mod, "_start_quick_tunnel",
        lambda port: ("https://new-host.trycloudflare.com", new),
    )
    monkeypatch.setattr(tunnel_mod, "wait_for_dns", lambda host, timeout_s=30: [])
    exp = Exposure(public_base="https://old-host.trycloudflare.com", _proc=old)

    exp.restart(8321)

    assert exp.public_base == "https://new-host.trycloudflare.com"
    assert exp._proc is new
    assert old.terminated is True                     # old cloudflared killed
    assert exp.mcp_url(8321) == "https://new-host.trycloudflare.com/mcp"


def test_restart_is_noop_without_owned_process(monkeypatch):
    # A loopback / operator-supplied exposure owns no process to restart.
    called = False
    def _boom(port):
        nonlocal called
        called = True
        raise AssertionError("should not provision")
    monkeypatch.setattr(tunnel_mod, "_start_quick_tunnel", _boom)
    exp = Exposure(public_base=None, _proc=None)
    exp.restart(8321)
    assert called is False
    assert exp.public_base is None


def test_local_harness_needs_no_exposure():
    exposure = expose_adapter("http://localhost:8000", 8321)
    assert exposure.public_base is None
    assert exposure.mcp_url(8321) == "http://127.0.0.1:8321/mcp"
    exposure.close()  # no-op without a process


def test_explicit_public_url_wins_and_is_normalized():
    exposure = expose_adapter(
        "https://cloud.surogate.ai", 8321,
        public_url="https://claw.example.com/",
    )
    assert exposure.public_base == "https://claw.example.com"
    assert exposure.mcp_url(8321) == "https://claw.example.com/mcp"
    assert exposure.health_url(8321) == "https://claw.example.com/healthz"
    exposure.close()


def test_remote_harness_without_cloudflared_fails_with_guidance(monkeypatch):
    monkeypatch.setattr("claweval_bench.tunnel.shutil.which", lambda _: None)
    with pytest.raises(TunnelError, match="CLAWEVAL_ADAPTER_PUBLIC_URL"):
        expose_adapter("https://cloud.surogate.ai", 8321)


def test_exposure_urls_default_to_loopback():
    exposure = Exposure(public_base=None)
    assert exposure.health_url(9000) == "http://127.0.0.1:9000/healthz"


@respx.mock
def test_wait_for_dns_returns_records_when_they_appear():
    route = respx.get(DOH_URL).mock(
        side_effect=[
            httpx.Response(200, json={"Status": 3}),  # NXDOMAIN, no Answer
            httpx.Response(200, json={"Answer": [
                {"type": 1, "data": "104.16.1.1"},
                {"type": 5, "data": "cname.example."},  # non-A ignored
            ]}),
        ],
    )
    assert wait_for_dns("x.trycloudflare.com", timeout_s=30) == ["104.16.1.1"]
    assert route.call_count == 2
    first = route.calls[0].request
    assert first.url.params["name"] == "x.trycloudflare.com"
    assert first.headers["accept"] == "application/dns-json"


@respx.mock
def test_wait_for_dns_times_out_as_tunnel_error():
    respx.get(DOH_URL).mock(
        return_value=httpx.Response(200, json={"Status": 3}),
    )
    with pytest.raises(TunnelError, match="never appeared in DNS"):
        wait_for_dns("x.trycloudflare.com", timeout_s=0)
