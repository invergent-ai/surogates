# 5. Multi-Tenancy

Surogates is multi-tenant from the ground up. Every request is scoped to an organization (org) and a user. Tenant isolation is enforced at every layer: database queries, storage access, API routing, and policy enforcement.

## Tenant Model

```
Org (organization)
 |
 +-- Users
 |    +-- Channel Identities (Slack user, Telegram user, web login)
 |    +-- Sessions
 |    +-- Skills (user-scoped)
 |    +-- Memory (user-scoped)
 |
 +-- Service Accounts (API-channel API keys, no user identity)
 |    +-- Sessions (channel="api")
 |
 +-- Skills (org-wide)
 +-- Memory (org-wide; also used by API-channel sessions)
 +-- MCP Servers
 +-- Credentials (API keys, tokens)
 +-- Config (auth provider, model defaults, limits)
```

### Organizations

An org is the top-level tenant boundary. Each org has:
- Its own Garage bucket (`tenant-{org_id}`) for skills, memory, and MCP configs.
- Its own auth provider configuration.
- Its own credential vault (encrypted API keys and tokens).
- Configurable model defaults, tool access, and rate limits.

### Users

Users belong to exactly one org. A user can have multiple channel identities (e.g., a Slack account and a web login) that all map to the same internal user record, sharing sessions and memory.

### Channel Identities

A channel identity links a platform-specific user ID to an internal Surogates user. Each identity records the platform name (e.g., `slack`, `web`), the platform-specific user ID, and optional platform metadata.

A given platform + user ID combination is unique -- each external identity maps to exactly one internal user.

When a Slack message arrives, the adapter looks up the platform user ID to find the internal user. The same user, same sessions, same memory -- regardless of which channel they use.

## Authentication

Authentication uses the database provider: users are stored in the database with bcrypt-hashed passwords. The platform issues its own JWTs after verifying credentials.

Login: `POST /v1/auth/login {"email": "...", "password": "..."}`

## JWT Token Flow

Surogates issues its own JWTs regardless of the upstream auth provider. The flow:

```
1. Client sends credentials -> POST /v1/auth/login
2. API server resolves the org's AuthProvider from orgs.config
3. AuthProvider.authenticate(credentials) -> AuthResult
4. If authenticated: upsert user in users table
5. Issue Surogates JWT: {org_id, user_id, permissions, exp}
6. Return access_token + refresh_token
7. Client sends JWT with every subsequent request
```

**Access tokens** are short-lived (default 30 minutes, HS256).
**Refresh tokens** are long-lived (default 24 hours).
**Sandbox tokens** are session-scoped (1 hour, used for S3 access only).
**Service-account session tokens** are session-scoped JWTs minted by the worker for API-channel sessions (default 1 year); carry `service_account_id` + `session_id`, no user identity.

Each token carries the org ID, permissions, token type, and expiry timestamp.

## Service Accounts (Programmatic Access)

Interactive users sign in with a JWT. Non-interactive clients -- synthetic-data pipelines, batch jobs, scheduled workloads -- authenticate with an **org-scoped service-account token** instead. Tokens have the prefix `surg_sk_` and are issued by an admin:

```
POST /v1/admin/service-accounts
Authorization: Bearer <admin-jwt>

{"org_id": "00000000-...", "name": "dataset-gen-v1"}
```

The raw token is returned **once** in the response; only a SHA-256 hash is persisted, so the plaintext is not recoverable. Tokens are long-lived until revoked via `DELETE /v1/admin/service-accounts/{id}`.

**Path restriction.** Service-account tokens may only authenticate requests under `/v1/api/*`. Presenting one anywhere else yields 403. Conversely, the `/v1/api/*` routes reject interactive JWTs, so the two principal types stay cleanly separated.

**Revocation.** Revoking an SA invalidates every outstanding token. The auth layer caches resolutions for 60 seconds, so the process that performed the revoke applies it immediately while peer processes converge within the cache TTL.

**No permissions.** SA tokens carry no `admin`/`sessions:*` permissions; access is scoped entirely by org membership and the `/v1/api/*` path prefix.

See [Channels / API](../channels/api.md) for the submission endpoints and [Appendix B](../appendices/api-reference.md) for the admin CRUD.

## Tenant Context

Every request carries a `TenantContext` that flows through the entire async call chain via Python's context variables. It contains the org ID, user ID, org-level config, user preferences, permissions, and the tenant's asset root path.

The auth middleware extracts the JWT, builds the `TenantContext`, and binds it automatically. Any code downstream can access the current tenant without explicit parameter passing.

## Credential Vault

Sensitive values (API keys, OAuth secrets, LLM provider tokens) are stored encrypted at rest in the credential vault.

- **Org-wide credentials**: Shared across all users in the org (e.g., a shared OpenAI API key).
- **User-specific credentials**: Scoped to a single user (e.g., a personal GitHub token).
- **Encryption**: Fernet (symmetric, AES-128-CBC + HMAC-SHA256).
- **Referencing**: Credentials are referenced by name in configuration (e.g., `client_secret_ref: "okta_client_secret"`).

Credentials are never exposed to sandboxes. The MCP proxy fetches credentials from the vault and injects them into outbound requests.

## Agent Identity (`SOUL.md`)

Each tenant can define the agent's persona, voice, and identity via a `SOUL.md` file in the tenant's asset bucket. The harness loads it at session start and injects it into the system prompt under the heading `## Agent Identity (SOUL.md)`.

**Location** (first match wins):

```
tenant-{org_id}/
  shared/SOUL.md         # preferred
  SOUL.md                # fallback
```

Loaded by `surogates.harness.context_files.load_soul_md` and assembled into the prompt by `PromptBuilder._build_context_files`.

**Scope.** Org-wide only. There is no per-user `SOUL.md`; per-user personalization belongs in `users/{user_id}/memory/USER.md`. `SOUL.md` defines *who the agent is for this org*; `USER.md` records *what the agent knows about a specific user*.

**Safety.** Content passes through `scan_context_content` (12 prompt-injection patterns -- jailbreak phrases, hidden-instruction markers, role-override attempts, invisible unicode) and is truncated by `truncate_context` before injection. Detected injections are stripped, not silently passed through.

**Example.**

```markdown
# Acme Support Agent

You are the Acme Corp customer support assistant. You speak in the first
person plural ("we") when referring to Acme. You never disclose internal
ticket IDs to users. When a request is outside support scope, you offer
to escalate to a human agent rather than guessing.

Tone: warm, concise, technically literate. Avoid corporate boilerplate.
```

`SOUL.md` is optional -- if absent, no identity block is added to the prompt.

## Channel Identity Mapping

```
Web user authenticates -> AuthProvider verifies -> JWT contains org_id + user_id -> direct

Slack message arrives -> platform_user_id = "U03ABCDEF"
  -> lookup channel_identities WHERE platform='slack' AND platform_user_id='U03ABCDEF'
  -> resolve to internal user_id
  -> same TenantContext, same memory, same sessions
```

Users can link their channel identities via the web UI or via a pairing flow (e.g., the Slack adapter sends a pairing link). Once linked, all channels share the same user context.
