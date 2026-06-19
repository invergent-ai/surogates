# Browser Profiles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user save a browser's login/cookie state under a named, private "profile" and reuse it across agent tasks, capturing auth by logging in by hand over the CDP-free VNC live view.

**Architecture:** The surogates harness owns a `browser_profiles` table (surogates DB) plus capture/inject and a standalone `browser_setup` session; `surogate-ops` is a thin per-user-service-account proxy; the SDK adds a profile selector to the chat composer; Studio settings get a profile manager. Capture exports Playwright `storage_state` after a human login; inject applies it into a fresh context at browser-provision time, before registry publish / `browser.provisioned` / first navigation.

**Tech Stack:** Python 3.12 (async SQLAlchemy, FastAPI, cryptography Fernet, httpx, pytest), React 19 + TypeScript (SDK = vitest; Studio = Vite + vitest), kernel-images Playwright-execute browser REST.

## Global Constraints

- **Capture model:** cookies + Playwright `storage_state` only (no full `user-data-dir` sync in v1).
- **Principal model:** every profile is owned by exactly one principal — `user_id` XOR `service_account_id` — enforced by a DB CHECK constraint; ops-originated calls own profiles by the per-user `ops-chat-{org}-{ops_user_id}` service account (the harness sees `tenant.user_id = null`, `tenant.service_account_id` set).
- **Surogates DB schema is created via `Base.metadata.create_all`** (`surogates/db/engine.py:run_migrations`) — adding the ORM model is sufficient; there is **no** Alembic migration file for the surogates DB. Tests build schema with `Base.metadata.create_all`.
- **No `uv run` in `/work/surogate-ops`** — it reinstalls the pinned `surogates` wheel and clobbers the local dev install. Run ops Python via `/work/surogate-ops/.venv/bin/python -m pytest`. Run harness Python via `/work/surogates/.venv/bin/python -m pytest`.
- **Branch per change; Conventional Commits** (`type(scope): subject`); **no `Co-Authored-By` trailer**; **never** reference Plan/Task/Phase/Step numbers in code comments or commit messages.
- `storage_state_enc` is encrypted at rest with the existing Fernet key (`SUROGATES_ENCRYPTION_KEY`) and is **never** returned to any client.
- Ops→harness calls go through a per-`(agent, org)` `SurogatesApiClient`; for user-level profile calls, any running agent supplies the runtime transport (the `CodingAgentsSection` precedent) while the **service account** scopes the data.
- Browser tools other than the setup-only capture route keep rejecting with `paused_by_user` while a control lease is held.

---

## File Structure

**Harness — `/work/surogates`**
- `surogates/db/models.py` — add `BrowserProfile` model (modify).
- `surogates/browser/profiles.py` — `BrowserProfileStore` + `BrowserProfileRow` DTO (create).
- `surogates/browser/client.py` — `storage_state()` / `apply_storage_state()` on `KernelBrowserClient` (modify).
- `surogates/browser/base.py` — `BrowserSpec.storage_state` field (modify).
- `surogates/browser/pool.py` — inject in `ensure()` (modify).
- `surogates/tools/builtin/browser.py` — resolve `profile_id` → spec.storage_state (modify).
- `surogates/api/routes/browser_profiles.py` — `/v1/api/browser-profiles` router (create).
- `surogates/api/app.py` — build `browser_profile_store`, include router, wire tool dep (modify).
- `surogates/session/provisioning.py` — `browser_setup` channel helper if needed (modify).
- `tests/test_browser_profiles_store.py`, `tests/test_browser_profiles_routes.py`, `tests/test_browser_client_storage_state.py`, `tests/test_browser_pool_inject.py`, `tests/test_browser_tools_profile_inject.py` (create).

**Ops — `/work/surogate-ops`**
- `surogate_ops/server/routes/browser_profiles.py` — `/api/browser-profiles` proxy (create).
- `surogate_ops/server/routes/__init__.py` + `surogate_ops/server/app.py` — register router (modify).
- `surogate_ops/server/routes/sessions.py` — accept `browser_profile_id` on create (modify).
- `tests/test_browser_profiles_proxy.py` (create).

**SDK — `/work/surogates/sdk/agent-chat-react`**
- `src/types.ts` — `AgentChatBrowserProfile`, adapter `listBrowserProfiles` (modify).
- `src/components/chat/chat-composer.tsx` — profile selector popover (modify).
- `tests/browser-profile-selector.test.tsx` (create).

**Studio — `/work/surogate-ops/frontend`**
- `src/api/browser-profiles.ts` — client (create).
- `src/features/settings/browser-profiles-section.tsx` — manager UI (create).
- `src/features/settings/browser-profile-setup-dialog.tsx` — setup live-view dialog (create).
- `src/features/settings/profile-tab.tsx` — mount the section (modify).
- `src/features/work/work-agent-chat-adapter.ts` — `listBrowserProfiles` + `browser_profile_id` on create (modify).

---

## Task 1: `BrowserProfile` model

**Files:**
- Modify: `surogates/db/models.py`
- Test: `tests/test_browser_profiles_store.py`

**Interfaces:**
- Produces: `BrowserProfile` ORM model, table `browser_profiles`, columns `id, org_id, user_id, service_account_id, name, source, storage_state_enc, cookie_domains, created_at, last_used_at`.

- [ ] **Step 1: Write the failing test** — `tests/test_browser_profiles_store.py`

```python
import uuid
import pytest
from sqlalchemy import select
from surogates.db.models import BrowserProfile


@pytest.mark.asyncio
async def test_browser_profile_persists_for_service_account_principal(session_factory):
    org_id = uuid.uuid4()
    sa_id = uuid.uuid4()
    async with session_factory() as s:
        async with s.begin():
            s.add(BrowserProfile(
                org_id=org_id, service_account_id=sa_id, name="Personal",
            ))
        row = (await s.execute(
            select(BrowserProfile).where(BrowserProfile.org_id == org_id)
        )).scalar_one()
    assert row.name == "Personal"
    assert row.user_id is None
    assert row.service_account_id == sa_id
    assert row.source == "manual_vnc"
    assert row.cookie_domains == []
    assert row.storage_state_enc is None
```

This test relies on the existing `session_factory` fixture in `tests/integration/conftest.py` (it runs `Base.metadata.create_all`). Place the new test under `tests/integration/` if your conftest only exposes `session_factory` there; otherwise import the fixture path the repo uses. Confirm with `grep -n "def session_factory" tests/integration/conftest.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /work/surogates && .venv/bin/python -m pytest tests/integration/test_browser_profiles_store.py -v`
Expected: FAIL — `ImportError: cannot import name 'BrowserProfile'`.

- [ ] **Step 3: Add the model** — append to `surogates/db/models.py` (the imports `CheckConstraint, Index, text, JSONB, LargeBinary, func, UUID, ForeignKey, Mapped, mapped_column, Text` already exist at the top of the file):

```python
class BrowserProfile(Base):
    """A reusable, encrypted browser login state owned by one principal.

    Like ``ScheduledSession``, a profile is owned by exactly one principal —
    a human ``user_id`` or a ``service_account_id`` (the ops-chat SA that
    work-chat requests authenticate as). The CHECK enforces the XOR.
    """

    __tablename__ = "browser_profiles"
    __table_args__ = (
        CheckConstraint(
            "(user_id IS NOT NULL)::int + (service_account_id IS NOT NULL)::int = 1",
            name="ck_browser_profiles_one_principal",
        ),
        Index(
            "uq_browser_profiles_user_name",
            "org_id", "user_id", "name",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
        ),
        Index(
            "uq_browser_profiles_sa_name",
            "org_id", "service_account_id", "name",
            unique=True,
            postgresql_where=text("service_account_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    service_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_accounts.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'manual_vnc'")
    )
    storage_state_enc: Mapped[Optional[bytes]] = mapped_column(
        LargeBinary, nullable=True
    )
    cookie_domains: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        nullable=True
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /work/surogates && .venv/bin/python -m pytest tests/integration/test_browser_profiles_store.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git switch -c feat/browser-profiles-harness
git add surogates/db/models.py tests/integration/test_browser_profiles_store.py
git commit -m "feat(browser-profiles): add BrowserProfile model"
```

---

## Task 2: `BrowserProfileStore`

**Files:**
- Create: `surogates/browser/profiles.py`
- Modify: `surogates/api/app.py` (instantiate `app.state.browser_profile_store`)
- Test: `tests/integration/test_browser_profiles_store.py` (extend)

**Interfaces:**
- Consumes: `BrowserProfile` (Task 1); `async_sessionmaker`; Fernet `encryption_key: bytes`.
- Produces:
  - `BrowserProfileRow` dataclass: `id: UUID, name: str, source: str, cookie_domains: list[str], created_at: datetime, last_used_at: datetime | None, has_state: bool`.
  - `BrowserProfileStore(session_factory, encryption_key: bytes)` with:
    - `create(org_id, *, user_id, service_account_id, name) -> BrowserProfileRow`
    - `list(org_id, *, user_id, service_account_id) -> list[BrowserProfileRow]`
    - `rename(profile_id, org_id, *, user_id, service_account_id, name) -> bool`
    - `delete(profile_id, org_id, *, user_id, service_account_id) -> bool`
    - `save_capture(profile_id, org_id, *, user_id, service_account_id, storage_state: dict) -> BrowserProfileRow`
    - `storage_state_for(profile_id, org_id, *, user_id, service_account_id) -> dict | None`
    - `touch_last_used(profile_id, org_id, *, user_id, service_account_id) -> None`

- [ ] **Step 1: Write the failing tests** — extend `tests/integration/test_browser_profiles_store.py`:

```python
from cryptography.fernet import Fernet
from surogates.browser.profiles import BrowserProfileStore

_KEY = Fernet.generate_key()


def _store(session_factory):
    return BrowserProfileStore(session_factory, encryption_key=_KEY)


@pytest.mark.asyncio
async def test_create_list_scoped_to_principal(session_factory):
    store = _store(session_factory)
    org = uuid.uuid4()
    sa_a, sa_b = uuid.uuid4(), uuid.uuid4()
    a = await store.create(org, user_id=None, service_account_id=sa_a, name="A")
    await store.create(org, user_id=None, service_account_id=sa_b, name="B")
    rows = await store.list(org, user_id=None, service_account_id=sa_a)
    assert [r.name for r in rows] == ["A"]
    assert rows[0].id == a.id
    assert rows[0].has_state is False


@pytest.mark.asyncio
async def test_capture_roundtrip_and_cookie_domains(session_factory):
    store = _store(session_factory)
    org, sa = uuid.uuid4(), uuid.uuid4()
    p = await store.create(org, user_id=None, service_account_id=sa, name="P")
    state = {"cookies": [
        {"name": "SID", "domain": ".google.com", "value": "x"},
        {"name": "h", "domain": "github.com", "value": "y"},
    ], "origins": []}
    row = await store.save_capture(
        p.id, org, user_id=None, service_account_id=sa, storage_state=state
    )
    assert sorted(row.cookie_domains) == ["github.com", "google.com"]
    assert row.has_state is True
    got = await store.storage_state_for(
        p.id, org, user_id=None, service_account_id=sa
    )
    assert got == state


@pytest.mark.asyncio
async def test_storage_state_for_denies_foreign_principal(session_factory):
    store = _store(session_factory)
    org, sa, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    p = await store.create(org, user_id=None, service_account_id=sa, name="P")
    await store.save_capture(
        p.id, org, user_id=None, service_account_id=sa,
        storage_state={"cookies": [], "origins": []},
    )
    assert await store.storage_state_for(
        p.id, org, user_id=None, service_account_id=other
    ) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /work/surogates && .venv/bin/python -m pytest tests/integration/test_browser_profiles_store.py -v`
Expected: FAIL — `ModuleNotFoundError: surogates.browser.profiles`.

- [ ] **Step 3: Implement** — `surogates/browser/profiles.py`:

```python
"""Encrypted, principal-scoped browser login profiles."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete as sa_delete, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from surogates.db.models import BrowserProfile


@dataclass(slots=True)
class BrowserProfileRow:
    id: UUID
    name: str
    source: str
    cookie_domains: list[str]
    created_at: datetime
    last_used_at: datetime | None
    has_state: bool


def _row(p: BrowserProfile) -> BrowserProfileRow:
    return BrowserProfileRow(
        id=p.id,
        name=p.name,
        source=p.source,
        cookie_domains=list(p.cookie_domains or []),
        created_at=p.created_at,
        last_used_at=p.last_used_at,
        has_state=p.storage_state_enc is not None,
    )


def _cookie_domains(storage_state: dict) -> list[str]:
    seen: dict[str, None] = {}
    for cookie in storage_state.get("cookies", []) or []:
        domain = str(cookie.get("domain", "")).lstrip(".")
        if domain:
            seen.setdefault(domain, None)
    return sorted(seen)


class BrowserProfileStore:
    """CRUD + encrypted capture/inject for browser profiles.

    Every method takes ``(org_id, user_id, service_account_id)`` and filters on
    the exact principal so a profile is unreadable cross-principal even with a
    guessed id.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        encryption_key: bytes,
    ) -> None:
        self._session_factory = session_factory
        self._fernet = Fernet(encryption_key)

    @staticmethod
    def _principal_clause(user_id: UUID | None, service_account_id: UUID | None):
        if (user_id is None) == (service_account_id is None):
            raise ValueError("exactly one of user_id / service_account_id required")
        if user_id is not None:
            return BrowserProfile.user_id == user_id
        return BrowserProfile.service_account_id == service_account_id

    async def create(
        self,
        org_id: UUID,
        *,
        user_id: UUID | None,
        service_account_id: UUID | None,
        name: str,
    ) -> BrowserProfileRow:
        self._principal_clause(user_id, service_account_id)
        profile = BrowserProfile(
            org_id=org_id,
            user_id=user_id,
            service_account_id=service_account_id,
            name=name,
        )
        async with self._session_factory() as s:
            async with s.begin():
                s.add(profile)
            await s.refresh(profile)
            return _row(profile)

    async def list(
        self,
        org_id: UUID,
        *,
        user_id: UUID | None,
        service_account_id: UUID | None,
    ) -> list[BrowserProfileRow]:
        clause = self._principal_clause(user_id, service_account_id)
        async with self._session_factory() as s:
            rows = (await s.execute(
                select(BrowserProfile)
                .where(BrowserProfile.org_id == org_id, clause)
                .order_by(BrowserProfile.created_at.asc())
            )).scalars().all()
        return [_row(p) for p in rows]

    async def _get(
        self, s, profile_id, org_id, user_id, service_account_id
    ) -> BrowserProfile | None:
        clause = self._principal_clause(user_id, service_account_id)
        return (await s.execute(
            select(BrowserProfile).where(
                BrowserProfile.id == profile_id,
                BrowserProfile.org_id == org_id,
                clause,
            )
        )).scalar_one_or_none()

    async def rename(
        self, profile_id, org_id, *, user_id, service_account_id, name
    ) -> bool:
        clause = self._principal_clause(user_id, service_account_id)
        async with self._session_factory() as s:
            async with s.begin():
                result = await s.execute(
                    update(BrowserProfile)
                    .where(
                        BrowserProfile.id == profile_id,
                        BrowserProfile.org_id == org_id,
                        clause,
                    )
                    .values(name=name)
                )
        return result.rowcount > 0

    async def delete(
        self, profile_id, org_id, *, user_id, service_account_id
    ) -> bool:
        clause = self._principal_clause(user_id, service_account_id)
        async with self._session_factory() as s:
            async with s.begin():
                result = await s.execute(
                    sa_delete(BrowserProfile).where(
                        BrowserProfile.id == profile_id,
                        BrowserProfile.org_id == org_id,
                        clause,
                    )
                )
        return result.rowcount > 0

    async def save_capture(
        self, profile_id, org_id, *, user_id, service_account_id, storage_state: dict
    ) -> BrowserProfileRow:
        blob = self._fernet.encrypt(json.dumps(storage_state).encode("utf-8"))
        domains = _cookie_domains(storage_state)
        async with self._session_factory() as s:
            async with s.begin():
                profile = await self._get(
                    s, profile_id, org_id, user_id, service_account_id
                )
                if profile is None:
                    raise KeyError("profile not found")
                profile.storage_state_enc = blob
                profile.cookie_domains = domains
            await s.refresh(profile)
            return _row(profile)

    async def storage_state_for(
        self, profile_id, org_id, *, user_id, service_account_id
    ) -> dict | None:
        async with self._session_factory() as s:
            profile = await self._get(
                s, profile_id, org_id, user_id, service_account_id
            )
            if profile is None or profile.storage_state_enc is None:
                return None
            raw = profile.storage_state_enc
        try:
            return json.loads(self._fernet.decrypt(raw).decode("utf-8"))
        except InvalidToken:
            raise ValueError("failed to decrypt browser profile state")

    async def touch_last_used(
        self, profile_id, org_id, *, user_id, service_account_id
    ) -> None:
        clause = self._principal_clause(user_id, service_account_id)
        async with self._session_factory() as s:
            async with s.begin():
                await s.execute(
                    update(BrowserProfile)
                    .where(
                        BrowserProfile.id == profile_id,
                        BrowserProfile.org_id == org_id,
                        clause,
                    )
                    .values(last_used_at=datetime.now(timezone.utc))
                )
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd /work/surogates && .venv/bin/python -m pytest tests/integration/test_browser_profiles_store.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Wire into app startup** — in `surogates/api/app.py`, next to the `credential_vault` build (search `credential_vault`), add:

```python
from surogates.browser.profiles import BrowserProfileStore

# ... after app.state.credential_vault is built and settings.encryption_key is known:
app.state.browser_profile_store = (
    BrowserProfileStore(
        app.state.session_factory,
        encryption_key=settings.encryption_key.encode("utf-8"),
    )
    if settings.encryption_key
    else None
)
```

- [ ] **Step 6: Commit**

```bash
cd /work/surogates
git add surogates/browser/profiles.py surogates/api/app.py tests/integration/test_browser_profiles_store.py
git commit -m "feat(browser-profiles): add encrypted principal-scoped profile store"
```

---

## Task 3: `KernelBrowserClient` storage_state helpers

**Files:**
- Modify: `surogates/browser/client.py`
- Test: `tests/test_browser_client_storage_state.py`

**Interfaces:**
- Consumes: existing `KernelBrowserClient._playwright_execute(code, *, timeout_sec=60)`.
- Produces: `async def storage_state(self) -> dict`; `async def apply_storage_state(self, state: dict) -> None`.

- [ ] **Step 1: Write the failing test** — `tests/test_browser_client_storage_state.py`:

```python
import json
import pytest
from surogates.browser.client import KernelBrowserClient


@pytest.mark.asyncio
async def test_storage_state_returns_execute_result(monkeypatch):
    client = KernelBrowserClient("http://browser:10001")
    captured = {}

    async def fake_exec(code, *, timeout_sec=60):
        captured["code"] = code
        return {"cookies": [{"name": "SID"}], "origins": []}

    monkeypatch.setattr(client, "_playwright_execute", fake_exec)
    state = await client.storage_state()
    assert state["cookies"][0]["name"] == "SID"
    assert "storageState()" in captured["code"]
    await client.close()


@pytest.mark.asyncio
async def test_apply_storage_state_adds_cookies(monkeypatch):
    client = KernelBrowserClient("http://browser:10001")
    captured = {}

    async def fake_exec(code, *, timeout_sec=60):
        captured["code"] = code
        return None

    monkeypatch.setattr(client, "_playwright_execute", fake_exec)
    state = {"cookies": [{"name": "SID", "domain": ".google.com", "value": "x"}],
             "origins": [{"origin": "https://google.com",
                          "localStorage": [{"name": "k", "value": "v"}]}]}
    await client.apply_storage_state(state)
    assert "addCookies" in captured["code"]
    assert json.dumps(state["cookies"]) in captured["code"]
    await client.close()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /work/surogates && .venv/bin/python -m pytest tests/test_browser_client_storage_state.py -v`
Expected: FAIL — `AttributeError: 'KernelBrowserClient' object has no attribute 'storage_state'`.

- [ ] **Step 3: Implement** — add to `KernelBrowserClient` in `surogates/browser/client.py` (after `navigate`):

```python
async def storage_state(self) -> dict[str, Any]:
    """Export the live context's cookies + per-origin localStorage."""
    code = "return await page.context().storageState();"
    result = await self._playwright_execute(code)
    return result or {"cookies": [], "origins": []}

async def apply_storage_state(self, state: dict[str, Any]) -> None:
    """Inject cookies (and best-effort localStorage) into the live context.

    Cookies are applied directly to the existing context. localStorage is
    seeded per origin (best effort): a fresh context cannot be created on an
    already-running browser, so we navigate to each origin and set its items.
    """
    cookies_json = json.dumps(state.get("cookies", []) or [])
    origins_json = json.dumps(state.get("origins", []) or [])
    code = (
        "const context = page.context();\n"
        f"await context.addCookies({cookies_json});\n"
        f"for (const o of {origins_json}) {{\n"
        "  try {\n"
        "    await page.goto(o.origin, {waitUntil: 'domcontentloaded'});\n"
        "    await page.evaluate((items) => {\n"
        "      for (const it of items) localStorage.setItem(it.name, it.value);\n"
        "    }, o.localStorage || []);\n"
        "  } catch (e) { /* best-effort per origin */ }\n"
        "}\n"
        "return true;"
    )
    await self._playwright_execute(code)
    self._invalidate_snapshot_cache()
```

Ensure `import json` is present at the top of `client.py` (add it if missing).

- [ ] **Step 4: Run to verify it passes**

Run: `cd /work/surogates && .venv/bin/python -m pytest tests/test_browser_client_storage_state.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/browser/client.py tests/test_browser_client_storage_state.py
git commit -m "feat(browser-profiles): add storage_state export/apply to browser client"
```

---

## Task 4: Inject `storage_state` at provision in `BrowserPool.ensure()`

**Files:**
- Modify: `surogates/browser/base.py` (`BrowserSpec.storage_state`)
- Modify: `surogates/browser/pool.py` (`ensure()`)
- Test: `tests/test_browser_pool_inject.py`

**Interfaces:**
- Consumes: `KernelBrowserClient.apply_storage_state` (Task 3).
- Produces: `BrowserSpec.storage_state: dict | None = None`; injection happens in the new-provision branch **after** `backend.provision()` returns and **before** `registry.set(...)` / the `BROWSER_PROVISIONED` event.

- [ ] **Step 1: Write the failing test** — `tests/test_browser_pool_inject.py`:

```python
import pytest
from surogates.browser.base import BrowserSpec, BrowserEndpoint, BrowserStatus
from surogates.browser.pool import BrowserPool


class _FakeBackend:
    async def provision(self, spec, *, session_id, org_id, user_id):
        return "bid-1", BrowserEndpoint(
            rest_url="http://b:10001", cdp_url="ws://b:9222",
            live_view_url="ws://b:8080",
        )
    async def status(self, browser_id):
        return BrowserStatus.RUNNING


@pytest.mark.asyncio
async def test_inject_applies_state_before_registry_publish(monkeypatch):
    order = []

    class _Registry:
        async def set(self, entry):
            order.append("registry")

    applied = {}

    class _FakeClient:
        def __init__(self, rest_url, **kw):
            applied["rest_url"] = rest_url
        async def apply_storage_state(self, state):
            order.append("apply")
            applied["state"] = state
        async def close(self):
            pass

    monkeypatch.setattr("surogates.browser.pool.KernelBrowserClient", _FakeClient)

    pool = BrowserPool(
        backend=_FakeBackend(), registry=_Registry(),
        event_emitter=_make_recording_emitter(order),
    )
    spec = BrowserSpec(storage_state={"cookies": [{"name": "SID"}], "origins": []})
    await pool.ensure(session_id="s1", org_id="o1", user_id="u1", spec=spec)

    assert order.index("apply") < order.index("registry")
    assert applied["state"]["cookies"][0]["name"] == "SID"
```

Add a tiny helper at the top of the test file that builds whatever `event_emitter` shape `BrowserPool.__init__` expects — inspect the real constructor first with `grep -n "def __init__" surogates/browser/pool.py` and mirror its parameters. The assertion that matters is `order.index("apply") < order.index("registry")`.

- [ ] **Step 2: Run to verify it fails**

Run: `cd /work/surogates && .venv/bin/python -m pytest tests/test_browser_pool_inject.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'storage_state'`.

- [ ] **Step 3: Add the spec field** — in `surogates/browser/base.py`, add to the `BrowserSpec` dataclass:

```python
    # When set, the pool injects this Playwright storage_state into the fresh
    # context at provision time (before registry publish / navigation).
    storage_state: dict | None = None
```

- [ ] **Step 4: Inject in `ensure()`** — in `surogates/browser/pool.py`, in the new-provision branch, immediately after `backend.provision(...)` returns and **before** `self._registry.set(...)`:

```python
        browser_id, endpoint = await self._backend.provision(
            spec, session_id=session_id, org_id=org_id, user_id=user_id,
        )
        if spec.storage_state:
            client = KernelBrowserClient(endpoint.rest_url)
            try:
                await client.apply_storage_state(spec.storage_state)
            finally:
                await client.close()
        slot = _Slot(browser_id=browser_id, endpoint=endpoint, snapshot_cache={})
        self._mapping[session_id] = slot
        await self._registry.set(BrowserEntry(...))  # unchanged
```

Add `from surogates.browser.client import KernelBrowserClient` to `pool.py` imports.

- [ ] **Step 5: Run to verify it passes**

Run: `cd /work/surogates && .venv/bin/python -m pytest tests/test_browser_pool_inject.py -v`
Expected: PASS.

- [ ] **Step 6: Run the existing browser-pool tests for regressions**

Run: `cd /work/surogates && .venv/bin/python -m pytest tests/ -k "browser_pool or pool" -v`
Expected: PASS (no regressions; injection is gated on `spec.storage_state`).

- [ ] **Step 7: Commit**

```bash
cd /work/surogates
git add surogates/browser/base.py surogates/browser/pool.py tests/test_browser_pool_inject.py
git commit -m "feat(browser-profiles): inject storage_state at provision before registry publish"
```

---

## Task 5: Resolve `profile_id` → spec.storage_state at the tool layer

**Files:**
- Modify: `surogates/tools/builtin/browser.py` (`_resolve_session_browser`)
- Modify: `surogates/api/app.py` and the harness tool-dispatch wiring to pass `browser_profile_store`
- Test: `tests/test_browser_tools_profile_inject.py`

**Interfaces:**
- Consumes: `BrowserProfileStore.storage_state_for`, `.touch_last_used` (Task 2); `session_config["browser"]["profile_id"]`; tenant `org_id` / `user_id` / `service_account_id`.
- Produces: when a profile is named and owned by the session principal, `BrowserSpec.storage_state` is set before `browser_pool.ensure(...)`.

- [ ] **Step 1: Write the failing test** — `tests/test_browser_tools_profile_inject.py`:

```python
import uuid
import pytest
from surogates.tools.builtin.browser import _resolve_session_browser
from surogates.browser.base import BrowserEndpoint, BrowserStatus


class _Tenant:
    def __init__(self, org_id, user_id=None, service_account_id=None):
        self.org_id = org_id
        self.user_id = user_id
        self.service_account_id = service_account_id


class _Pool:
    def __init__(self):
        self.spec = None
    async def ensure(self, *, session_id, org_id, user_id, spec):
        self.spec = spec
        from surogates.browser.pool import EnsureResult
        return EnsureResult("bid", BrowserEndpoint("http://b", "ws://c", "ws://l"),
                            True, {})


class _Store:
    def __init__(self, state):
        self._state = state
        self.touched = False
    async def storage_state_for(self, profile_id, org_id, *, user_id, service_account_id):
        return self._state
    async def touch_last_used(self, *a, **k):
        self.touched = True


@pytest.mark.asyncio
async def test_profile_state_is_set_on_spec():
    org, sa, pid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    pool = _Pool()
    store = _Store({"cookies": [{"name": "SID"}], "origins": []})
    result = await _resolve_session_browser(
        tenant=_Tenant(org, service_account_id=sa),
        session_id="s1",
        browser_pool=pool,
        browser_control=None,
        browser_profile_store=store,
        session_config={"browser": {"profile_id": str(pid)},
                        "service_account_id": str(sa)},
    )
    assert not isinstance(result, str)
    assert pool.spec.storage_state == {"cookies": [{"name": "SID"}], "origins": []}
    assert store.touched is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /work/surogates && .venv/bin/python -m pytest tests/test_browser_tools_profile_inject.py -v`
Expected: FAIL — `_resolve_session_browser` does not accept `browser_profile_store`.

- [ ] **Step 3: Implement** — modify `_resolve_session_browser` in `surogates/tools/builtin/browser.py`. Add the `browser_profile_store=None` parameter, and after computing `browser_spec` but before `browser_pool.ensure(...)`:

```python
        profile_id = (session_config or {}).get("browser", {}).get("profile_id")
        if profile_id and browser_profile_store is not None:
            org_id = getattr(tenant, "org_id", None)
            user_id = getattr(tenant, "user_id", None)
            sa_raw = (session_config or {}).get("service_account_id")
            service_account_id = (
                getattr(tenant, "service_account_id", None)
                or (UUID(sa_raw) if sa_raw else None)
            )
            state = await browser_profile_store.storage_state_for(
                UUID(str(profile_id)),
                org_id,
                user_id=user_id,
                service_account_id=service_account_id,
            )
            if state is not None:
                browser_spec.storage_state = state
                await browser_profile_store.touch_last_used(
                    UUID(str(profile_id)),
                    org_id,
                    user_id=user_id,
                    service_account_id=service_account_id,
                )
```

Ensure `from uuid import UUID` is imported in `browser.py`. Then thread `browser_profile_store` from the tool dispatch: find where `browser_pool=` is passed into the browser handlers (`grep -n "browser_pool=" surogates/harness/tool_exec.py surogates/tools/builtin/browser.py`) and pass `browser_profile_store=request.app.state.browser_profile_store` (or the dispatch's app-state handle) the same way.

- [ ] **Step 4: Run to verify it passes**

Run: `cd /work/surogates && .venv/bin/python -m pytest tests/test_browser_tools_profile_inject.py -v`
Expected: PASS.

- [ ] **Step 5: Run existing browser-tool tests for regressions**

Run: `cd /work/surogates && .venv/bin/python -m pytest tests/test_browser_tools.py -v`
Expected: PASS (the new param defaults to `None`).

- [ ] **Step 6: Commit**

```bash
cd /work/surogates
git add surogates/tools/builtin/browser.py surogates/api/app.py tests/test_browser_tools_profile_inject.py
git commit -m "feat(browser-profiles): inject profile storage_state into agent browser sessions"
```

---

## Task 6: Harness `/v1/api/browser-profiles` CRUD router

**Files:**
- Create: `surogates/api/routes/browser_profiles.py`
- Modify: `surogates/api/app.py` (include router)
- Test: `tests/test_browser_profiles_routes.py`

**Interfaces:**
- Consumes: `BrowserProfileStore` (Task 2); `TenantContext` (`org_id`, `user_id`, `service_account_id`); the dual `/api` + `/v1/api` prefix pattern from `surogates/api/routes/browser.py`.
- Produces: routes `GET /`, `POST /`, `PATCH /{id}`, `DELETE /{id}` returning `BrowserProfileOut` (metadata only — `id, name, source, cookie_domains, created_at, last_used_at, has_state`).

- [ ] **Step 1: Write the failing test** — `tests/test_browser_profiles_routes.py`. Mirror the structure of `tests/test_browser_route_ws.py` (build a `FastAPI`, `app.include_router`, set `app.state.browser_profile_store`, monkeypatch the tenant dependency). Concretely:

```python
import uuid
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
import surogates.api.routes.browser_profiles as bp
from surogates.tenant.context import TenantContext


class _FakeStore:
    def __init__(self):
        self.rows = []
    async def list(self, org_id, *, user_id, service_account_id):
        return list(self.rows)
    async def create(self, org_id, *, user_id, service_account_id, name):
        from surogates.browser.profiles import BrowserProfileRow
        from datetime import datetime, timezone
        row = BrowserProfileRow(uuid.uuid4(), name, "manual_vnc", [],
                                datetime.now(timezone.utc), None, False)
        self.rows.append(row)
        return row


def _app(store, *, user_id=None, sa_id=uuid.uuid4()):
    app = FastAPI()
    app.include_router(bp.router)
    app.state.browser_profile_store = store

    async def _tenant():
        return TenantContext(
            org_id=uuid.uuid4(), user_id=user_id, org_config={},
            user_preferences={}, permissions=frozenset(),
            asset_root="/tmp", service_account_id=sa_id,
        )
    app.dependency_overrides[bp.get_current_tenant] = _tenant
    return app


def test_create_then_list_returns_metadata_only():
    store = _FakeStore()
    client = TestClient(_app(store))
    created = client.post("/api/browser-profiles", json={"name": "Personal"})
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "Personal" and body["has_state"] is False
    assert "storage_state_enc" not in body
    listed = client.get("/api/browser-profiles").json()
    assert [p["name"] for p in listed] == ["Personal"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /work/surogates && .venv/bin/python -m pytest tests/test_browser_profiles_routes.py -v`
Expected: FAIL — `ModuleNotFoundError: surogates.api.routes.browser_profiles`.

- [ ] **Step 3: Implement** — `surogates/api/routes/browser_profiles.py`:

```python
"""Browser-profile CRUD + setup/capture routes (dual /api + /v1/api prefix)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from surogates.browser.profiles import BrowserProfileRow
from surogates.tenant.context import TenantContext
from surogates.api.deps import get_current_tenant  # confirm exact import path

router = APIRouter()


class CreateProfileRequest(BaseModel):
    name: str | None = None


class RenameProfileRequest(BaseModel):
    name: str


class BrowserProfileOut(BaseModel):
    id: str
    name: str
    source: str
    cookie_domains: list[str]
    created_at: str
    last_used_at: str | None
    has_state: bool

    @classmethod
    def of(cls, row: BrowserProfileRow) -> "BrowserProfileOut":
        return cls(
            id=str(row.id), name=row.name, source=row.source,
            cookie_domains=row.cookie_domains,
            created_at=row.created_at.isoformat(),
            last_used_at=row.last_used_at.isoformat() if row.last_used_at else None,
            has_state=row.has_state,
        )


def _principal(tenant: TenantContext, request: Request) -> tuple[UUID | None, UUID | None]:
    """Resolve (user_id, service_account_id) — exactly one is non-null."""
    if tenant.user_id is not None:
        return tenant.user_id, None
    if tenant.service_account_id is not None:
        return None, tenant.service_account_id
    raise HTTPException(status_code=403, detail="Browser profiles require a principal.")


def _store(request: Request):
    store = getattr(request.app.state, "browser_profile_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Browser profiles unavailable.")
    return store


@router.get("/api/browser-profiles")
@router.get("/v1/api/browser-profiles")
async def list_profiles(
    request: Request, tenant: TenantContext = Depends(get_current_tenant),
) -> list[BrowserProfileOut]:
    user_id, sa_id = _principal(tenant, request)
    rows = await _store(request).list(
        tenant.org_id, user_id=user_id, service_account_id=sa_id
    )
    return [BrowserProfileOut.of(r) for r in rows]


@router.post("/api/browser-profiles", status_code=201)
@router.post("/v1/api/browser-profiles", status_code=201)
async def create_profile(
    body: CreateProfileRequest, request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> BrowserProfileOut:
    user_id, sa_id = _principal(tenant, request)
    row = await _store(request).create(
        tenant.org_id, user_id=user_id, service_account_id=sa_id,
        name=(body.name or "Profile").strip() or "Profile",
    )
    return BrowserProfileOut.of(row)


@router.patch("/api/browser-profiles/{profile_id}")
@router.patch("/v1/api/browser-profiles/{profile_id}")
async def rename_profile(
    profile_id: UUID, body: RenameProfileRequest, request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict[str, bool]:
    user_id, sa_id = _principal(tenant, request)
    ok = await _store(request).rename(
        profile_id, tenant.org_id, user_id=user_id, service_account_id=sa_id,
        name=body.name.strip(),
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"renamed": True}


@router.delete("/api/browser-profiles/{profile_id}", status_code=204)
@router.delete("/v1/api/browser-profiles/{profile_id}", status_code=204)
async def delete_profile(
    profile_id: UUID, request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> None:
    user_id, sa_id = _principal(tenant, request)
    await _store(request).delete(
        profile_id, tenant.org_id, user_id=user_id, service_account_id=sa_id,
    )
```

Confirm the real import path of `get_current_tenant` with `grep -rn "def get_current_tenant\|get_current_tenant" surogates/api | head` and fix the import. Mirror exactly how `surogates/api/routes/browser.py` imports it.

- [ ] **Step 4: Run to verify it passes**

Run: `cd /work/surogates && .venv/bin/python -m pytest tests/test_browser_profiles_routes.py -v`
Expected: PASS.

- [ ] **Step 5: Include the router** — in `surogates/api/app.py`, where `browser` routes are included (`grep -n "routes.browser\|include_router" surogates/api/app.py`), add:

```python
from surogates.api.routes import browser_profiles
app.include_router(browser_profiles.router)
```

- [ ] **Step 6: Commit**

```bash
cd /work/surogates
git add surogates/api/routes/browser_profiles.py surogates/api/app.py tests/test_browser_profiles_routes.py
git commit -m "feat(browser-profiles): add harness CRUD routes"
```

---

## Task 7: Harness setup-session route + `browser_setup` channel

**Files:**
- Modify: `surogates/api/routes/browser_profiles.py` (add `POST /{id}/setup-session`)
- Test: `tests/test_browser_profiles_routes.py` (extend)

**Interfaces:**
- Consumes: `create_agent_session` (`surogates/session/provisioning.py`), `BrowserPool.ensure`, `BrowserControlStore.acquire`, the app-state `session_store`/`storage`/`settings`/`browser_pool`/`browser_control`.
- Produces: `POST /api/browser-profiles/{id}/setup-session` body `{owner_user_id?, agent_id?, setup_spec?: {proxy?: null}}` → `{"session_id": str, "expires_at": iso8601}`. Creates a `channel="browser_setup"` session that does **not** enqueue a harness wake; provisions a browser; grants the owner control.

- [ ] **Step 1: Write the failing test** — extend `tests/test_browser_profiles_routes.py`:

```python
def test_setup_session_creates_browser_setup_without_wake(monkeypatch):
    store = _FakeStore()
    app = _app(store)
    created = {}
    waked = {"called": False}

    class _Store2:
        async def create_session(self, **kw):
            created.update(kw)
            class _S: id = kw["session_id"]; config = kw["config"]
            return _S()

    async def _ensure(**kw):
        return None

    class _Control:
        async def acquire(self, sid, uid):
            from surogates.browser.control import AcquireOutcome, ControlEntry
            from datetime import datetime, timezone
            return AcquireOutcome.GRANTED, ControlEntry(uid, datetime.now(timezone.utc))

    # set the profile up so the route can find it
    import asyncio
    row = asyncio.get_event_loop().run_until_complete(
        store.create(uuid.uuid4(), user_id=None, service_account_id=uuid.uuid4(), name="P")
    )
    app.state.session_store = _Store2()
    app.state.browser_pool = type("P", (), {"ensure": staticmethod(_ensure)})()
    app.state.browser_control = _Control()
    app.state.session_wake = lambda sid: waked.update(called=True)
    # storage/settings stubs as needed by create_agent_session — see Step 3

    client = TestClient(app)
    resp = client.post(
        f"/api/browser-profiles/{row.id}/setup-session",
        json={"owner_user_id": "ops-user", "agent_id": "agent-1"},
    )
    assert resp.status_code == 200
    assert created["channel"] == "browser_setup"
    assert waked["called"] is False
    assert "session_id" in resp.json() and "expires_at" in resp.json()
```

This test will need `create_agent_session`'s storage/settings dependencies stubbed. Before writing the final assertions, read `surogates/session/provisioning.py:create_agent_session` and stub `app.state.storage` (needs `create_bucket`, `resolve_workspace_path`) and `app.state.settings` (needs `storage.bucket`, `llm.model`) — or, preferred, factor the setup-session body to call a thin internal `_create_browser_setup_session(...)` you can unit-test directly with a fake `session_store`. Keep the assertion `created["channel"] == "browser_setup"` and `waked["called"] is False`.

- [ ] **Step 2: Run to verify it fails**

Run: `cd /work/surogates && .venv/bin/python -m pytest tests/test_browser_profiles_routes.py -k setup -v`
Expected: FAIL — route does not exist.

- [ ] **Step 3: Implement** — add to `surogates/api/routes/browser_profiles.py`:

```python
from datetime import datetime, timedelta, timezone
from surogates.session.provisioning import create_agent_session
from surogates.browser.base import BrowserSpec

_SETUP_TTL_SECONDS = 15 * 60


class SetupSessionRequest(BaseModel):
    owner_user_id: str | None = None
    agent_id: str | None = None
    setup_spec: dict | None = None


@router.post("/api/browser-profiles/{profile_id}/setup-session")
@router.post("/v1/api/browser-profiles/{profile_id}/setup-session")
async def create_setup_session(
    profile_id: UUID, body: SetupSessionRequest, request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict[str, str]:
    user_id, sa_id = _principal(tenant, request)
    store = _store(request)

    # Profile must exist + belong to the caller before we burn a browser pod.
    rows = await store.list(tenant.org_id, user_id=user_id, service_account_id=sa_id)
    if not any(r.id == profile_id for r in rows):
        raise HTTPException(status_code=404, detail="Profile not found")

    owner = body.owner_user_id or (str(tenant.user_id) if tenant.user_id else None)
    if not owner:
        raise HTTPException(status_code=403, detail="Setup requires an owner user id.")

    setup_spec = body.setup_spec or {}
    if setup_spec.get("proxy") is not None:
        raise HTTPException(status_code=400, detail="Egress proxy is not yet supported.")

    settings = request.app.state.settings
    session = await create_agent_session(
        store=request.app.state.session_store,
        storage=request.app.state.storage,
        settings=settings,
        org_id=tenant.org_id,
        user_id=user_id,
        agent_id=body.agent_id or "browser-setup",
        channel="browser_setup",
        model=settings.llm.model,
        config={"browser": {"profile_id": str(profile_id), "setup_spec": setup_spec}},
        service_account_id=sa_id,
    )
    sid = str(session.id)

    # Provision a bare browser (empty profile → no injection) and grant control.
    # Deliberately do NOT enqueue_session / session_wake: no agent loop runs.
    pool = request.app.state.browser_pool
    await pool.ensure(
        session_id=sid, org_id=str(tenant.org_id),
        user_id=owner,
        spec=BrowserSpec(active_deadline_seconds=_SETUP_TTL_SECONDS),
    )
    await request.app.state.browser_control.acquire(sid, owner)

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=_SETUP_TTL_SECONDS)
    return {"session_id": sid, "expires_at": expires_at.isoformat()}
```

Confirm `BrowserSpec` exposes `active_deadline_seconds` (it does, per `surogates/browser/base.py`). If `create_agent_session` rejects an unknown `agent_id`, pass a sentinel that the provisioning path tolerates, or relax it — verify by reading the function; the only requirement is a `browser_setup` session row with the profile id in config.

- [ ] **Step 4: Run to verify it passes**

Run: `cd /work/surogates && .venv/bin/python -m pytest tests/test_browser_profiles_routes.py -k setup -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /work/surogates
git add surogates/api/routes/browser_profiles.py tests/test_browser_profiles_routes.py
git commit -m "feat(browser-profiles): add browser_setup session provisioning route"
```

---

## Task 8: Harness capture route

**Files:**
- Modify: `surogates/api/routes/browser_profiles.py` (add `POST /{id}/capture`)
- Test: `tests/test_browser_profiles_routes.py` (extend)

**Interfaces:**
- Consumes: `browser_resolver` (resolve session → endpoint), `browser_control.held_by`, `session_store.get_session` (to check `channel`/`config`), `KernelBrowserClient.storage_state` (Task 3), `BrowserProfileStore.save_capture` (Task 2).
- Produces: `POST /api/browser-profiles/{id}/capture?session_id=…` body `{owner_user_id?}` → `BrowserProfileOut`. Guards: session `channel == "browser_setup"`; `config.browser.profile_id == id`; caller holds the control lease; profile belongs to caller.

- [ ] **Step 1: Write the failing test** — extend `tests/test_browser_profiles_routes.py`:

```python
def test_capture_rejects_non_setup_session(monkeypatch):
    store = _FakeStore()
    app = _app(store)
    import asyncio
    row = asyncio.get_event_loop().run_until_complete(
        store.create(uuid.uuid4(), user_id=None, service_account_id=uuid.uuid4(), name="P")
    )

    class _SessionStore:
        async def get_session(self, sid):
            class _S:
                channel = "web"  # not browser_setup
                config = {"browser": {"profile_id": str(row.id)}}
            return _S()

    app.state.session_store = _SessionStore()
    app.state.browser_control = type("C", (), {
        "held_by": staticmethod(lambda sid: _async("ops-user"))})()
    app.state.browser_resolver = type("R", (), {
        "resolve": staticmethod(lambda sid, expected_org_id=None: _async(object()))})()

    client = TestClient(app)
    resp = client.post(
        f"/api/browser-profiles/{row.id}/capture",
        params={"session_id": str(uuid.uuid4())},
        json={"owner_user_id": "ops-user"},
    )
    assert resp.status_code == 409  # not a browser_setup session
```

Add an `async def _async(v): return v` helper. The happy-path test (separate) should stub a resolver returning an endpoint, `held_by` returning the owner, a `browser_setup` session whose config names the profile, and a `KernelBrowserClient` whose `storage_state()` returns a fixed dict, then assert `save_capture` was called and the response `has_state is True`. Monkeypatch `bp.KernelBrowserClient` like Task 4 monkeypatches the pool's client.

- [ ] **Step 2: Run to verify it fails**

Run: `cd /work/surogates && .venv/bin/python -m pytest tests/test_browser_profiles_routes.py -k capture -v`
Expected: FAIL — route does not exist.

- [ ] **Step 3: Implement** — add to `surogates/api/routes/browser_profiles.py`:

```python
from surogates.browser.client import KernelBrowserClient


class CaptureRequest(BaseModel):
    owner_user_id: str | None = None


@router.post("/api/browser-profiles/{profile_id}/capture")
@router.post("/v1/api/browser-profiles/{profile_id}/capture")
async def capture_profile(
    profile_id: UUID, session_id: UUID, body: CaptureRequest, request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> BrowserProfileOut:
    user_id, sa_id = _principal(tenant, request)
    store = _store(request)

    owner = body.owner_user_id or (str(tenant.user_id) if tenant.user_id else None)
    if not owner:
        raise HTTPException(status_code=403, detail="Capture requires an owner user id.")

    session = await request.app.state.session_store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.channel != "browser_setup":
        raise HTTPException(status_code=409, detail="Not a browser-setup session.")
    if str((session.config or {}).get("browser", {}).get("profile_id")) != str(profile_id):
        raise HTTPException(status_code=409, detail="Session is not bound to this profile.")

    holder = await request.app.state.browser_control.held_by(str(session_id))
    if holder != owner:
        raise HTTPException(status_code=403, detail="Caller does not hold control.")

    resolved = await request.app.state.browser_resolver.resolve(
        str(session_id), expected_org_id=str(tenant.org_id),
    )
    if resolved is None:
        raise HTTPException(status_code=404, detail="No browser for session")

    client = KernelBrowserClient(resolved.endpoint.rest_url)
    try:
        state = await client.storage_state()
    finally:
        await client.close()

    row = await store.save_capture(
        profile_id, tenant.org_id, user_id=user_id, service_account_id=sa_id,
        storage_state=state,
    )
    return BrowserProfileOut.of(row)
```

Confirm `resolved.endpoint.rest_url` matches what `browser_resolver.resolve(...)` returns in `surogates/api/routes/browser.py` (the resolver returns a resolved object with `.endpoint`). Adjust attribute access to match.

- [ ] **Step 4: Run to verify it passes**

Run: `cd /work/surogates && .venv/bin/python -m pytest tests/test_browser_profiles_routes.py -k capture -v`
Expected: PASS.

- [ ] **Step 5: Run the whole harness browser-profiles suite**

Run: `cd /work/surogates && .venv/bin/python -m pytest tests/ -k "browser_profile" -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /work/surogates
git add surogates/api/routes/browser_profiles.py tests/test_browser_profiles_routes.py
git commit -m "feat(browser-profiles): add control-guarded storage_state capture route"
```

---

## Task 9: Ops `/api/browser-profiles` proxy

**Files:**
- Create: `surogate_ops/server/routes/browser_profiles.py`
- Modify: `surogate_ops/server/routes/__init__.py`, `surogate_ops/server/app.py`
- Test: `tests/test_browser_profiles_proxy.py`

**Interfaces:**
- Consumes: `_build_live_agent_client`, `_ops_chat_service_account_name`, `_ops_chat_credential_name`, `_resolve_current_ops_user_id`, `get_current_subject`, `get_session`, `_resolve_live_agent` (all in `surogate_ops/server/routes/sessions.py`). The browser-profile call takes `agent_id` (any running agent) as transport; the harness scopes by the per-user service account.
- Produces: ops routes under `/api/browser-profiles` forwarding to `/v1/api/browser-profiles…`, always injecting `owner_user_id = subject`.

- [ ] **Step 1: Write the failing test** — `tests/test_browser_profiles_proxy.py`:

```python
import pytest
from surogate_ops.server.routes import browser_profiles as bp


@pytest.mark.asyncio
async def test_list_forwards_to_harness(monkeypatch):
    calls = {}

    class _Client:
        async def request_json(self, method, path, **kw):
            calls["method"] = method
            calls["path"] = path
            return 200, [{"id": "p1", "name": "P"}]

    async def _fake_build(agent_id, request, ops_session, **kw):
        return _Client()

    monkeypatch.setattr(bp, "_build_live_agent_client", _fake_build)
    result = await bp._forward_list(
        agent_id="a1", request=object(), ops_session=object(),
        subject="ops-user",
    )
    assert calls["method"] == "GET"
    assert calls["path"].endswith("/browser-profiles")
    assert result[0]["name"] == "P"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /work/surogate-ops && .venv/bin/python -m pytest tests/test_browser_profiles_proxy.py -v`
Expected: FAIL — module/function not found.

- [ ] **Step 3: Implement** — `surogate_ops/server/routes/browser_profiles.py`. Mirror `post_live_browser_control` (sessions.py) for the client build + forward, and `create_live_session` for the SA name/credential. Reuse the helpers by importing them from `routes.sessions`:

```python
"""Per-user proxy for harness browser-profile routes."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import get_current_subject  # match sessions.py import
from db.session import get_session              # match sessions.py import
from routes.sessions import (
    _build_live_agent_client,
    _ops_chat_service_account_name,
    _ops_chat_credential_name,
    _resolve_live_agent,
    _resolve_current_ops_user_id,
)

router = APIRouter()

_HARNESS_PREFIX = "/v1/api/browser-profiles"


async def _client(agent_id, request, ops_session, current_subject):
    _agent, org_id, _url = await _resolve_live_agent(agent_id, request, ops_session)
    user_id = await _resolve_current_ops_user_id(ops_session, current_subject)
    return await _build_live_agent_client(
        agent_id, request, ops_session,
        service_account_name=_ops_chat_service_account_name(org_id, user_id),
        service_account_credential_name=_ops_chat_credential_name(org_id, user_id),
    )


async def _forward_list(*, agent_id, request, ops_session, subject):
    client = await _client(agent_id, request, ops_session, subject)
    _status, payload = await client.request_json("GET", _HARNESS_PREFIX)
    return payload


@router.get("")
async def list_profiles(
    agent_id: str, request: Request,
    ops_session: AsyncSession = Depends(get_session),
    subject: str = Depends(get_current_subject),
):
    return await _forward_list(
        agent_id=agent_id, request=request, ops_session=ops_session, subject=subject,
    )


@router.post("", status_code=201)
async def create_profile(
    agent_id: str, body: dict[str, Any], request: Request,
    ops_session: AsyncSession = Depends(get_session),
    subject: str = Depends(get_current_subject),
):
    client = await _client(agent_id, request, ops_session, subject)
    _status, payload = await client.request_json("POST", _HARNESS_PREFIX, json_body=body)
    return payload


@router.patch("/{profile_id}")
async def rename_profile(
    profile_id: UUID, agent_id: str, body: dict[str, Any], request: Request,
    ops_session: AsyncSession = Depends(get_session),
    subject: str = Depends(get_current_subject),
):
    client = await _client(agent_id, request, ops_session, subject)
    _status, payload = await client.request_json(
        "PATCH", f"{_HARNESS_PREFIX}/{profile_id}", json_body=body,
    )
    return payload


@router.delete("/{profile_id}", status_code=204)
async def delete_profile(
    profile_id: UUID, agent_id: str, request: Request,
    ops_session: AsyncSession = Depends(get_session),
    subject: str = Depends(get_current_subject),
):
    client = await _client(agent_id, request, ops_session, subject)
    await client.request_json("DELETE", f"{_HARNESS_PREFIX}/{profile_id}")


@router.post("/{profile_id}/setup-session")
async def setup_session(
    profile_id: UUID, agent_id: str, body: dict[str, Any], request: Request,
    ops_session: AsyncSession = Depends(get_session),
    subject: str = Depends(get_current_subject),
):
    client = await _client(agent_id, request, ops_session, subject)
    _status, payload = await client.request_json(
        "POST", f"{_HARNESS_PREFIX}/{profile_id}/setup-session",
        json_body={**body, "owner_user_id": subject, "agent_id": agent_id},
    )
    return payload


@router.post("/{profile_id}/capture")
async def capture(
    profile_id: UUID, agent_id: str, session_id: str, body: dict[str, Any],
    request: Request,
    ops_session: AsyncSession = Depends(get_session),
    subject: str = Depends(get_current_subject),
):
    client = await _client(agent_id, request, ops_session, subject)
    _status, payload = await client.request_json(
        "POST", f"{_HARNESS_PREFIX}/{profile_id}/capture",
        params={"session_id": session_id},
        json_body={**body, "owner_user_id": subject},
    )
    return payload
```

Fix the imports (`get_current_subject`, `get_session`, and whether `_resolve_current_ops_user_id` exists) to match `routes/sessions.py` exactly — read its import block first.

- [ ] **Step 4: Run to verify it passes**

Run: `cd /work/surogate-ops && .venv/bin/python -m pytest tests/test_browser_profiles_proxy.py -v`
Expected: PASS.

- [ ] **Step 5: Register the router** — in `surogate_ops/server/routes/__init__.py` add `from routes.browser_profiles import router as browser_profiles_router`; in `surogate_ops/server/app.py` add (next to the sessions include):

```python
app.include_router(browser_profiles_router, prefix="/api/browser-profiles", tags=["browser-profiles"])
```

- [ ] **Step 6: Commit**

```bash
cd /work/surogate-ops
git switch -c feat/browser-profiles-ops
git add surogate_ops/server/routes/browser_profiles.py surogate_ops/server/routes/__init__.py surogate_ops/server/app.py tests/test_browser_profiles_proxy.py
git commit -m "feat(browser-profiles): add ops proxy routes"
```

---

## Task 10: Ops session-create accepts `browser_profile_id`

**Files:**
- Modify: `surogate_ops/server/routes/sessions.py` (`LiveSessionCreateRequest` + `create_live_session`)
- Test: `tests/test_browser_profiles_proxy.py` (extend)

**Interfaces:**
- Consumes: `create_live_session` config build (verbatim in Task-prep notes).
- Produces: when `body.browser_profile_id` is set, `session_config["browser"]["profile_id"]` is stamped before forwarding to the harness.

- [ ] **Step 1: Write the failing test** — extend `tests/test_browser_profiles_proxy.py`:

```python
def test_browser_profile_id_is_stamped_into_config():
    from surogate_ops.server.routes.sessions import _stamp_browser_profile
    cfg = _stamp_browser_profile({"ops": {"user_id": "u"}}, "prof-1")
    assert cfg["browser"]["profile_id"] == "prof-1"
    assert _stamp_browser_profile({"browser": {"x": 1}}, None) == {"browser": {"x": 1}}
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /work/surogate-ops && .venv/bin/python -m pytest tests/test_browser_profiles_proxy.py -k stamp -v`
Expected: FAIL — `_stamp_browser_profile` not defined.

- [ ] **Step 3: Implement** — in `surogate_ops/server/routes/sessions.py`, add a helper and use it. Add `browser_profile_id: str | None = None` to `LiveSessionCreateRequest`, then:

```python
def _stamp_browser_profile(config: dict, profile_id: str | None) -> dict:
    if not profile_id:
        return config
    browser = dict(config.get("browser") or {})
    browser["profile_id"] = profile_id
    return {**config, "browser": browser}
```

In `create_live_session`, wrap the `session_config` right before constructing the `SurogatesApiClient`:

```python
    session_config = _stamp_browser_profile(session_config, body.browser_profile_id)
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd /work/surogate-ops && .venv/bin/python -m pytest tests/test_browser_profiles_proxy.py -k stamp -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /work/surogate-ops
git add surogate_ops/server/routes/sessions.py tests/test_browser_profiles_proxy.py
git commit -m "feat(browser-profiles): stamp browser_profile_id into session config"
```

---

## Task 11: SDK adapter `listBrowserProfiles` + create wiring

**Files:**
- Modify: `sdk/agent-chat-react/src/types.ts` (`AgentChatBrowserProfile`, adapter method)
- Modify: `/work/surogate-ops/frontend/src/features/work/work-agent-chat-adapter.ts` (impl + create)
- Test: `sdk/agent-chat-react/tests/browser-profile-selector.test.tsx` (adapter typing covered indirectly in Task 12)

**Interfaces:**
- Produces:
  - `AgentChatBrowserProfile = { id: string; name: string; cookieDomains: string[]; hasState: boolean; createdAt: string; lastUsedAt: string | null }`.
  - `AgentChatAdapter.listBrowserProfiles?(): Promise<AgentChatBrowserProfile[]>`.
  - Session-create path carries an optional `browserProfileId`.

- [ ] **Step 1: Add the type + adapter method** — in `sdk/agent-chat-react/src/types.ts`:

```typescript
export interface AgentChatBrowserProfile {
  id: string;
  name: string;
  cookieDomains: string[];
  hasState: boolean;
  createdAt: string;
  lastUsedAt: string | null;
}
```

In the `AgentChatAdapter` interface, beside the other browser methods, add:

```typescript
  /** List the caller's saved browser profiles (optional capability). */
  listBrowserProfiles?(): Promise<AgentChatBrowserProfile[]>;
```

- [ ] **Step 2: Typecheck the SDK**

Run: `cd /work/surogates/sdk/agent-chat-react && npx tsc --noEmit`
Expected: PASS (no usages yet; pure type addition).

- [ ] **Step 3: Implement in the ops work adapter** — in `/work/surogate-ops/frontend/src/features/work/work-agent-chat-adapter.ts`, add (mirroring the existing `getBrowserState` fetch style, scoping by `agentId`):

```typescript
async listBrowserProfiles() {
  const response = await authFetch(
    `/api/browser-profiles?agent_id=${encodeURIComponent(agentId)}`,
  );
  if (!response.ok) throw new Error("Failed to list browser profiles");
  const data = (await response.json()) as Array<{
    id: string; name: string; cookie_domains: string[]; has_state: boolean;
    created_at: string; last_used_at: string | null;
  }>;
  return data.map((p) => ({
    id: p.id, name: p.name, cookieDomains: p.cookie_domains,
    hasState: p.has_state, createdAt: p.created_at, lastUsedAt: p.last_used_at,
  }));
},
```

- [ ] **Step 4: Typecheck the ops frontend**

Run: `cd /work/surogate-ops/frontend && npm run typecheck`
Expected: PASS.

- [ ] **Step 5: Commit (two repos)**

```bash
cd /work/surogates && git add sdk/agent-chat-react/src/types.ts && \
  git commit -m "feat(browser-profiles): add listBrowserProfiles adapter capability"
cd /work/surogate-ops && git add frontend/src/features/work/work-agent-chat-adapter.ts && \
  git commit -m "feat(browser-profiles): implement listBrowserProfiles in work adapter"
```

---

## Task 12: SDK chat-composer profile selector popover

**Files:**
- Modify: `sdk/agent-chat-react/src/components/chat/chat-composer.tsx`
- Test: `sdk/agent-chat-react/tests/browser-profile-selector.test.tsx`

**Interfaces:**
- Consumes: `adapter.listBrowserProfiles` (Task 11), `Popover`/`Command` (already imported in chat-composer), `useAgentChatAdapterContext`.
- Produces: new composer props `browserProfileId?: string | null`, `onSelectBrowserProfile?: (id: string | null) => void`; a `UserCircleIcon`/`IdCardIcon` `PromptInputButton` rendered next to the globe button when `canShowBrowser`.

- [ ] **Step 1: Write the failing test** — `sdk/agent-chat-react/tests/browser-profile-selector.test.tsx`. Follow `tests/browser-live-view.test.tsx` conventions (createRoot + act). Render `ChatComposer` inside an adapter provider whose `listBrowserProfiles` resolves to two profiles; open the popover; assert both names render and clicking one fires `onSelectBrowserProfile` with its id. Use the existing test harness/provider util the other composer tests use — find it with `grep -rln "AgentChatAdapterProvider\|renderComposer" tests/`.

```typescript
// Skeleton — adapt the provider wrapper to the existing composer tests:
it("lists profiles and selects one", async () => {
  const onSelect = vi.fn();
  const profiles = [
    { id: "p1", name: "Personal", cookieDomains: [], hasState: true,
      createdAt: "", lastUsedAt: null },
    { id: "p2", name: "Work", cookieDomains: [], hasState: false,
      createdAt: "", lastUsedAt: null },
  ];
  const node = await renderComposer({
    canShowBrowser: true,
    onSelectBrowserProfile: onSelect,
    adapter: { listBrowserProfiles: async () => profiles },
  });
  const trigger = node.querySelector('[aria-label="Select browser profile"]') as HTMLElement;
  await act(async () => trigger.click());
  const work = [...node.querySelectorAll('[role="option"]')]
    .find((el) => el.textContent?.includes("Work")) as HTMLElement;
  await act(async () => work.click());
  expect(onSelect).toHaveBeenCalledWith("p2");
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /work/surogates/sdk/agent-chat-react && npx vitest run tests/browser-profile-selector.test.tsx`
Expected: FAIL — no `Select browser profile` button.

- [ ] **Step 3: Implement** — add the props to `ChatComposerProps` and render the popover right after the browser-toggle `PromptInputButton` block (chat-composer.tsx ~line 798). Use `IdCardIcon` from lucide-react (add to the import) and a small local state + effect that loads profiles when the popover opens:

```typescript
// props (near showBrowser/onToggleBrowser):
browserProfileId?: string | null;
onSelectBrowserProfile?: (id: string | null) => void;

// inside the component body:
const [profileMenuOpen, setProfileMenuOpen] = useState(false);
const [profiles, setProfiles] = useState<AgentChatBrowserProfile[]>([]);
useEffect(() => {
  if (!profileMenuOpen || !adapter.listBrowserProfiles) return;
  let cancelled = false;
  void adapter.listBrowserProfiles()
    .then((p) => { if (!cancelled) setProfiles(p); })
    .catch(() => { if (!cancelled) setProfiles([]); });
  return () => { cancelled = true; };
}, [profileMenuOpen, adapter]);

// JSX, right after the globe PromptInputButton:
{canShowBrowser && onSelectBrowserProfile && adapter.listBrowserProfiles && (
  <Popover open={profileMenuOpen} onOpenChange={setProfileMenuOpen}>
    <PopoverTrigger asChild>
      <PromptInputButton
        aria-label="Select browser profile"
        aria-pressed={!!browserProfileId}
        tooltip="Browser profile"
        className={browserProfileId ? "bg-accent text-foreground" : undefined}
      >
        <IdCardIcon className="size-4" />
      </PromptInputButton>
    </PopoverTrigger>
    <PopoverContent side="top" align="start" className="w-64 overflow-hidden rounded-xl p-1">
      <Command>
        <CommandList>
          <CommandItem
            onSelect={() => { setProfileMenuOpen(false); onSelectBrowserProfile(null); }}
            className="gap-3 rounded-md px-3 py-2"
          >
            No profile
          </CommandItem>
          {profiles.map((p) => (
            <CommandItem
              key={p.id}
              onSelect={() => { setProfileMenuOpen(false); onSelectBrowserProfile(p.id); }}
              className="gap-3 rounded-md px-3 py-2"
            >
              {p.name}
              {browserProfileId === p.id && (
                <span className="ml-auto text-xs text-muted-foreground">Active</span>
              )}
            </CommandItem>
          ))}
        </CommandList>
      </Command>
    </PopoverContent>
  </Popover>
)}
```

Add `import type { AgentChatBrowserProfile } from "../../types";` and `IdCardIcon` to the lucide import.

- [ ] **Step 4: Run to verify it passes**

Run: `cd /work/surogates/sdk/agent-chat-react && npx vitest run tests/browser-profile-selector.test.tsx`
Expected: PASS.

- [ ] **Step 5: Typecheck + full composer tests**

Run: `cd /work/surogates/sdk/agent-chat-react && npx tsc --noEmit && npx vitest run tests/agent-chat.test.tsx`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /work/surogates
git add sdk/agent-chat-react/src/components/chat/chat-composer.tsx sdk/agent-chat-react/tests/browser-profile-selector.test.tsx
git commit -m "feat(browser-profiles): add profile selector to chat composer"
```

---

## Task 13: Studio `api/browser-profiles.ts` client

**Files:**
- Create: `/work/surogate-ops/frontend/src/api/browser-profiles.ts`

**Interfaces:**
- Consumes: the `request`/`authFetch` wrapper pattern from `frontend/src/api/sessions.ts`.
- Produces: `listBrowserProfiles(agentId)`, `createBrowserProfile(agentId, name)`, `renameBrowserProfile(agentId, id, name)`, `deleteBrowserProfile(agentId, id)`, `createSetupSession(agentId, id)`, `captureProfile(agentId, id, sessionId)`, and a `BrowserProfile` type.

- [ ] **Step 1: Implement** — `/work/surogate-ops/frontend/src/api/browser-profiles.ts` (reuse the same `authFetch`/error helper `sessions.ts` uses — import from the shared module it lives in):

```typescript
import { authFetch } from "./client"; // match the import sessions.ts uses

export interface BrowserProfile {
  id: string;
  name: string;
  source: string;
  cookieDomains: string[];
  hasState: boolean;
  createdAt: string;
  lastUsedAt: string | null;
}

interface RawProfile {
  id: string; name: string; source: string; cookie_domains: string[];
  has_state: boolean; created_at: string; last_used_at: string | null;
}

const toProfile = (p: RawProfile): BrowserProfile => ({
  id: p.id, name: p.name, source: p.source, cookieDomains: p.cookie_domains,
  hasState: p.has_state, createdAt: p.created_at, lastUsedAt: p.last_used_at,
});

async function json<T>(url: string, msg: string, init?: RequestInit): Promise<T> {
  const r = await authFetch(url, init);
  if (!r.ok) throw new Error(msg);
  return (await r.json()) as T;
}

const q = (agentId: string) => `agent_id=${encodeURIComponent(agentId)}`;

export async function listBrowserProfiles(agentId: string): Promise<BrowserProfile[]> {
  const raw = await json<RawProfile[]>(`/api/browser-profiles?${q(agentId)}`, "Failed to load profiles");
  return raw.map(toProfile);
}

export async function createBrowserProfile(agentId: string, name: string): Promise<BrowserProfile> {
  return toProfile(await json<RawProfile>(`/api/browser-profiles?${q(agentId)}`, "Failed to create profile", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  }));
}

export async function renameBrowserProfile(agentId: string, id: string, name: string): Promise<void> {
  await json(`/api/browser-profiles/${id}?${q(agentId)}`, "Failed to rename profile", {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
}

export async function deleteBrowserProfile(agentId: string, id: string): Promise<void> {
  const r = await authFetch(`/api/browser-profiles/${id}?${q(agentId)}`, { method: "DELETE" });
  if (!r.ok && r.status !== 204) throw new Error("Failed to delete profile");
}

export async function createSetupSession(agentId: string, id: string): Promise<{ sessionId: string; expiresAt: string }> {
  const raw = await json<{ session_id: string; expires_at: string }>(
    `/api/browser-profiles/${id}/setup-session?${q(agentId)}`, "Failed to start setup", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
  return { sessionId: raw.session_id, expiresAt: raw.expires_at };
}

export async function captureProfile(agentId: string, id: string, sessionId: string): Promise<BrowserProfile> {
  return toProfile(await json<RawProfile>(
    `/api/browser-profiles/${id}/capture?${q(agentId)}&session_id=${encodeURIComponent(sessionId)}`,
    "Failed to save authentication", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    }));
}
```

Fix the `authFetch` import path to match `sessions.ts` (`grep -n "authFetch" frontend/src/api/sessions.ts`).

- [ ] **Step 2: Typecheck**

Run: `cd /work/surogate-ops/frontend && npm run typecheck`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
cd /work/surogate-ops
git add frontend/src/api/browser-profiles.ts
git commit -m "feat(browser-profiles): add Studio browser-profiles api client"
```

---

## Task 14: Studio "Browser Profiles" manager section

**Files:**
- Create: `/work/surogate-ops/frontend/src/features/settings/browser-profiles-section.tsx`
- Modify: `/work/surogate-ops/frontend/src/features/settings/profile-tab.tsx` (mount it)

**Interfaces:**
- Consumes: Task 13 client; the `codeAgentId` already resolved in `profile-tab.tsx` (any agent in the active project — the runtime transport).
- Produces: `<BrowserProfilesSection agentId={string | null} />` rendering the list, create, rename, delete, and a "Set up authentication" button that opens the Task 15 dialog.

- [ ] **Step 1: Implement the section** — `/work/surogate-ops/frontend/src/features/settings/browser-profiles-section.tsx`:

```tsx
// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
import { useCallback, useEffect, useState } from "react";
import { Globe, Plus, Trash2, Play } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { RelativeTime } from "@/components/ui/relative-time";
import {
  type BrowserProfile,
  listBrowserProfiles,
  createBrowserProfile,
  deleteBrowserProfile,
} from "@/api/browser-profiles";
import { BrowserProfileSetupDialog } from "./browser-profile-setup-dialog";

export function BrowserProfilesSection({ agentId }: { agentId: string | null }) {
  const [profiles, setProfiles] = useState<BrowserProfile[]>([]);
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [removeId, setRemoveId] = useState<string | null>(null);
  const [setupId, setSetupId] = useState<string | null>(null);

  const refresh = useCallback(() => {
    if (!agentId) return;
    setLoading(true);
    listBrowserProfiles(agentId)
      .then(setProfiles)
      .catch(() => toast.error("Couldn't load browser profiles."))
      .finally(() => setLoading(false));
  }, [agentId]);

  useEffect(() => { refresh(); }, [refresh]);

  async function handleCreate() {
    if (!agentId) return;
    setCreating(true);
    try {
      await createBrowserProfile(agentId, "Personal Profile");
      refresh();
    } catch {
      toast.error("Couldn't create profile.");
    } finally {
      setCreating(false);
    }
  }

  async function handleDelete() {
    if (!agentId || !removeId) return;
    try {
      await deleteBrowserProfile(agentId, removeId);
      setRemoveId(null);
      refresh();
    } catch {
      toast.error("Couldn't delete profile.");
    }
  }

  if (!agentId) {
    return (
      <div className="mt-10 border-t border-line pt-8">
        <h2 className="font-display text-[15px] font-bold text-foreground mb-1.5">
          Browser Profiles
        </h2>
        <p className="text-[12px] text-muted-foreground">
          Select a project with an agent to manage browser profiles.
        </p>
      </div>
    );
  }

  return (
    <div className="mt-10 border-t border-line pt-8">
      <div className="flex items-center justify-between mb-1.5">
        <h2 className="font-display text-[15px] font-bold text-foreground">
          Browser Profiles
        </h2>
        <Button size="sm" onClick={handleCreate} disabled={creating}>
          <Plus className="size-4" /> Create Profile
        </Button>
      </div>
      <p className="text-[12px] text-muted-foreground mb-4">
        Preserve browser state and login sessions across tasks.
      </p>

      {loading && profiles.length === 0 ? (
        <div className="text-[13px] text-muted-foreground">Loading…</div>
      ) : profiles.length === 0 ? (
        <div className="text-[13px] text-muted-foreground">No profiles yet.</div>
      ) : (
        <div className="space-y-3">
          {profiles.map((p) => (
            <div key={p.id} className="bg-card border border-line rounded-xl px-5 py-4">
              <div className="flex items-center justify-between">
                <div className="font-display text-[14px] font-bold text-foreground">
                  {p.name}
                </div>
                <div className="flex items-center gap-2">
                  <Button size="sm" variant="outline" onClick={() => setSetupId(p.id)}>
                    <Play className="size-3.5" /> Set up authentication
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setRemoveId(p.id)}>
                    <Trash2 className="size-3.5 text-destructive" />
                  </Button>
                </div>
              </div>
              <div className="text-[11px] text-muted-foreground mt-2">
                Created <RelativeTime value={p.createdAt} />
                {p.lastUsedAt && (<> · Last used <RelativeTime value={p.lastUsedAt} /></>)}
              </div>
              {p.cookieDomains.length > 0 && (
                <div className="flex items-center gap-1.5 mt-2 text-[11px] text-muted-foreground">
                  <Globe className="size-3.5" />
                  Cookie Domains ({p.cookieDomains.length}): {p.cookieDomains.join(", ")}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      <ConfirmDialog
        open={!!removeId}
        title="Delete this profile?"
        description="Its saved authentication will be permanently removed."
        confirmLabel="Delete"
        onConfirm={handleDelete}
        onCancel={() => setRemoveId(null)}
      />

      {setupId && (
        <BrowserProfileSetupDialog
          agentId={agentId}
          profileId={setupId}
          open={!!setupId}
          onOpenChange={(o) => { if (!o) setSetupId(null); }}
          onSaved={() => { setSetupId(null); refresh(); }}
        />
      )}
    </div>
  );
}
```

- [ ] **Step 2: Mount it** — in `profile-tab.tsx`, import and render `<BrowserProfilesSection agentId={codeAgentId} />` just below the Coding Agents block (it already computes `codeAgentId`).

- [ ] **Step 3: Typecheck**

Run: `cd /work/surogate-ops/frontend && npm run typecheck`
Expected: PASS (the setup dialog import resolves once Task 15 lands — do Task 15 before typecheck, or stub the dialog import first).

- [ ] **Step 4: Commit** (with Task 15, since they typecheck together)

---

## Task 15: Studio "Set up authentication" dialog

**Files:**
- Create: `/work/surogate-ops/frontend/src/features/settings/browser-profile-setup-dialog.tsx`

**Interfaces:**
- Consumes: `createSetupSession`, `captureProfile` (Task 13); the work adapter / `BrowserPane` for the live view; `Dialog` from `@/components/ui/dialog`.
- Produces: `<BrowserProfileSetupDialog agentId profileId open onOpenChange onSaved />` — starts a setup session, renders the live `BrowserPane` with control, shows a TTL countdown, and a "Save authentication and close" button that calls `captureProfile`.

- [ ] **Step 1: Implement** — `/work/surogate-ops/frontend/src/features/settings/browser-profile-setup-dialog.tsx`:

```tsx
// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { createSetupSession, captureProfile } from "@/api/browser-profiles";
import { createWorkAgentChatAdapter } from "@/features/work/work-agent-chat-adapter";
import { BrowserPane } from "@invergent/agent-chat-react";

interface Props {
  agentId: string;
  profileId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
}

export function BrowserProfileSetupDialog(
  { agentId, profileId, open, onOpenChange, onSaved }: Props,
) {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [expiresAt, setExpiresAt] = useState<number | null>(null);
  const [remaining, setRemaining] = useState(0);
  const [saving, setSaving] = useState(false);
  const startedRef = useRef(false);

  // Start the setup session once per open.
  useEffect(() => {
    if (!open || startedRef.current) return;
    startedRef.current = true;
    createSetupSession(agentId, profileId)
      .then(({ sessionId, expiresAt }) => {
        setSessionId(sessionId);
        setExpiresAt(new Date(expiresAt).getTime());
      })
      .catch(() => {
        toast.error("Couldn't start the setup browser.");
        onOpenChange(false);
      });
    return () => { startedRef.current = false; };
  }, [open, agentId, profileId, onOpenChange]);

  // Countdown.
  useEffect(() => {
    if (!expiresAt) return;
    const tick = () => setRemaining(Math.max(0, Math.round((expiresAt - Date.now()) / 1000)));
    tick();
    const h = window.setInterval(tick, 1000);
    return () => window.clearInterval(h);
  }, [expiresAt]);

  useEffect(() => {
    if (expiresAt && remaining === 0) {
      toast.info("Setup session expired.");
      onOpenChange(false);
    }
  }, [remaining, expiresAt, onOpenChange]);

  const adapter = useMemo(() => createWorkAgentChatAdapter(agentId), [agentId]);

  async function handleSave() {
    if (!sessionId) return;
    setSaving(true);
    try {
      await captureProfile(agentId, profileId, sessionId);
      toast.success("Authentication saved.");
      onSaved();
    } catch {
      toast.error("Couldn't save authentication.");
    } finally {
      setSaving(false);
    }
  }

  const mmss = `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")}`;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-dvh w-screen max-w-none flex-col gap-0 rounded-none border-0 bg-background p-0">
        <DialogHeader className="h-12 shrink-0 flex-row items-center justify-between border-b border-line px-4">
          <DialogTitle className="text-sm">Set up browser authentication</DialogTitle>
          <div className="flex items-center gap-3">
            {expiresAt && (
              <span className="text-xs tabular-nums text-muted-foreground">{mmss}</span>
            )}
            <Button size="sm" onClick={handleSave} disabled={!sessionId || saving}>
              {saving ? "Saving…" : "Save authentication and close"}
            </Button>
          </div>
        </DialogHeader>
        <div className="min-h-0 flex-1 bg-black">
          {sessionId ? (
            <BrowserPane
              sessionId={sessionId}
              state={{ status: "user-control", controlOwner: null, liveViewPath: "" }}
              adapter={adapter}
              onClose={() => onOpenChange(false)}
            />
          ) : (
            <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
              Starting browser…
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
```

Verify the real exports: `BrowserPane` from `@invergent/agent-chat-react`, the work-adapter factory name (`grep -n "export" frontend/src/features/work/work-agent-chat-adapter.ts`), and `BrowserPane`'s required `state`/`adapter` prop shapes (it may auto-acquire control via the adapter's `acquireBrowserControl` — confirm against `browser-pane.tsx`). Adjust the `state` seed and props to match.

- [ ] **Step 2: Typecheck**

Run: `cd /work/surogate-ops/frontend && npm run typecheck`
Expected: PASS.

- [ ] **Step 3: Build the frontend to confirm the SDK bundles the new imports**

Run: `cd /work/surogate-ops/frontend && npm run build`
Expected: build succeeds.

- [ ] **Step 4: Commit**

```bash
cd /work/surogate-ops
git add frontend/src/features/settings/browser-profiles-section.tsx \
        frontend/src/features/settings/browser-profile-setup-dialog.tsx \
        frontend/src/features/settings/profile-tab.tsx
git commit -m "feat(browser-profiles): add Studio profile manager and setup dialog"
```

---

## Self-Review

**Spec coverage:**
- Data model (principal XOR, `source`, encrypted blob, cookie_domains) → Task 1.
- Encrypted store + CRUD scoping → Task 2.
- storage_state capture/inject mechanism → Tasks 3, 4, 5.
- Harness CRUD routes → Task 6.
- `browser_setup` standalone session (no wake, control granted, TTL) → Task 7.
- Control-guarded capture (only browser_setup, bound profile, lease held) → Task 8.
- Ops thin proxy (per-user SA, any-agent transport) → Task 9.
- `browser_profile_id` stamped on session create → Task 10.
- SDK selector + adapter → Tasks 11, 12.
- Studio manager + setup dialog → Tasks 13, 14, 15.
- Security (encrypted-at-rest, never-returned, cross-principal denial, capture-needs-lease) → Tasks 2, 6, 8.
- Deferred seams (`source`, `setup_spec.proxy` rejected) → Tasks 1, 7.

**Deviations from the spec, by design:** the spec's "Alembic migration in surogates/db/migrations" is replaced by a plain model addition because the harness applies schema via `Base.metadata.create_all` (`run_migrations`). No behavior change — the table still ships via `surogate-ops migrate upgrade`.

**Deploy note (not a task):** the harness changes ship in the runtime image (`runtime-api`/`runtime-worker`); ops changes ship in the ops image; SDK/Studio ship in the next web build. Run `surogate-ops migrate upgrade` once after the runtime image rolls to create `browser_profiles`.

**Open verification points flagged inline for the implementer** (confirm against real code before finalizing each task): exact import path of `get_current_tenant`; `BrowserPool.__init__` parameter names; `browser_resolver.resolve(...)` return attribute (`.endpoint.rest_url`); `create_agent_session` tolerance of a synthetic `agent_id`; `authFetch` import path; `BrowserPane` prop shapes and whether it self-acquires control.
