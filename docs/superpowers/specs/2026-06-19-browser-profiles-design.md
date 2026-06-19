# Browser profiles for persistent authentication — design

- **Date:** 2026-06-19
- **Status:** Approved (design) — amended after implementation-context review
- **Scope:** Per-user, reusable browser login state ("profiles") for managed-agent browsing
- **Repos:** `surogates` (harness owns the table, capture/inject, setup session, SDK selector) + `surogate-ops` (thin proxy API + Studio settings manager)

## Problem

Managed-agent browsers are **ephemeral**: every browser pod boots a fresh Chromium with
an empty, `emptyDir`-backed `user-data-dir` (`/chromium-data`). Nothing — cookies,
logins, localStorage — survives the pod. So an agent cannot act on sites that require a
login, and a user who logs in by hand (via the VNC "take control" live view) loses that
session the moment the browser closes.

We want **browser profiles**: a user saves a browser's login state once, under a named
profile, and reuses it across future agent tasks. Authentication is captured the only way
that survives Google's automation detection — a **human logging in over the CDP-free VNC
live view** — then exported and replayed into later sessions.

Nothing in this space exists today (the `zstd` in the browser image is an unused upstream
artifact). It is greenfield, but two durable primitives are already in place to build on:
S3/R2 `TenantStorage` and the Fernet-encrypted `CredentialVault`.

## Goals

- A user can **create, name, list, and delete** private browser profiles from Studio
  settings.
- A user can **set up authentication** for a profile: an interactive browser opens, they
  take control and log in by hand, then click **"Save authentication and close"** to
  capture the login state.
- A user can **attach a profile** to an agent's browser from the chat composer; the
  agent's browser then starts **already authenticated**.
- Captured state is **encrypted at rest** and never returned to any client.

## Non-goals (v1 — deferred, with seams left in)

- **Geo-proxy** (`Proxy: <Country>`). No egress-proxy infra exists; large separate
  workstream. Leave a `proxy` seam on the setup spec.
- **Import Local Profile.** Uploading/syncing a user's local Chrome profile. Leave a
  `source` notion on the profile.
- **Full Chromium `user-data-dir` sync.** v1 captures cookies + `storage_state` only
  (portable, small, yields the per-domain UI). Full-dir tar+zstd is a later robustness
  upgrade for stubborn sites.
- Agent tasks **writing back** to a profile. The setup flow is the only writer; tasks
  inject read-only.
- Org-shared / team profiles. v1 profiles are private to their owning user.

## Decision

- **Capture model: cookies + Playwright `storage_state`.** Exported via CDP/Playwright
  after the human login; injected into a fresh context before navigation on later tasks.
- **Ownership: the harness.** The `browser_profiles` table lives in the surogates DB
  next to `sessions`/`credentials`; capture/inject and the setup session run in the
  harness. `surogate-ops` is a thin authenticated proxy, exactly like `/api/sessions`
  and browser control already are.
- **Principal model: human user or per-user service account.** Work-chat requests reach
  the harness through `ops-chat-{org}-{ops_user_id}` service-account tokens, so the
  harness often has `tenant.user_id = null`. Profiles therefore use the same principal
  ownership pattern as scheduled sessions: exactly one of `user_id` or
  `service_account_id` is set. In Studio/Work, profiles are private to the caller's
  per-user service account.
- **Setup session: a real `Session` with `channel="browser_setup"`** that provisions a
  browser but does not start the agent harness, and grants the creating user control
  immediately. Reuses the entire existing live-view / RFB-input-gate / control-lease /
  ops-proxy / credit-metering stack — all keyed on `session_id`.
- **Binding: per browser session.** The selected `profile_id` is stamped into
  `session.config.browser.profile_id` and consumed when the browser is (re)provisioned.

## Architecture

```
Studio settings (ops frontend)            Chat composer (SDK)
  Browser Profiles manager                  profile selector popover
        │  /api/browser-profiles                 │  listBrowserProfiles()
        ▼                                         ▼  profile_id → session create
   surogate-ops  ── thin proxy (per-user service account) ──►  harness /v1/api/browser-profiles
                                                                   │
                              ┌────────────────────────────────────┼─────────────────────────┐
                              ▼                                    ▼                            ▼
                     browser_profiles table              setup session (channel=             capture/inject
                     (surogates DB)                       browser_setup) → VNC control        via CDP/Playwright
                     storage_state_enc (Fernet)                                                against the browser pod
```

## Data model

New table in the **surogates DB** (`surogates/db/models.py`), following the existing
principal-owned table pattern (UUID pk, `org_id` + user-or-service-account scoping,
server-default timestamps):

```
browser_profiles
  id                 uuid pk
  org_id             uuid  fk orgs            not null
  user_id            uuid  fk users           null        # direct harness user owner
  service_account_id uuid  fk service_accounts null       # ops-chat owner
  name               text                     not null    # "Personal Profile"
  source             text                     not null default 'manual_vnc'
  storage_state_enc  bytea                    null        # Fernet(storage_state JSON); null until first capture
  cookie_domains     jsonb                    not null default '[]'   # ["google.com", ...] for the UI
  created_at         timestamptz              not null default now()
  last_used_at       timestamptz              null
  check exactly one of (user_id, service_account_id) is non-null
  unique (org_id, user_id, name) where user_id is not null
  unique (org_id, service_account_id, name) where service_account_id is not null
```

- `storage_state_enc` holds the sensitive blob (cookies + per-origin localStorage),
  encrypted with the existing `CredentialVault` Fernet key. Never leaves the harness.
- `cookie_domains` is non-sensitive metadata, derived from the captured cookies, stored
  plaintext so the manager UI renders the domain list + favicons without decrypting.
- `source` is the v1 seam for future imported/local/full-dir profiles. v1 writes
  `manual_vnc` only.
- Alembic migration in `surogates/db/migrations` (and the embedded-migration path the
  `surogate-ops migrate` CLI runs against `surogates_database_url`).

## Capture & inject

- **Capture** (on "Save authentication and close"): the harness drives the live browser
  via the existing `KernelBrowserClient` Playwright-execute path —
  `context.storage_state()` → `{cookies, origins:[{origin, localStorage}]}`. Encrypt to
  `storage_state_enc`; derive `cookie_domains` from the cookie set; persist. Reading
  cookies via CDP *after* the human has logged in does not disturb the established
  session. This is the only permitted CDP call while the user-control lease is held:
  normal browser tools still reject with `paused_by_user`, but the setup-only capture
  route may call `KernelBrowserClient` directly after verifying the caller owns both the
  profile and the setup session's control lease.
- **Inject** (agent task): when `session.config.browser.profile_id` is set, the harness —
  immediately after the browser pool provisions/leases the pod and **before any
  navigation, registry publish, or `browser.provisioned` event** — decrypts the blob and
  applies it (`add_cookies` + seed `localStorage` per origin) into the fresh context.
  The agent then drives normally, already authenticated. Updates `last_used_at`.

## Component changes

### 1. Harness — `surogates/`

- **`db/models.py` + migration:** the `browser_profiles` table.
- **`browser/profiles.py` (new):** `BrowserProfileStore` — CRUD scoped to `(org_id,
  principal)` where principal is `user_id` or `service_account_id`; encrypt/decrypt via
  the vault; `capture_setup_session(session_id, profile_id, owner_user_id)` and
  `storage_state_for(profile_id, principal)`.
- **`api/routes/browser_profiles.py` (new):** router under `/v1/api/browser-profiles`:
  - `GET /` — list caller's profiles (metadata only).
  - `POST /` — create `{name?}` with `source="manual_vnc"`.
  - `DELETE /{id}` — delete profile + blob.
  - `POST /{id}/setup-session` — create the `browser_setup` session, return `session_id`
    (+ control granted to the caller). Body accepts `{setup_spec?: {proxy?: null}}`;
    v1 stores it on `session.config.browser.setup_spec` as the future proxy seam and
    rejects any non-null `proxy` until egress-proxy support exists.
  - `POST /{id}/capture?session_id=…` — export `storage_state` from that session's
    browser and save. Requires: profile principal matches caller; session
    `channel == "browser_setup"`; session config names the same `profile_id`; and the
    caller holds the control lease.
- **Session provisioning:** `channel="browser_setup"` creates a session that provisions a
  browser and grants control but does not run the agent loop. Implementation detail:
  create the `Session` row without adding an initial `user.message` or enqueueing a
  harness wake, then call `BrowserPool.ensure()` from the setup route and acquire the
  `BrowserControlStore` lease for the setup owner. A server-side TTL (~15 min)
  auto-closes and discards. Browser-pool `ensure()` reads `config.browser.profile_id`
  and injects `storage_state` before returning the endpoint.
- **`browser/client.py`:** add small public helpers on `KernelBrowserClient` for
  `storage_state()` and `apply_storage_state(state)` instead of reaching into its private
  `_playwright_execute()` from route code.
- **`tools/builtin/browser.py`:** inject-before-navigate happens at provision, so the
  tools are unchanged beyond getting an already-seeded context.

### 2. Ops — `surogate-ops/`

- **`surogate_ops/server/routes/browser_profiles.py` (new):** thin proxy under
  `/api/browser-profiles`
  — one passthrough per harness route, authenticated with the per-user ops-chat service
  account (mirrors `/api/sessions`). Resolves the user's org/SA the same way
  `create_live_session` does.
- **`surogate_ops/server/routes/sessions.py`:** `POST /api/sessions` accepts an optional
  `browser_profile_id` and stamps it into `config.browser.profile_id`.
- The existing live-view / control / preview / DELETE browser routes are reused
  unchanged for the setup session (they already key on `session_id`).

### 3. SDK — `sdk/agent-chat-react`

- **Adapter:** `listBrowserProfiles()`; thread the selected `profile_id` into session
  create. (Setup uses the existing browser-control adapter against the setup session.)
- **`chat-composer.tsx`:** a profile-selector `Popover` + `Command` button beside the
  globe/browser button in `PromptInputTools`. Lists the user's profiles + "No profile" +
  "Manage profiles…". The choice is a property of the browser session, applied when the
  browser (re)starts; changing it while a browser is live prompts a reload. Hidden when
  `canShowBrowser` is false.
- The setup live view reuses `BrowserPane` / `BrowserLiveView` (incl. the new zoom
  controls), with a "Save authentication and close" affordance + TTL countdown.

### 4. Studio — `surogate-ops/frontend`

- **`api/browser-profiles.ts` (new):** client for the ops routes.
- **`features/settings/profile-tab.tsx`:** a "Browser Profiles" section — count badge +
  "Create Profile"; per-profile card (name + inline rename, id + copy, created/last-used
  relative times, expandable **Cookie Domains (N)** with favicons, **Set up
  authentication**, **Delete** with confirm). The setup action opens the live setup
  session in a dialog. Built from existing shadcn primitives.

## Control flows

**Set up authentication**

1. User clicks **Set up authentication** (settings) or **Set up** (chat selector).
2. Ops `POST /api/browser-profiles/{id}/setup-session` → harness creates a
   `browser_setup` session owned by the caller's principal, provisions a browser, grants
   the user control using the ops user id as `owner_user_id`; returns `session_id`.
3. The live view opens (existing VNC transport) with control pre-granted and a ~15-min
   countdown.
4. User logs in by hand (CDP-free input over RFB).
5. **Save authentication and close** → ops `POST /api/browser-profiles/{id}/capture?session_id=…`
   → harness exports `storage_state`, saves to the profile, derives `cookie_domains`,
   then tears down the browser + session.
6. On TTL expiry without a save, the session auto-closes and nothing is persisted.

**Agent task using a profile**

1. User picks a profile in the chat selector → `profile_id` held for the next browser
   session.
2. Session create stamps `config.browser.profile_id`.
3. Agent's first browser tool call provisions a browser; the pool injects the profile's
   `storage_state` before navigation; `last_used_at` updated.
4. Agent browses already authenticated.

## Security & edge cases

- `storage_state_enc` is encrypted at rest and **never** returned to any client; it flows
  only harness → browser pod.
- Every route re-scopes to `(org_id, principal)` where principal is either the harness
  `user_id` or the caller's per-user `service_account_id`; a profile is unreadable
  cross-user even with a guessed id.
- Capture requires the caller to **hold the control lease** on the setup session — no
  exporting another user's live browser.
- Capture is restricted to `browser_setup` sessions bound to the target profile. It
  cannot export arbitrary normal agent sessions, even if a user temporarily holds their
  control lease.
- Setup-session TTL auto-discards on expiry; "Save" is the only persist path.
- Deleting a profile does not touch already-injected running sessions; it only prevents
  future attachment.
- Empty profile (created, never set up) injects nothing — the browser is simply fresh.

## Testing

- **Harness:** profile CRUD `(org_id, principal)` scoping for both direct users and
  service accounts; setup-session creation does not enqueue a harness wake;
  capture-requires-control and rejects non-`browser_setup` sessions; capture route is the
  only allowed CDP exception while control is held; inject-before-registry/event/navigate
  ordering; vault encrypt/decrypt round-trip; `cookie_domains` derivation; setup-session
  TTL discard.
- **Ops:** proxy auth through the caller's per-user service account; profile manager
  responses isolated per ops user; `browser_profile_id` stamped into session config on
  create.
- **SDK:** selector render/select + adapter wiring; profile flows into session create.
- **Studio:** manager render, create/rename/delete, setup-dialog open.
- TDD throughout — failing test first.

## Out of scope (deferred workstreams)

- Geo-proxy (`Proxy: <Country>`): egress-proxy pool + per-pod `--proxy-server` + region
  UI. Seam: `proxy` field on the setup spec.
- Import Local Profile: local-profile export + large upload. Seam: profile `source`.
- Full `user-data-dir` tar+zstd sync (robustness upgrade over `storage_state`).
- Agent task write-back / cookie refresh.
- Org-shared / team profiles.

## Open questions / risks

- **Stubborn sites** that bind sessions to IndexedDB / service workers may not fully
  restore from `storage_state` alone — accepted v1 limitation; full-dir sync is the
  escape hatch.
- **Setup session credit metering:** a `browser_setup` session consumes browser minutes
  like any live browser; confirm the reserve/relief path treats it the same as a normal
  browser session (it runs through the same pool).
- **Fleet vs. native backend:** injection happens at the harness pool layer, so it works
  regardless of which backend (fleet warm-pool / K8s / process) leases the pod.
