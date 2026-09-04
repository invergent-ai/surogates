import json

import httpx
import pytest
import respx

from claweval_bench.registrar import (
    FIREBASE_SIGNIN_URL,
    Registrar,
    RegistrarError,
    server_name,
)

BASE = "http://localhost:8888"
AGENT = "agent-1"
PROJECT = "proj-1"


def _mock_local_account(username: str = "u") -> None:
    respx.get(f"{BASE}/api/auth/resolve-username").mock(
        return_value=httpx.Response(
            200, json={"kind": "local", "email": f"{username}@example.com"},
        ),
    )


def _registrar(**kwargs) -> Registrar:
    kwargs.setdefault("token", "tok")
    return Registrar(BASE, PROJECT, AGENT, **kwargs)


def _mock_list(servers: list[dict]) -> None:
    respx.get(f"{BASE}/api/mcp-servers").mock(
        return_value=httpx.Response(
            200, json={"mcp_servers": servers, "total": len(servers)},
        ),
    )


def test_server_name_is_task_scoped():
    assert server_name("T028_x") == "claweval-T028_x"


def test_missing_auth_is_rejected():
    with pytest.raises(RegistrarError):
        Registrar(BASE, PROJECT, AGENT)


@respx.mock
def test_login_with_credentials_sets_bearer():
    _mock_local_account()
    login = respx.post(f"{BASE}/api/auth/login").mock(
        return_value=httpx.Response(
            200, json={"access_token": "jwt-1", "refresh_token": "r"},
        ),
    )
    _mock_list([])
    reg = Registrar(BASE, PROJECT, AGENT, username="u", password="p")
    assert reg.cleanup_all() == 0
    assert login.called
    list_call = respx.calls[-1].request
    assert list_call.headers["Authorization"] == "Bearer jwt-1"
    reg.close()


@respx.mock
def test_firebase_account_signs_in_via_identitytoolkit():
    respx.get(f"{BASE}/api/auth/resolve-username").mock(
        return_value=httpx.Response(
            200, json={"kind": "firebase", "email": "u@example.com"},
        ),
    )
    signin = respx.post(FIREBASE_SIGNIN_URL).mock(
        return_value=httpx.Response(200, json={"idToken": "fb-id-token"}),
    )
    exchange = respx.post(f"{BASE}/api/auth/firebase").mock(
        return_value=httpx.Response(
            200, json={"status": "authenticated", "access_token": "jwt-fb",
                       "refresh_token": "r"},
        ),
    )
    _mock_list([])
    reg = Registrar(
        BASE, PROJECT, AGENT, username="u", password="p",
        firebase_api_key="key-1",
    )
    assert reg.cleanup_all() == 0
    assert signin.calls[0].request.url.params["key"] == "key-1"
    signin_body = json.loads(signin.calls[0].request.content)
    assert signin_body["email"] == "u@example.com"
    assert signin_body["password"] == "p"
    assert json.loads(exchange.calls[0].request.content) == {
        "id_token": "fb-id-token",
    }
    list_call = respx.calls[-1].request
    assert list_call.headers["Authorization"] == "Bearer jwt-fb"
    reg.close()


@respx.mock
def test_unknown_account_fails_with_guidance():
    respx.get(f"{BASE}/api/auth/resolve-username").mock(
        return_value=httpx.Response(404, json={"detail": "Username not found"}),
    )
    with pytest.raises(RegistrarError, match="not found"):
        Registrar(BASE, PROJECT, AGENT, username="u", password="p")


@respx.mock
def test_expired_token_relogins_once():
    _mock_local_account()
    tokens = iter(["jwt-1", "jwt-2"])
    respx.post(f"{BASE}/api/auth/login").mock(
        side_effect=lambda _req: httpx.Response(
            200, json={"access_token": next(tokens), "refresh_token": "r"},
        ),
    )
    listed = respx.get(f"{BASE}/api/mcp-servers").mock(
        side_effect=[
            httpx.Response(401, json={"detail": "expired"}),
            httpx.Response(200, json={"mcp_servers": [], "total": 0}),
        ],
    )
    reg = Registrar(BASE, PROJECT, AGENT, username="u", password="p")
    assert reg.cleanup_all() == 0
    assert listed.call_count == 2
    assert listed.calls[1].request.headers["Authorization"] == "Bearer jwt-2"
    reg.close()


@respx.mock
def test_firebase_relogin_does_not_leak_stale_bearer():
    # A full run outlasts the ~hourly ops token; the re-login must not send
    # the expired ops bearer to Google's identitytoolkit (it 401s with
    # "Expected OAuth 2 access token").
    respx.get(f"{BASE}/api/auth/resolve-username").mock(
        return_value=httpx.Response(
            200, json={"kind": "firebase", "email": "u@example.com"},
        ),
    )
    fb_tokens = iter(["fb-1", "fb-2"])
    signin = respx.post(FIREBASE_SIGNIN_URL).mock(
        side_effect=lambda _req: httpx.Response(
            200, json={"idToken": next(fb_tokens)},
        ),
    )
    ops_tokens = iter(["jwt-1", "jwt-2"])
    respx.post(f"{BASE}/api/auth/firebase").mock(
        side_effect=lambda _req: httpx.Response(
            200, json={"status": "authenticated",
                       "access_token": next(ops_tokens), "refresh_token": "r"},
        ),
    )
    respx.get(f"{BASE}/api/mcp-servers").mock(
        side_effect=[
            httpx.Response(401, json={"detail": "expired"}),  # forces re-login
            httpx.Response(200, json={"mcp_servers": [], "total": 0}),
        ],
    )
    reg = Registrar(BASE, PROJECT, AGENT, username="u", password="p")
    assert reg.cleanup_all() == 0
    # Two Firebase sign-ins happened (initial + re-login); neither carried
    # an Authorization header.
    assert signin.call_count == 2
    for call in signin.calls:
        assert call.request.headers.get("Authorization") in (None, "")
    reg.close()


@respx.mock
def test_register_creates_and_attaches():
    _mock_list([])
    created = respx.post(f"{BASE}/api/mcp-servers").mock(
        return_value=httpx.Response(201, json={"id": "row-1"}),
    )
    attached = respx.put(
        f"{BASE}/api/agents/agents/{AGENT}/mcp-servers/row-1",
    ).mock(return_value=httpx.Response(204))

    reg = _registrar()
    assert reg.register("T028_x", "https://x.trycloudflare.com/mcp") == "row-1"
    assert attached.called
    assert created.calls[0].request.url.params["project_id"] == PROJECT
    payload = json.loads(created.calls[0].request.content)
    assert payload["transport"] == "http"
    assert payload["url"] == "https://x.trycloudflare.com/mcp"
    assert payload["name"] == "claweval-T028_x"
    assert payload["enabled"] is True
    reg.close()


@respx.mock
def test_register_replaces_stale_namesake():
    _mock_list([{"id": "stale", "name": "claweval-T028_x"}])
    detach = respx.delete(
        f"{BASE}/api/agents/agents/{AGENT}/mcp-servers/stale",
    ).mock(return_value=httpx.Response(404))  # crash left no attachment
    deleted = respx.delete(f"{BASE}/api/mcp-servers/stale").mock(
        return_value=httpx.Response(204),
    )
    respx.post(f"{BASE}/api/mcp-servers").mock(
        return_value=httpx.Response(201, json={"id": "row-2"}),
    )
    respx.put(
        f"{BASE}/api/agents/agents/{AGENT}/mcp-servers/row-2",
    ).mock(return_value=httpx.Response(204))

    reg = _registrar()
    assert reg.register("T028_x", "http://127.0.0.1:8321/mcp") == "row-2"
    assert detach.called
    assert deleted.called
    reg.close()


@respx.mock
def test_remove_detaches_before_delete():
    _mock_list([{"id": "row-1", "name": "claweval-T028_x"}])
    order: list[str] = []
    respx.delete(
        f"{BASE}/api/agents/agents/{AGENT}/mcp-servers/row-1",
    ).mock(side_effect=lambda _r: (order.append("detach"), httpx.Response(204))[1])
    respx.delete(f"{BASE}/api/mcp-servers/row-1").mock(
        side_effect=lambda _r: (order.append("delete"), httpx.Response(204))[1],
    )
    reg = _registrar()
    reg.remove("T028_x")
    assert order == ["detach", "delete"]
    reg.close()


@respx.mock
def test_cleanup_all_removes_only_claweval_rows():
    _mock_list([
        {"id": "a", "name": "claweval-T001_x"},
        {"id": "b", "name": "customer-mcp"},
    ])
    respx.delete(
        f"{BASE}/api/agents/agents/{AGENT}/mcp-servers/a",
    ).mock(return_value=httpx.Response(204))
    deleted = respx.delete(f"{BASE}/api/mcp-servers/a").mock(
        return_value=httpx.Response(204),
    )
    reg = _registrar()
    assert reg.cleanup_all() == 1
    assert deleted.called
    reg.close()
