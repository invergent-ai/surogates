# Browser Use

Browser use gives an agent a real, session-scoped Chromium browser. The agent can
navigate pages, inspect accessibility state, click, type, scroll, drag, wait, and
capture screenshots. In the web channel, users can watch the live browser and
temporarily take manual control for login, MFA, CAPTCHA, or other tasks where
human input is required.

The browser is separate from the execution sandbox, but it mounts the same
session workspace at `/workspace`. Sandbox tools operate on files and shell
commands there; browser tools run in the worker and talk to a dedicated browser
container or pod.

## Quick Start

Start a normal web chat session and ask for an interactive web task. For
example:

```text
Open https://example.com in the browser, inspect the page, take a screenshot,
and tell me the page title.
```

That prompt should cause the agent to call `browser_navigate`, then
`browser_get_state` or `browser_screenshot`. When the first browser tool runs,
the session provisions a browser and the web UI shows the live browser pane.

For a task that exercises user control handoff, use a prompt like:

```text
Open my account settings page. If you reach a login, pause and let me take over
the browser so I can sign in, then continue after I release control.
```

## What It Is For

Use browser tools when the task needs an interactive web page rather than static
page extraction:

- Fill out forms, move through multi-step flows, or verify UI behavior.
- Inspect rendered pages, not just HTML or text.
- Interact with sites that require JavaScript.
- Ask the user to complete an authentication step, then continue from the same
  browser session.

Use `web_search` and `web_extract` for simple research or page text retrieval.
They are cheaper and do not allocate a browser.

## Lifecycle

Each session gets at most one browser instance. It is created lazily on the first
`browser_*` tool call and reused by later browser calls in that same session.

1. The agent calls a browser tool, such as `browser_navigate`.
2. The worker provisions a browser through the configured backend.
3. Browser metadata is mirrored in Redis so API servers can resolve it for live
   view and control endpoints.
4. The worker emits `browser.provisioned`; the web UI opens the live browser
   pane.
5. The agent continues using the same browser until it calls `browser_close` or
   the session ends.
6. Teardown emits `browser.destroyed`.

In production, the browser backend is Kubernetes and provisions one pod and one
Service per active browser session. In local development, the process backend can
run the same browser image with Docker and fixed host port ranges.

## Browser Tools

Browser tools are registered under the `browser` toolset and run in the worker.
Every call still passes through governance before execution.

| Tool | Purpose |
|---|---|
| `browser_navigate` | Navigate to a URL and return the final URL and page title. |
| `browser_get_state` | Return the rendered page's accessibility tree with stable `@eN` refs. |
| `browser_click` | Click an `@eN` ref or viewport coordinates. |
| `browser_type` | Type text at the current focus or into an `@eN` ref. |
| `browser_press_key` | Press keys or chords such as `Enter`, `Tab`, or `Control+L`. |
| `browser_scroll` | Scroll at viewport coordinates. |
| `browser_drag` | Drag along a path of viewport coordinates. |
| `browser_wait` | Wait up to 30 seconds for page transitions or async UI work. |
| `browser_screenshot` | Capture a bounded PNG screenshot, optionally with numbered annotations. |
| `browser_close` | Close the browser for the current session. |

`browser_get_state` is the default perception channel. It caches element refs
such as `@e3`, which `browser_click` and `browser_type` can use later. After
navigation or large page changes, call `browser_get_state` again to refresh refs.

`browser_screenshot` saves each PNG in the session workspace under
`browser-screenshots/` and returns both a tool-usable absolute `path`, such as
`/workspace/browser-screenshots/...png`, and a workspace-relative
`relative_path`. It never returns inline base64 image data; pass the returned
path to `vision_analyze` or use workspace/file tools to inspect or move the
image.

## Live View And User Control

The web UI shows a browser pane once a session provisions a browser. The user's
browser never talks directly to the browser pod. It talks to the API server,
which authenticates the request, checks tenant/session scope, resolves the
browser, and proxies the browser live view.

Users can acquire browser control from the live view. While user control is held:

- The API records the holder in Redis and emits `browser.control_granted`.
- Browser tool calls return `paused_by_user`.
- The harness injects guidance telling the agent to wait for the user.
- Releasing control emits `browser.control_returned` and wakes the session.

Only the current holder can release control. If another user tries to acquire an
already-held browser, the API returns a conflict.

## API Endpoints

Browser live view and control endpoints are part of the session API:

| Endpoint | Purpose |
|---|---|
| `GET /v1/sessions/{id}/browser/state` | Resolve the session browser and return `live` or `user-control` status plus the live-view path. |
| `POST /v1/sessions/{id}/browser/control` | Acquire or release manual browser control. Body: `{"action":"acquire"}` or `{"action":"release"}`. |
| `GET /v1/sessions/{id}/browser/live/{path}` | Proxy live-view HTTP assets through the API server. |
| `WS /v1/sessions/{id}/browser/live/{path}` | Proxy live-view websocket paths through the API server. |

The API also exposes `/v1/api/sessions/...` variants for internal callers. Normal
service-account clients should not use the interactive browser endpoints.

## Configuration

The Helm chart enables the Kubernetes browser backend by default:

```yaml
browser:
  backend: "kubernetes"
  image: "ghcr.io/invergent-ai/surogates-agent-browser:latest"
  resources:
    requests:
      cpu: "1"
      memory: 2Gi
    limits:
      cpu: "2"
      memory: 4Gi
  podReadyTimeout: 60
  activeDeadlineSeconds: 3600
  serviceAccountSuffix: "browser"
  k8sNamespace: ""
  blockedCidrs: []
```

The Python configuration uses the `SUROGATES_BROWSER_` environment prefix:

| Setting | Environment variable | Default |
|---|---|---|
| `browser.backend` | `SUROGATES_BROWSER_BACKEND` | `process` in raw config, `kubernetes` in Helm |
| `browser.image` | `SUROGATES_BROWSER_IMAGE` | `ghcr.io/invergent-ai/surogates-agent-browser:latest` |
| `browser.k8s_namespace` | `SUROGATES_BROWSER_K8S_NAMESPACE` | `surogates` |
| `browser.k8s_service_account` | `SUROGATES_BROWSER_K8S_SERVICE_ACCOUNT` | `surogates-browser` |
| `browser.pod_ready_timeout` | `SUROGATES_BROWSER_POD_READY_TIMEOUT` | `60` |
| `browser.active_deadline_seconds` | `SUROGATES_BROWSER_ACTIVE_DEADLINE_SECONDS` | `3600` |
| `browser.cpu` | `SUROGATES_BROWSER_CPU` | `1` |
| `browser.memory` | `SUROGATES_BROWSER_MEMORY` | `2Gi` |
| `browser.cpu_limit` | `SUROGATES_BROWSER_CPU_LIMIT` | `2` |
| `browser.memory_limit` | `SUROGATES_BROWSER_MEMORY_LIMIT` | `4Gi` |

For the process backend, these fixed port bases are also available:

| Setting | Environment variable | Default |
|---|---|---|
| `browser.rest_port_base` | `SUROGATES_BROWSER_REST_PORT_BASE` | `30000` |
| `browser.cdp_port_base` | `SUROGATES_BROWSER_CDP_PORT_BASE` | `31000` |
| `browser.live_view_port_base` | `SUROGATES_BROWSER_LIVE_VIEW_PORT_BASE` | `32000` |

## Deployment Notes

Release builds publish the browser image from `images/browser/Dockerfile` as:

```text
ghcr.io/invergent-ai/surogates-agent-browser:<version>
ghcr.io/invergent-ai/surogates-agent-browser:latest
```

The worker needs RBAC to create, watch, and delete browser pods and Services.
Browser pods run under their own ServiceAccount with no Kubernetes API
permissions. The API server needs the same browser settings as the worker so it
can resolve live-view targets if Redis metadata is stale.

## Security Model

Browser pods are intentionally not the execution sandbox:

- They do not receive database, Redis, API, or LLM credentials.
- The browser container gets `/workspace`; in Kubernetes, an s3fs sidecar holds
  session-scoped storage credentials for that mount.
- Their ServiceAccount has no Kubernetes permissions.
- The API server authenticates and authorizes all live-view and control traffic.
- Browser tool calls pass through governance, including URL checks for
  `browser_navigate`.
- NetworkPolicy permits API and worker ingress to browser pods. Browser egress is
  intentionally available because browsing the public web is the feature.

If the browser backend is unavailable, browser tools return a structured
`browser_unavailable` result with guidance instead of failing the whole session.
