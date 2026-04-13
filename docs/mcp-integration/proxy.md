# MCP Proxy

The MCP proxy is a credential-injecting proxy service that sits between sandbox pods and external MCP servers. It ensures secrets never enter the untrusted sandbox environment.

## Architecture

```
Sandbox pod (untrusted)
  → POST http://mcp-proxy:8001/mcp/v1/tools/call
  → MCP Proxy (trusted)
    1. Validate sandbox JWT
    2. Load MCP server configs (platform volume + DB)
    3. Resolve credential_refs from encrypted vault
    4. Inject credentials into MCP server connection
    5. Forward tool call to MCP server
    6. Strip credentials from response
  ← Return sanitized result to sandbox
```

Sandbox pods can **only** reach the MCP proxy (enforced by K8s NetworkPolicy). They never see API keys, tokens, or passwords.

## Prerequisites

- Surogates API server, worker, PostgreSQL, and Redis running
- The `mcp` Python package installed (`uv pip install mcp`)
- A Fernet encryption key for the credential vault

## 1. Generate an Encryption Key

The credential vault encrypts secrets at rest using Fernet (AES-128-CBC + HMAC-SHA256):

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Save the output — you'll need it for both the MCP proxy and any process that stores credentials.

## 2. Register an MCP Server

MCP servers can be configured in three ways, merged with platform < org < user precedence.

### Option A: Platform Config (filesystem)

Create a JSON file at `/etc/surogates/mcp/servers.json` (mounted from a ConfigMap in K8s):

```json
{
  "filesystem": {
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"],
    "timeout": 120
  },
  "github": {
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"],
    "credential_refs": ["GITHUB_TOKEN"]
  }
}
```

### Option B: Database (via Admin API)

Register an MCP server for an org:

```bash
# Store the credential first
curl -X POST http://localhost:8000/v1/admin/credentials \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "org_id": "ORG_UUID",
    "name": "GITHUB_TOKEN",
    "value": "ghp_your_github_pat_here"
  }'

# Register the MCP server
curl -X POST http://localhost:8000/v1/admin/mcp-servers \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "org_id": "ORG_UUID",
    "name": "github",
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"],
    "credential_refs": [{"name": "GITHUB_TOKEN", "env": "GITHUB_PERSONAL_ACCESS_TOKEN"}]
  }'
```

### Option C: Per-user MCP server

Same as Option B but include `user_id` — the server is only available to that user:

```bash
curl -X POST http://localhost:8000/v1/admin/mcp-servers \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "org_id": "ORG_UUID",
    "user_id": "USER_UUID",
    "name": "my-private-server",
    "transport": "http",
    "url": "https://my-mcp-server.example.com/mcp",
    "credential_refs": [{"name": "MY_API_KEY", "header": "X-API-Key"}]
  }'
```

## 3. Credential Refs

The `credential_refs` field on an MCP server config tells the proxy which credentials to resolve from the vault and where to inject them.

### Simple string format

```json
"credential_refs": ["GITHUB_TOKEN"]
```

- For **stdio** servers: injected as `env.GITHUB_TOKEN`
- For **http** servers: injected as `headers.Authorization: Bearer <value>`

### Structured object format

```json
"credential_refs": [
  {"name": "MY_TOKEN", "env": "GITHUB_PERSONAL_ACCESS_TOKEN"},
  {"name": "API_KEY", "header": "X-API-Key"},
  {"name": "AUTH", "header": "Authorization", "prefix": "Bearer "}
]
```

| Field    | Description                                    |
| -------- | ---------------------------------------------- |
| `name`   | Credential name in the vault (required)        |
| `env`    | Inject as this environment variable (stdio)    |
| `header` | Inject as this HTTP header (http)              |
| `prefix` | Prepend this string to the value (e.g. `Bearer `) |

Credential resolution tries the user-scoped credential first, then falls back to the org-scoped credential with the same name.

## 4. Run the Proxy

### Local Development

```bash
export SUROGATES_ENCRYPTION_KEY="your-fernet-key-here"
SUROGATES_CONFIG=config.dev.yaml surogates mcp-proxy
```

The proxy starts on port 8001 by default.

### Tell the Worker to Use the Proxy

Set `mcp_proxy_url` in your config or environment:

```bash
export SUROGATES_MCP_PROXY_URL="http://localhost:8001"
SUROGATES_CONFIG=config.dev.yaml surogates worker
```

When `mcp_proxy_url` is empty (the default), the worker connects to MCP servers directly — suitable for local development without the proxy.

### Kubernetes

The proxy is deployed as a separate Deployment. Apply the manifest:

```bash
kubectl apply -f k8/base/mcp-proxy-deployment.yaml
```

Required K8s secrets:

| Secret                       | Key   | Value                           |
| ---------------------------- | ----- | ------------------------------- |
| `surogates-db`               | `url` | PostgreSQL connection string    |
| `surogates-redis`            | `url` | Redis connection string         |
| `surogates-jwt`              | `secret` | JWT signing secret           |
| `surogates-credential-key`   | `key` | Fernet encryption key           |

Create the credential key secret:

```bash
kubectl -n surogates create secret generic surogates-credential-key \
  --from-literal=key="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
```

Add `mcp_proxy_url` to the worker deployment:

```yaml
# In worker-deployment.yaml, add to env:
- name: SUROGATES_MCP_PROXY_URL
  value: "http://mcp-proxy.surogates.svc:8001"
```

## 5. Network Isolation

The `sandbox-networkpolicy.yaml` restricts sandbox pods to only reach:

- **MCP proxy** (port 8001) — for MCP tool calls
- **Garage S3** (port 3900) — for workspace FUSE mount
- **DNS** (port 53) — for name resolution

All other egress is denied. The sandbox cannot reach the database, Redis, API server, or the internet directly.

## 6. Verify

Check the proxy health:

```bash
curl http://localhost:8001/health
# {"status":"ok"}
```

## Configuration Reference

| Environment Variable               | Default                | Description                              |
| ---------------------------------- | ---------------------- | ---------------------------------------- |
| `SUROGATES_HOST`                   | `0.0.0.0`             | Bind address                             |
| `SUROGATES_PORT`                   | `8001`                 | Bind port                                |
| `SUROGATES_WORKERS`               | `1`                    | Uvicorn worker count                     |
| `SUROGATES_ENCRYPTION_KEY`         | (empty)                | Fernet key for credential vault          |
| `SUROGATES_IDLE_CONNECTION_TIMEOUT`| `300`                  | Seconds before idle MCP connections close |
| `SUROGATES_MAX_CONNECTIONS_PER_ORG`| `50`                   | Max concurrent MCP connections per org   |
| `SUROGATES_PLATFORM_MCP_DIR`      | `/etc/surogates/mcp`  | Platform MCP config directory            |
| `SUROGATES_DB_URL`                 | —                      | PostgreSQL connection string             |
| `SUROGATES_REDIS_URL`             | —                      | Redis connection string                  |
| `SUROGATES_JWT_SECRET`            | —                      | JWT signing secret                       |

## Troubleshooting

| Problem                                | Solution                                                                                          |
| -------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `401 Invalid token`                    | The sandbox JWT has expired or the `SUROGATES_JWT_SECRET` doesn't match between proxy and worker  |
| `403 Expected a sandbox token`         | The token's `type` claim is not `"sandbox"` — ensure the worker is minting sandbox tokens         |
| `404 No MCP servers configured`        | No servers in platform volume or DB for this org/user — check `/etc/surogates/mcp/` and DB       |
| `credential not found` warnings        | The `credential_refs` name doesn't match any entry in the `credentials` table for this org        |
| MCP server connection failures         | Check that the MCP server command (e.g. `npx`) is available in the proxy container's `PATH`       |
| Sandbox can't reach proxy              | Verify the `sandbox-networkpolicy.yaml` allows egress to `component: mcp-proxy` on port 8001      |
| Credentials appearing in error output  | Should not happen — credentials are automatically stripped from errors. File a bug if it does.     |
