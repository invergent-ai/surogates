# 12. MCP Integration

The Model Context Protocol (MCP) allows agents to use external tools hosted by third-party servers. Surogates includes a full MCP client, a credential-injecting proxy, and security scanning for MCP tool definitions.

## MCP Client

The MCP client connects to external MCP servers and registers their tools in the agent's tool registry. Two transport modes are supported:

### Stdio Transport

The client launches the MCP server as a subprocess and communicates via stdin/stdout. Suitable for locally-installed tools.

**Example configuration:**

```yaml
mcp_servers:
  github:
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_TOKEN_REF: github_token   # resolved from credential vault
    timeout: 120
```

### HTTP / StreamableHTTP Transport

The client connects to a remote MCP server over HTTP. Suitable for cloud-hosted tools and shared infrastructure.

**Example configuration:**

```yaml
mcp_servers:
  jira:
    transport: http
    url: "https://mcp.acme.com/jira"
    auth: oauth
    oauth:
      client_id: "surogates-agent"
      scope: "read write"
    timeout: 120
```

### Features

| Feature | Description |
|---|---|
| **Auto-reconnect** | Exponential backoff with up to 5 retries on connection loss |
| **Sampling** | MCP servers can request LLM completions back through the client |
| **Environment filtering** | Only explicitly allowed env vars are passed to stdio servers |
| **Credential stripping** | Secrets are scrubbed from error messages |
| **Per-server timeout** | Configurable timeout per MCP server |
| **Thread safety** | Dedicated background event loop for MCP connections |

## OAuth 2.1 PKCE for MCP Servers

MCP servers that require authentication can use the OAuth 2.1 PKCE flow. The client handles the full authorization flow:

```
1. Agent needs to call an OAuth-protected MCP tool
2. MCP client checks for cached tokens (on disk)
3. If no valid token:
   a. Start ephemeral localhost HTTP server for redirect
   b. Open browser to authorization URL with PKCE challenge
   c. User approves in browser
   d. Callback server receives authorization code
   e. Exchange code for access + refresh tokens
   f. Store tokens on disk for reuse
4. Attach access token to MCP requests
5. Auto-refresh when token expires
```

**Configuration example:**

```yaml
mcp_servers:
  salesforce:
    url: "https://mcp.salesforce.com/mcp"
    auth: oauth
    oauth:
      client_id: "pre-registered-id"
      client_secret: "secret"
      scope: "api refresh_token"
      redirect_port: 0           # auto-pick available port
      client_name: "Surogates Agent"
      token_dir: "/var/lib/surogates/tokens"
```

## MCP Proxy

The MCP proxy sits between sandboxes and external MCP servers. It injects credentials from the vault so that the sandbox never sees them. Deployed as a separate K8s service, it is the only external endpoint sandbox pods can reach (enforced by NetworkPolicy).

See the **[MCP Proxy](proxy.md)** page for the full setup guide, including credential refs, network isolation, configuration reference, and troubleshooting.

## MCP Server Configuration

MCP servers are registered in the surogates `mcp_servers` table.  Two scopes:

| Scope | Row | Managed by |
|---|---|---|
| **Org-wide** | `org_id=<org>, user_id=NULL` | Org admin via the ops API / Studio UI |
| **User-specific** | `org_id=<org>, user_id=<user>` | Org admin via the ops API |

User-specific rows override org-wide rows with the same name. The MCP proxy reads from this table on every tenant's first tool-discovery call (per (org_id, user_id) tenant pool entry) — see [MCP Proxy](proxy.md).

Each row carries `transport` (`stdio` or `http`), `command`/`args`/`env` (stdio), or `url` (http), an optional `credential_refs` array pointing at vault entries, and a `timeout`. Secrets must never be inlined in `env` — use `credential_refs` so the proxy resolves them via the encrypted credential vault at connect time.

## MCP Security Scanning

Every MCP tool definition is scanned before registration. The scanner combines pattern-based checks with the Microsoft AGT `MCPSecurityScanner`.

### Threat Detection

| Threat | Detection Method |
|---|---|
| **Invisible unicode** | Zero-width chars, bidi marks, ZWJ sequences in tool names/descriptions |
| **Prompt injection** | 9 regex patterns: "ignore previous", "override instructions", "new system prompt", etc. |
| **Hidden HTML** | HTML comments that could contain invisible instructions |
| **Tool poisoning** | Deceptive descriptions that trick the LLM into dangerous behavior |
| **Schema abuse** | Malicious default values or enum entries in tool parameters |

### Rug-Pull Detection

The scanner maintains SHA-256 fingerprints of tool definitions. If a tool's definition changes between connections (a "rug-pull" attack), the scanner flags it:

```
First connection:
  tool "create_issue" -> fingerprint: abc123...
  
Later connection:
  tool "create_issue" -> fingerprint: def456...  (different!)
  --> rug_pull event emitted, tool blocked until admin reviews
```

### Scan Results

Each scan produces a result with:
- **safe**: boolean -- did the tool pass all checks?
- **threats**: list of human-readable threat descriptions
- **severity**: `info`, `warning`, or `critical`

Tools that fail scanning are not registered. Scan results are logged as events for audit purposes.
