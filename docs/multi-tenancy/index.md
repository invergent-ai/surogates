# 5. Multi-Tenancy

Surogates is multi-tenant from the ground up. Every request is scoped to an organization (org) and a user. Tenant isolation is enforced at every layer: database queries, storage access, API routing, and policy enforcement.

## Tenant Model

```
Org (organization)
 |
 +-- Users
 |    +-- Channel Identities (Slack user, web login)
 |    +-- Sessions
 |    +-- Skills (user-scoped)
 |    +-- Memory (user-scoped)
 |
 +-- Skills (org-wide)
 +-- Memory (org-wide)
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

Each token carries the org ID, user ID, permissions, token type, and expiry timestamp.

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

## Channel Identity Mapping

```
Web user authenticates -> AuthProvider verifies -> JWT contains org_id + user_id -> direct

Slack message arrives -> platform_user_id = "U03ABCDEF"
  -> lookup channel_identities WHERE platform='slack' AND platform_user_id='U03ABCDEF'
  -> resolve to internal user_id
  -> same TenantContext, same memory, same sessions
```

Users can link their channel identities via the web UI or via a pairing flow (e.g., the Slack adapter sends a pairing link). Once linked, all channels share the same user context.
