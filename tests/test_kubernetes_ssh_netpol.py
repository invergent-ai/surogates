"""SSH NetworkPolicy scopes egress to resolved target IPs (fail-closed)."""

from __future__ import annotations

from surogates.sandbox.base import SandboxSpec
from surogates.sandbox.kubernetes import K8sSandbox


def _backend(**kw):
    return K8sSandbox(
        namespace="ns", service_account="sa", pod_ready_timeout=1,
        executor_port=8080, storage_settings=None, s3fs_image="i",
        s3_endpoint="", mcp_proxy_url="", **kw,
    )


def _ssh_spec():
    return SandboxSpec(
        ssh_targets=[{"alias": "d", "host": "deploy.example.com", "port": 22,
                      "key_name": "prod"}],
        ssh_key_material={"prod": {"private_key": "x", "passphrase": ""}},
    )


def test_netpol_scopes_egress_to_targets_and_baseline():
    b = _backend(ssh_egress_baseline_cidrs=["10.0.0.0/8"])
    np = b._build_ssh_network_policy("sandbox-x", "sid1", [("1.2.3.4", 22)])
    cidrs = [
        peer.ip_block.cidr
        for rule in np.spec.egress for peer in (rule.to or [])
        if peer.ip_block
    ]
    assert "1.2.3.4/32" in cidrs
    assert "10.0.0.0/8" in cidrs
    assert np.spec.pod_selector.match_labels["surogates.ai/sandbox-id"] == "sid1"
    assert np.spec.policy_types == ["Egress"]
    assert np.metadata.name == "ssh-sandbox-x"
    # DNS egress present (first rule, no `to`, ports 53)
    dns_ports = {p.port for p in (np.spec.egress[0].ports or [])}
    assert dns_ports == {53}


def test_netpol_target_rule_restricts_to_port():
    b = _backend()
    np = b._build_ssh_network_policy("sandbox-x", "sid1", [("1.2.3.4", 2222)])
    target_rule = next(
        r for r in np.spec.egress
        if r.to and any(p.ip_block and p.ip_block.cidr == "1.2.3.4/32" for p in r.to)
    )
    assert [p.port for p in target_rule.ports] == [2222]


async def test_apply_returns_none_without_ssh():
    b = _backend()
    assert await b._apply_ssh_network_policy(SandboxSpec(), "sid", "sandbox-x") is None


async def test_resolve_target_ips_uses_getaddrinfo(monkeypatch):
    import asyncio

    b = _backend()

    async def fake_getaddrinfo(host, port, *, type=None):
        return [(None, None, None, None, ("9.9.9.9", port))]

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo, raising=False)
    ips = await b._resolve_target_ips(_ssh_spec())
    assert ("9.9.9.9", 22) in ips


async def test_resolve_target_ips_skips_unresolvable(monkeypatch):
    import asyncio

    b = _backend()

    async def boom(host, port, *, type=None):
        raise OSError("no such host")

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", boom, raising=False)
    assert await b._resolve_target_ips(_ssh_spec()) == []
