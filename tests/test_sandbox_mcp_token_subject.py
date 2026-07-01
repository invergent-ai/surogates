from __future__ import annotations

from uuid import uuid4

from surogates.sandbox.base import SandboxSpec
from surogates.sandbox.docker import DockerSandbox
from surogates.sandbox.kubernetes import K8sSandbox


def test_docker_mcp_token_marks_service_account(monkeypatch):
    captured = {}

    def fake_create_sandbox_token(**kwargs):
        captured.update(kwargs)
        return "token"

    monkeypatch.setattr(
        "surogates.tenant.auth.jwt.create_sandbox_token",
        fake_create_sandbox_token,
    )

    org_id = uuid4()
    service_account_id = uuid4()
    session_id = uuid4()
    spec = SandboxSpec(
        env={
            "ORG_ID": str(org_id),
            "USER_ID": str(service_account_id),
            "SUROGATES_AGENT_ID": "agent-1",
            "SUROGATES_IS_SERVICE_ACCOUNT": "1",
        },
        session_id=str(session_id),
    )
    sandbox = DockerSandbox.__new__(DockerSandbox)

    token = sandbox._mint_mcp_token(spec, sandbox_id=str(uuid4()))

    assert token == "token"
    assert captured["org_id"] == org_id
    assert captured["user_id"] == service_account_id
    assert captured["session_id"] == session_id
    assert captured["agent_id"] == "agent-1"
    assert captured["is_service_account"] is True


def test_docker_mcp_token_marks_user(monkeypatch):
    captured = {}

    def fake_create_sandbox_token(**kwargs):
        captured.update(kwargs)
        return "token"

    monkeypatch.setattr(
        "surogates.tenant.auth.jwt.create_sandbox_token",
        fake_create_sandbox_token,
    )

    org_id = uuid4()
    user_id = uuid4()
    spec = SandboxSpec(
        env={
            "ORG_ID": str(org_id),
            "USER_ID": str(user_id),
            "SUROGATES_AGENT_ID": "agent-1",
            "SUROGATES_IS_SERVICE_ACCOUNT": "0",
        },
        session_id=str(uuid4()),
    )
    sandbox = DockerSandbox.__new__(DockerSandbox)

    token = sandbox._mint_mcp_token(spec, sandbox_id=str(uuid4()))

    assert token == "token"
    assert captured["is_service_account"] is False


def test_kubernetes_mcp_token_marks_service_account(monkeypatch):
    captured = {}

    def fake_create_sandbox_token(**kwargs):
        captured.update(kwargs)
        return "token"

    monkeypatch.setattr(
        "surogates.tenant.auth.jwt.create_sandbox_token",
        fake_create_sandbox_token,
    )

    org_id = uuid4()
    service_account_id = uuid4()
    session_id = uuid4()
    sandbox = K8sSandbox(
        namespace="default",
        service_account="sandbox",
        executor_port=8071,
        storage_settings=None,
        mcp_proxy_url="http://mcp-proxy",
    )
    spec = SandboxSpec(
        env={
            "ORG_ID": str(org_id),
            "USER_ID": str(service_account_id),
            "SUROGATES_AGENT_ID": "agent-1",
            "SUROGATES_IS_SERVICE_ACCOUNT": "1",
        },
    )

    sandbox._build_pod_manifest(
        str(session_id),
        "sandbox-test",
        "sandbox-secret",
        spec,
        executor_token="executor",
    )

    assert captured["is_service_account"] is True
    assert captured["org_id"] == org_id
    assert captured["user_id"] == service_account_id
    assert captured["session_id"] == session_id
    assert captured["agent_id"] == "agent-1"
