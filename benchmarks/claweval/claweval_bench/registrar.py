"""Register the adapter as an MCP server through the ops control plane.

The harness's mcp-proxy loads MCP servers strictly from the agent's
runtime-config allow-list (``mcp_server_ids``), and that allow-list is
maintained by the **ops server** only: attaching a server to an agent
there updates the join table, resyncs the runtime config, and publishes
the cache invalidation every runtime pod listens for. A row written
straight into the harness registry is invisible to the agent, so the
registrar talks to ops — the same control plane the dashboard uses —
for both local and prod runs:

    POST   /api/mcp-servers?project_id=…                 create (+ mirror
                                                          into the harness
                                                          registry)
    PUT    /api/agents/agents/{agent}/mcp-servers/{id}   attach to agent
    DELETE /api/agents/agents/{agent}/mcp-servers/{id}   detach
    DELETE /api/mcp-servers/{id}                         delete row

The row is named per task (``claweval-<task_id>``) and removed right
after the task: reusing one row across tasks would trust every cache
between the proxy and the adapter to notice that the toolset behind an
unchanged URL changed, and a stale cache would leak one task's tools
into the next. Fresh name, fresh row, nothing to invalidate.

Auth is an ops user session: pass a ready JWT via ``CLAWEVAL_OPS_TOKEN``,
or a ``CLAWEVAL_OPS_USER`` / ``CLAWEVAL_OPS_PASSWORD`` pair and the
registrar logs in itself — routing the attempt the same way the
dashboard's login form does. Ops accounts come in two kinds
(``GET /api/auth/resolve-username``): *local* accounts authenticate at
``POST /api/auth/login``; *Firebase-backed* accounts have a random local
password they don't know, so the dashboard signs them into Firebase
(email + password against the identitytoolkit REST API, using the same
public web API key the deployed frontend ships) and exchanges the ID
token at ``POST /api/auth/firebase``. Google-SSO-only identities have no
password at all — those must use ``CLAWEVAL_OPS_TOKEN``.

Ops access tokens expire after an hour and a full run takes several, so
with credentials in hand an expired token is re-minted transparently on
the first 401.
"""
from __future__ import annotations

from typing import Any

import httpx

FIREBASE_SIGNIN_URL = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
)

# The public Firebase web API key baked into the deployed ops dashboard
# bundle (it identifies the Firebase project, it is not a secret).
# Override with CLAWEVAL_FIREBASE_API_KEY when the deployment changes
# Firebase projects.
DEFAULT_FIREBASE_API_KEY = "AIzaSyBqlo4z-dugjDARIaXtNEsHtmbmashBzgs"


def server_name(task_id: str) -> str:
    return f"claweval-{task_id}"[:120]


class RegistrarError(RuntimeError):
    """A control-plane call failed in a way the run cannot recover from."""


class Registrar:
    def __init__(
        self,
        base_url: str,
        project_id: str,
        agent_id: str,
        *,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        firebase_api_key: str = DEFAULT_FIREBASE_API_KEY,
    ) -> None:
        if not token and not (username and password):
            raise RegistrarError(
                "ops auth missing: set CLAWEVAL_OPS_TOKEN, or "
                "CLAWEVAL_OPS_USER and CLAWEVAL_OPS_PASSWORD"
            )
        self._base = base_url.rstrip("/")
        self._project_id = project_id
        self._agent_id = agent_id
        self._username = username
        self._password = password
        self._firebase_api_key = firebase_api_key
        self._http = httpx.Client(timeout=20.0)
        if token:
            self._http.headers["Authorization"] = f"Bearer {token}"
        else:
            self._login()

    # -- auth ----------------------------------------------------------

    def _account(self) -> dict[str, Any]:
        """Ask ops which backend this identity authenticates against."""
        if "@" in (self._username or ""):
            path, params = "/api/auth/resolve-email", {"email": self._username}
        else:
            path, params = (
                "/api/auth/resolve-username", {"username": self._username},
            )
        resp = self._http.get(f"{self._base}{path}", params=params)
        if resp.status_code == 404:
            raise RegistrarError(
                f"ops account {self._username!r} not found "
                f"(CLAWEVAL_OPS_USER takes the dashboard username or email)"
            )
        if resp.status_code >= 400:
            raise RegistrarError(
                f"ops account lookup failed (HTTP {resp.status_code}): "
                f"{resp.text[:300]}"
            )
        return resp.json()

    def _login_local(self) -> str:
        resp = self._http.post(
            f"{self._base}/api/auth/login",
            json={"username": self._username, "password": self._password},
        )
        if resp.status_code >= 400:
            raise RegistrarError(
                f"ops login failed (HTTP {resp.status_code}): {resp.text[:300]}"
            )
        return resp.json()["access_token"]

    def _login_firebase(self, email: str) -> str:
        # No Authorization header reaches this call: _login() pops any prior
        # bearer first. This is Google's public identitytoolkit, keyed by
        # ?key= alone -- a stray ops JWT makes it 401 "Expected OAuth 2
        # access token".
        signin = self._http.post(
            FIREBASE_SIGNIN_URL,
            params={"key": self._firebase_api_key},
            json={
                "email": email,
                "password": self._password,
                "returnSecureToken": True,
            },
        )
        if signin.status_code >= 400:
            raise RegistrarError(
                f"Firebase sign-in for {email} failed "
                f"(HTTP {signin.status_code}): {signin.text[:300]} -- a "
                "Google-SSO-only account has no password; use "
                "CLAWEVAL_OPS_TOKEN instead"
            )
        exchange = self._http.post(
            f"{self._base}/api/auth/firebase",
            json={"id_token": signin.json()["idToken"]},
        )
        if exchange.status_code >= 400:
            raise RegistrarError(
                f"ops Firebase token exchange failed "
                f"(HTTP {exchange.status_code}): {exchange.text[:300]}"
            )
        payload = exchange.json()
        if payload.get("status") != "authenticated":
            raise RegistrarError(
                "ops Firebase token exchange did not authenticate "
                f"(status {payload.get('status')!r}) -- the Firebase "
                "identity is not linked to a Surogate account"
            )
        return payload["access_token"]

    def _login(self) -> None:
        # Drop any prior bearer BEFORE the login calls. On a re-login (the
        # ops token expires ~hourly and a full run outlasts it) the client
        # still carries the expired ops JWT, and sending it to Google's
        # identitytoolkit makes Firebase reject the request with 401
        # "Expected OAuth 2 access token" -- the sign-in endpoint keys off
        # ?key=, never an Authorization header. The first login worked only
        # because no header was set yet; every re-login would fail without
        # this. Belt-and-suspenders with the explicit header on the Google
        # call in _login_firebase.
        self._http.headers.pop("Authorization", None)
        account = self._account()
        if account.get("kind") == "firebase":
            token = self._login_firebase(account["email"])
        else:
            token = self._login_local()
        self._http.headers["Authorization"] = f"Bearer {token}"

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """One control-plane call, re-authenticating once on an expired token."""
        resp = self._http.request(method, f"{self._base}{path}", **kwargs)
        if resp.status_code == 401 and self._username and self._password:
            self._login()
            resp = self._http.request(method, f"{self._base}{path}", **kwargs)
        return resp

    def _check(self, resp: httpx.Response, what: str) -> httpx.Response:
        if resp.status_code >= 400:
            raise RegistrarError(
                f"{what} failed (HTTP {resp.status_code}): {resp.text[:300]}"
            )
        return resp

    # -- rows ----------------------------------------------------------

    def _rows(self) -> list[dict[str, Any]]:
        resp = self._check(
            self._request(
                "GET", "/api/mcp-servers",
                params={"project_id": self._project_id, "limit": 500},
            ),
            "list mcp servers",
        )
        return resp.json().get("mcp_servers") or []

    def register(self, task_id: str, url: str, timeout: int = 120) -> str:
        """Create the task's MCP server row and attach it to the agent.

        Replaces any stale namesake first (crash residue), and returns
        the row id. The attach is what makes the tools visible: ops
        mirrors the row into the harness registry on create, then the
        attach writes the id into the agent's runtime-config allow-list
        and broadcasts the cache invalidation.
        """
        self.remove(task_id)
        resp = self._check(
            self._request(
                "POST", "/api/mcp-servers",
                params={"project_id": self._project_id},
                json={
                    "name": server_name(task_id),
                    "description": "Claw-Eval benchmark task adapter (transient)",
                    "transport": "http",
                    "url": url,
                    "timeout": timeout,
                    "enabled": True,
                },
            ),
            f"create mcp server for {task_id}",
        )
        server_id = resp.json()["id"]
        self._check(
            self._request(
                "PUT",
                f"/api/agents/agents/{self._agent_id}/mcp-servers/{server_id}",
            ),
            f"attach mcp server for {task_id}",
        )
        return server_id

    def _detach_and_delete(self, server_id: str) -> None:
        # Detach first: deleting an attached row trips the in-use guard
        # while the benchmark agent is running. A 404 means the join was
        # already gone (registration died between create and attach).
        detach = self._request(
            "DELETE",
            f"/api/agents/agents/{self._agent_id}/mcp-servers/{server_id}",
        )
        if detach.status_code >= 400 and detach.status_code != 404:
            raise RegistrarError(
                f"detach mcp server {server_id} failed "
                f"(HTTP {detach.status_code}): {detach.text[:300]}"
            )
        self._check(
            self._request("DELETE", f"/api/mcp-servers/{server_id}"),
            f"delete mcp server {server_id}",
        )

    def remove(self, task_id: str) -> None:
        name = server_name(task_id)
        for row in self._rows():
            if row.get("name") == name:
                self._detach_and_delete(row["id"])

    def cleanup_all(self) -> int:
        """Remove every claweval-* row (crash residue); returns the count."""
        removed = 0
        for row in self._rows():
            if str(row.get("name", "")).startswith("claweval-"):
                self._detach_and_delete(row["id"])
                removed += 1
        return removed

    def close(self) -> None:
        self._http.close()
