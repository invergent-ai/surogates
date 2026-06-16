# Website Channel Per-Agent Routing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the website widget per-agent by resolving the agent + allowed origin from the `channel_routing` table (keyed by publishable key), exactly like Slack/Telegram — instead of the global `settings.website.*` gates.

**Architecture:** Three coordinated changes against one contract. (A) `surogates` `website.py` resolves `channel_routing(website:<key>)` → `(agent_id, org_id, api_web_url)`, validates Origin against `api_web_url`. (B) `surogate-ops` upserts the routing row when the Website channel is saved in Studio. (C) the widget SDK gains a key-only mount that calls the live pairing endpoint first.

**Tech Stack:** Python/FastAPI (surogates + surogate-ops), pytest + testcontainers, React/TS (Studio), TypeScript/tsup/vitest (widget SDK).

**Spec:** `surogates/docs/superpowers/specs/2026-06-16-website-channel-per-agent-routing-design.md`

**Repos & commit boundaries:** Group A commits in `surogates/`, Group B in `surogate-ops/`, Group C in `surogates/sdk/website-widget/`. All three are on `master` — branch first in each repo before committing.

---

## File Structure

- `surogates/surogates/api/routes/website.py` — replace global gates with routing resolution (Group A).
- `surogates/tests/api/test_website_routing.py` — new route tests (Group A).
- `surogate-ops/surogate_ops/server/services/channel_routing.py` — new `upsert_website_routing` service (Group B).
- `surogate-ops/surogate_ops/server/routes/admin_channels.py` — refactor create to call the shared service (Group B).
- `surogate-ops/surogate_ops/server/routes/channels.py` — add `PUT /channels/website/{agent_id}` save endpoint (Group B).
- `surogate-ops/tests/test_website_routing_upsert.py` — service tests (Group B).
- `surogate-ops/frontend/src/features/agents/...` — Studio save calls the new endpoint (Group B).
- `surogates/sdk/website-widget/src/{constants,protocol,agent}.ts` + `ui/mount.tsx` — pairing (Group C).
- `surogates/sdk/website-widget/test/pairing.test.ts` — SDK test (Group C).

---

## Group A — surogates `website.py` route (the core)

### Task A1: Resolve agent + origin from channel_routing on bootstrap

**Files:**
- Modify: `surogates/surogates/api/routes/website.py`
- Test: `surogates/tests/api/test_website_routing.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# surogates/tests/api/test_website_routing.py
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from surogates.api.routes import website


class _FakeCache:
    def __init__(self, rows): self._rows = rows
    async def get(self, key): return self._rows.get(key)


def _app(rows):
    app = FastAPI()
    app.include_router(website.router, prefix="/v1")
    app.state.channel_routing_cache = _FakeCache(rows)
    return app


@pytest.mark.asyncio
async def test_bootstrap_unknown_key_returns_404():
    app = _app(rows={})
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://x") as c:
        r = await c.post(
            "/v1/website/sessions",
            headers={"Authorization": "Bearer surg_wk_nope", "Origin": "https://acme.com"},
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_bootstrap_origin_mismatch_returns_403():
    rows = {"website:surg_wk_ok": {"agent_id": "a1", "org_id": "o1", "api_web_url": "https://acme.com"}}
    app = _app(rows)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://x") as c:
        r = await c.post(
            "/v1/website/sessions",
            headers={"Authorization": "Bearer surg_wk_ok", "Origin": "https://evil.com"},
        )
    assert r.status_code == 403
```

- [ ] **Step 2: Run it, verify it fails**

Run: `cd surogates && SUROGATES_CONFIG=config.yaml.example uv run pytest tests/api/test_website_routing.py -x -q`
Expected: FAIL (route still requires global settings / 404 wiring differs).

- [ ] **Step 3: Add the routing-resolution helper**

Add to `website.py` (mirrors `channels/slack.py::resolve`, replacing `_require_website_enabled` + `_verify_publishable_key_from_request` for bootstrap):

```python
async def _resolve_website_routing(request: Request, key: str) -> dict:
    """Resolve (agent_id, org_id, api_web_url) from channel_routing(website:<key>).

    The publishable key is both identifier and auth. No active row → 404
    (replaces the global enabled + key checks). Mirrors the Slack/Telegram
    adapter resolution.
    """
    cache = getattr(request.app.state, "channel_routing_cache", None)
    routing = await cache.get(f"website:{key}") if cache is not None else None
    if not routing:
        raise HTTPException(status_code=404, detail="Unknown or inactive website key.")
    return routing
```

- [ ] **Step 4: Rewrite the bootstrap gate to use it**

In `bootstrap_website_session`, replace the global checks (`_require_website_enabled`, `_verify_publishable_key_from_request`, and the `settings.website.allowed_origins` origin check) with:

```python
    token = _extract_bearer(request)
    if not token or not is_publishable_key(token):
        raise HTTPException(
            status_code=401,
            detail=f"Website bootstrap requires a publishable key (prefix {PUBLISHABLE_KEY_PREFIX!r}).",
            headers={"WWW-Authenticate": "Bearer"},
        )
    routing = await _resolve_website_routing(request, token)
    request_origin = _extract_origin(request)
    if normalize_origin(request_origin) != normalize_origin(routing["api_web_url"] or ""):
        raise HTTPException(status_code=403, detail="Request origin is not the agent's configured site.")
    org_id = routing["org_id"]
    agent_id = routing["agent_id"]
```

Then use `org_id` / `agent_id` from `routing` for the rest of the handler (session create, cookie), replacing the prior `agent_runtime.org_id` / `agent_runtime.agent_id` reads. Keep the existing `org_id` UUID-parse + storage-bucket guards.

- [ ] **Step 5: Apply the same routing resolution to the cookie routes' origin check**

In `_load_and_authorize_session`, replace `parse_allowed_origins(settings.website.allowed_origins)` with the per-session origin already bound in the cookie claims (the cookie's `origin` claim is authoritative; keep `_enforce_origin_binding` against `(claims.origin, request_origin)` and drop the allow-list arg). Update `_enforce_origin_binding` signature to take only `(claims, request_origin)`.

- [ ] **Step 6: Run the tests, verify pass**

Run: `cd surogates && SUROGATES_CONFIG=config.yaml.example uv run pytest tests/api/test_website_routing.py -q`
Expected: PASS (both tests).

- [ ] **Step 7: Run the existing website suite to catch regressions**

Run: `cd surogates && SUROGATES_CONFIG=config.yaml.example uv run pytest tests/ -k website -q`
Expected: PASS, or update tests that asserted the old global-settings behavior (those assertions are now obsolete per the spec).

- [ ] **Step 8: Commit**

```bash
cd surogates && git checkout -b feat/website-per-agent-routing
git add surogates/api/routes/website.py tests/api/test_website_routing.py
git commit -m "feat(website): resolve agent + origin via channel_routing"
```

---

## Group B — surogate-ops: create the routing row on save

### Task B1: `upsert_website_routing` service (TDD)

**Files:**
- Create: `surogate-ops/surogate_ops/server/services/channel_routing.py`
- Test: `surogate-ops/tests/test_website_routing_upsert.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# surogate-ops/tests/test_website_routing_upsert.py
import pytest
from surogate_ops.server.services.channel_routing import upsert_website_routing
from surogate_ops.core.db.models.operate import ChannelKind, ChannelRouting
import sqlalchemy as sa


@pytest.mark.asyncio
async def test_upsert_creates_then_updates(db_session, seed_agent):
    # seed_agent → (agent_id, project_id)
    await upsert_website_routing(
        db_session, agent_id=seed_agent.id, project_id=seed_agent.project_id,
        publishable_key="surg_wk_k1", api_web_url="https://acme.com",
    )
    await db_session.commit()
    rows = (await db_session.execute(
        sa.select(ChannelRouting).where(ChannelRouting.agent_id == seed_agent.id)
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].channel_kind == ChannelKind.website
    assert rows[0].channel_identifier == "surg_wk_k1"
    assert rows[0].api_web_url == "https://acme.com"

    # rotate key + change origin → still one row
    await upsert_website_routing(
        db_session, agent_id=seed_agent.id, project_id=seed_agent.project_id,
        publishable_key="surg_wk_k2", api_web_url="https://app.acme.com",
    )
    await db_session.commit()
    rows = (await db_session.execute(
        sa.select(ChannelRouting).where(ChannelRouting.agent_id == seed_agent.id)
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].channel_identifier == "surg_wk_k2"
    assert rows[0].api_web_url == "https://app.acme.com"
```

- [ ] **Step 2: Run it, verify it fails**

Run: `cd surogate-ops && .venv/bin/python -m pytest tests/test_website_routing_upsert.py -x --tb=short`
Expected: FAIL with ImportError (`upsert_website_routing` not defined). If `seed_agent`/`db_session` fixtures are absent, add minimal ones in the test using the existing `tests/conftest.py` DB pattern.

- [ ] **Step 3: Implement the service**

```python
# surogate_ops/server/services/channel_routing.py
"""Upsert helpers for ChannelRouting rows (website kind)."""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from surogate_ops.core.db.models.operate import ChannelKind, ChannelRouting


async def upsert_website_routing(
    session: AsyncSession, *, agent_id: str, project_id: str,
    publishable_key: str, api_web_url: str,
) -> None:
    """Create or update the single website routing row for an agent.

    One website row per agent: identifier = publishable key, api_web_url =
    the embedding site. Rotating the key or changing the site updates the
    same row. Caller commits.
    """
    existing = (await session.execute(
        sa.select(ChannelRouting)
        .where(ChannelRouting.channel_kind == ChannelKind.website)
        .where(ChannelRouting.agent_id == agent_id)
    )).scalar_one_or_none()
    if existing is None:
        session.add(ChannelRouting(
            channel_kind=ChannelKind.website,
            channel_identifier=publishable_key,
            agent_id=agent_id, org_id=project_id,
            api_web_url=api_web_url, active=True,
        ))
    else:
        existing.channel_identifier = publishable_key
        existing.api_web_url = api_web_url
        existing.active = True
```

- [ ] **Step 4: Run the test, verify pass**

Run: `cd surogate-ops && .venv/bin/python -m pytest tests/test_website_routing_upsert.py -q`
Expected: PASS.

- [ ] **Step 5: Refactor `admin_channels.py` create to reuse the service (DRY)**

In `admin_channels.py`, where `kind == website`, call `upsert_website_routing` instead of inlining the `ChannelRouting(...)` build, so there is one code path. Leave slack/telegram create as-is.

- [ ] **Step 6: Commit**

```bash
cd surogate-ops && git checkout -b feat/website-per-agent-routing
git add surogate_ops/server/services/channel_routing.py surogate_ops/server/routes/admin_channels.py tests/test_website_routing_upsert.py
git commit -m "feat(channels): upsert_website_routing service"
```

### Task B2: Save endpoint + Studio wiring

**Files:**
- Modify: `surogate-ops/surogate_ops/server/routes/channels.py` (add `PUT /channels/website/{agent_id}`)
- Modify: `surogate-ops/frontend/src/features/agents/channels-tab.tsx` + `api/agents.ts`

- [ ] **Step 1: Write the failing route test**

```python
# add to surogate-ops/tests/test_website_routing_upsert.py
@pytest.mark.asyncio
async def test_put_website_channel_endpoint(client, seed_agent):
    r = await client.put(
        f"/api/channels/website/{seed_agent.id}",
        json={"publishable_key": "surg_wk_kE", "api_web_url": "https://acme.com"},
    )
    assert r.status_code == 204
```

- [ ] **Step 2: Run it, verify it fails (404 route missing)**

Run: `cd surogate-ops && .venv/bin/python -m pytest tests/test_website_routing_upsert.py::test_put_website_channel_endpoint -x --tb=short`
Expected: FAIL (404).

- [ ] **Step 3: Add the endpoint**

```python
# in channels.py
from pydantic import BaseModel
from surogate_ops.server.services.channel_routing import upsert_website_routing

class WebsiteChannelSave(BaseModel):
    publishable_key: str
    api_web_url: str

@router.put("/website/{agent_id}", status_code=204)
async def save_website_channel(
    agent_id: str, body: WebsiteChannelSave,
    current_subject: str = Depends(get_current_subject),
    session: AsyncSession = Depends(get_session),
):
    agent = (await session.execute(
        sa.select(Agent).where(Agent.id == agent_id)
    )).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "agent not found")
    await upsert_website_routing(
        session, agent_id=agent_id, project_id=agent.project_id,
        publishable_key=body.publishable_key, api_web_url=body.api_web_url,
    )
    await session.commit()
```

- [ ] **Step 4: Run test, verify pass**

Run: `cd surogate-ops && .venv/bin/python -m pytest tests/test_website_routing_upsert.py -q`
Expected: PASS.

- [ ] **Step 5: Wire Studio save (frontend)**

In `api/agents.ts` add `saveWebsiteChannel(agentId, { publishable_key, api_web_url })` → `PUT /api/channels/website/{agentId}`. In `channels-tab.tsx::handleSave`, after the existing `updateAgent` (which keeps appearance env-vars), call `saveWebsiteChannel(agent.id, { publishable_key: website.publishableKey, api_web_url: website.origins[0] })` when `website.enabled`. Drop `ENABLED/PUBLISHABLE_KEY/ALLOWED_ORIGINS` from the env fragment in `use-website-channel-state.ts::prepareSave` (keep appearance keys).

- [ ] **Step 6: Typecheck + commit**

Run: `cd surogate-ops/frontend && npm run typecheck`
Expected: clean.

```bash
cd surogate-ops && git add surogate_ops/server/routes/channels.py frontend/src/features/agents/ tests/test_website_routing_upsert.py
git commit -m "feat(channels): PUT website channel save + Studio wiring"
```

---

## Group C — widget SDK: key-only mount via pairing

### Task C1: Pairing then bootstrap

**Files:**
- Modify: `surogates/sdk/website-widget/src/constants.ts`, `src/protocol.ts`, `src/agent.ts`, `src/ui/types.ts`
- Test: `surogates/sdk/website-widget/test/pairing.test.ts` (create)

- [ ] **Step 1: Write the failing test**

```ts
// surogates/sdk/website-widget/test/pairing.test.ts
import { describe, it, expect, vi } from 'vitest';
import { resolvePairing } from '../src/protocol';

describe('resolvePairing', () => {
  it('resolves agent_id + api_web_url from the pairing endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, json: async () => ({ agent_id: 'a1', api_web_url: 'https://acme.com' }),
    });
    const out = await resolvePairing('https://api.surogate.ai', 'surg_wk_k', fetchMock as any);
    expect(out).toEqual({ agentId: 'a1', apiWebUrl: 'https://acme.com' });
    expect(fetchMock).toHaveBeenCalledWith(
      'https://api.surogate.ai/api/widget/p/surg_wk_k', expect.any(Object),
    );
  });
});
```

- [ ] **Step 2: Run it, verify it fails**

Run: `cd surogates/sdk/website-widget && pnpm test -- pairing`
Expected: FAIL (`resolvePairing` not exported).

- [ ] **Step 3: Implement `resolvePairing` + constants**

In `constants.ts`: `export const PATH_PAIRING = (key: string) => '/api/widget/p/' + key;` and `export const DEFAULT_PAIRING_BASE = 'https://api.surogate.ai';`.

In `protocol.ts`:

```ts
export async function resolvePairing(
  pairingBase: string, publishableKey: string, doFetch = fetch,
): Promise<{ agentId: string; apiWebUrl: string }> {
  const url = pairingBase.replace(/\/+$/, '') + PATH_PAIRING(publishableKey);
  const res = await doFetch(url, { method: 'GET' });
  if (!res.ok) throw new SurogatesAuthError('Unknown widget key');
  const body = await res.json();
  return { agentId: body.agent_id, apiWebUrl: body.api_web_url };
}
```

- [ ] **Step 4: Run test, verify pass**

Run: `cd surogates/sdk/website-widget && pnpm test -- pairing`
Expected: PASS.

- [ ] **Step 5: Use pairing in mount when apiUrl absent**

In `ui/types.ts` make `apiUrl` optional and add `pairingUrl?: string`. In `ui/mount.tsx` (and/or `agent.ts` construction): if `config.apiUrl` is missing, call `resolvePairing(config.pairingUrl ?? DEFAULT_PAIRING_BASE, config.publishableKey)` and use the returned `apiWebUrl` as the agent `apiUrl`. If `apiUrl` is present, skip pairing (back-compat).

- [ ] **Step 6: Typecheck + build + commit**

Run: `cd surogates/sdk/website-widget && pnpm typecheck && pnpm build`
Expected: clean; `dist/surogates-widget.global.js` rebuilt.

```bash
cd surogates && git add sdk/website-widget/src sdk/website-widget/test
git commit -m "feat(widget): key-only mount via pairing endpoint"
```

---

## Final verification

- [ ] **Run all three suites:**
  - `cd surogates && SUROGATES_CONFIG=config.yaml.example uv run pytest tests/ -k website -q`
  - `cd surogate-ops && .venv/bin/python -m pytest tests/test_website_routing_upsert.py -q`
  - `cd surogates/sdk/website-widget && pnpm test`
- [ ] **Note:** live end-to-end is gated on a deployment that serves the website channel (densemax 404s all API paths today). Do not fake a green bootstrap; the unit suites are the acceptance bar.

---

## Self-review notes

- **Spec coverage:** Gap 1 (route) → Task A1. Gap 2 (Studio save row) → Tasks B1+B2. Gap 3 (SDK pairing) → Task C1. Security model (key via routing + origin) → A1 steps 3-4. Testing section → per-task + Final verification.
- **Type consistency:** `upsert_website_routing(session, *, agent_id, project_id, publishable_key, api_web_url)` used identically in B1/B2; `resolvePairing` returns `{agentId, apiWebUrl}` in C1 test + impl; routing dict shape `{agent_id, org_id, api_web_url}` consistent A1 ↔ ops `ChannelRoutingResponse`.
- **Known confirm-on-execute:** the exact channel_routing cache dict keys (`agent_id`/`org_id`/`api_web_url`) match ops `ChannelRoutingResponse`; if the loader renames any, align A1 step 3.
