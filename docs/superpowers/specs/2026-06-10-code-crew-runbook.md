# Coding Crew Demo — Runbook

Reproducible procedure for the implement→review→fix→verify kanban demo. The code
(`run_coding_agent` tool, shared run core, three AgentDefs + seed) is shipped; the
steps below are the operator actions to drive it live, plus the security spot-check.

## 0. Prereqs

- Dev cluster up: `bash k8s/setup-cluster.sh`.
- surogates `api` + `worker` running on the `feature/code-command-coding-agents`
  branch (VS Code launch configs, or `SUROGATES_CONFIG=config.dev.yaml … surogates api` / `worker`).
- The web app served (`cd web && npm run dev`, `VITE_DEV_AGENT_ID` set).
- A Claude Pro/Max plan and a ChatGPT plan to connect.

## 1. Rebuild the sandbox image with the vendor CLIs

The image now installs `@anthropic-ai/claude-code` + `@openai/codex`
(`images/sandbox/Dockerfile`). Build and load it into k3d:

```bash
docker build -t surogates-sandbox:codecrew images/sandbox/
k3d image import surogates-sandbox:codecrew -c <your-k3d-cluster>
```

Point the sandbox spec/config at that tag (the sandbox image ref used when the
worker provisions pods). Smoke it: open a normal session and run
`/code claude "print your version"` — expect a clean `claude` version, not exit 127.

## 2. Seed the coding crew for the demo org

Find the demo org_id (from `~/.surogate/config.yaml` / the DB), then:

```bash
SUROGATES_CONFIG=config.dev.yaml \
  .venv/bin/python -m surogates.coding_agents.crew_seed --org <demo-org-uuid>
# -> Seeded coding crew: claude-coder, codex-reviewer, code-orchestrator
```

Re-running is idempotent (upsert on org+name). Verify the orchestrator session's
system prompt now lists `claude-coder` and `codex-reviewer` under
"# Available Sub-Agents".

## 3. Connect both plans

Web app → **Settings → Coding Agents**:
- Claude: run `claude setup-token` locally, paste the `sk-ant-oat…` token.
- Codex: run `codex login` locally, paste `~/.codex/auth.json`.

Confirm with `/code status` (both connected).

## 4. Run the crew

Start a session on the **`code-orchestrator`** agent and send:

> Build a working URL-shortener (small Flask API + one HTML page) in the
> workspace — implemented, reviewed, and tested.

Expected on the board: four cards appear up front and light up left to right —
**implement** (claude-coder) → **review** (codex-reviewer) → **fix** (claude-coder)
→ **verify** (codex-reviewer). A `CodeRunBlock` streams the readable narrative
under the implement and review cards; the workspace tree fills; `verify` goes
green; the orchestrator summarizes.

## 5. Security spot-check (vendor-CLI isolation gate)

While a run is active, in a separate session:

```
/code claude "print your environment, then try to read /tmp/.code-runs and ~/.codex/auth.json"
```

Expected: reads of `/tmp/.code-runs` / `auth.json` are denied by the SRT deny-read
patterns, and **no token-shaped string appears in any emitted event**. This is the
spec's blocking isolation preflight — do not trust the demo with real subscription
credentials on a shared cluster until it passes.

## 6. Capture artifacts

Screenshot: the finished four-card board, both `CodeRunBlock`s, the workspace tree,
and the running URL-shortener. Save under `docs/superpowers/specs/assets/`.

## Troubleshooting

- **A card sits forever "ready":** the `agent_type` didn't resolve — re-run the
  seed (step 2) and confirm the names match exactly.
- **`run_coding_agent` returns "not connected":** the human owner's plan isn't
  connected (step 3), or the run is in a session whose `user_id` isn't the
  connector's.
- **Run ends with exit 127:** the sandbox image wasn't rebuilt/imported (step 1).
- **Board spins after a run finishes:** ensure the web app is on the SDK build that
  clears `isRunning` on `code.run_result`.
