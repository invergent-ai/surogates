# Handoff: implementing office editors plan 5 on another machine

Written 2026-10-16. Picks up "comments that call the agent" (plan 5 of the office editors spec) where the laptop session left it. Read this first, then the plan.

## Where things stand

| Piece | State |
|---|---|
| Office editors plans 1a, 3, 1b, 4 | Merged into surogates `master`; 1a is in the v2.18.0 release, while 3, 1b and 4 are not released. Plan 4 = PR #531, merge `2a93e9333` (sheets, Claude Slides decks, PDF). |
| Plan 5 (this work) | Written and self-reviewed, **not vetted by Flavius yet, not committed**. Waits for his OK on the plan and the "Decided while planning" items, then execution. |
| Plan 2 (PROD rollout) | Next after plan 5. Not started. |
| Euro-Office forks | `invergent-ai/office-{documentserver,server,sdkjs,web-apps}`, patch branch `surogate/9.3.5`. First image build running on Blacksmith (see below). |

## Files

All in the surogates repo, branch `docs/office-editors-plan-5` (based on `master` at `2a93e9333`):

- **Plan:** `docs/superpowers/plans/2026-10-16-office-editors-5.md` (10 tasks, 67 steps).
- **Verify note:** `docs/superpowers/notes/2026-10-16-office-comments-verify.md`. It holds the probe results from the DEV Euro-Office 9.3.5 server: the notify payload in all four editors (verify item 13), how to list, look up, reply to and resolve threads per editor, and the traps.
- **This handoff.**
- **Spec:** `docs/superpowers/specs/2026-10-13-office-editors-design.md` (section "Comments that call the agent").

## Decisions already made (Flavius)

1. Mentions work in all four types: Word, spreadsheet, presentation and PDF.
2. The agent's reply in the comment thread states only the result, in one or two sentences, never the conversation. runtime-office refuses a thread reply over 400 characters.
3. In the thread: the worker posts a fixed acknowledgement ("On it, I'll reply here when done.") before the model runs. The agent then replies with the result and resolves last.
4. The agent is the only mentionable user, as `agent:<agent_id>`, with its name and the address `<agent_id>@agent.invalid`.
5. A mention goes to the chat whose file pane fired it.
6. The ops side is its own surogate-ops PR. The ops frontend picks up the SDK change only after an SDK release, and releasing is Flavius's call.

The plan's "Decided while planning" section lists the writer's own choices for Flavius to vet:
- a mention passes every `send_message` gate and is never taken as a pending answer;
- a mid-turn mention is acknowledged at the turn's next step;
- in cell, slide and PDF, reply and resolve go in a call of their own;
- the agent is offered only where runtime-office is configured, in edit mode;
- the spec's "on '…'" part is dropped.

## How plan 4 was executed (do the same)

- **Method:** `superpowers:subagent-driven-development`. Each task gets an implementer, then a task reviewer, then fix rounds, then a scoped re-review. The whole branch then gets a final review, a single fix wave, and a re-review.
  - A pre-flight conflict scan of the plan by a subagent found 14 real conflicts in plan 4 before any code was written. Do it again.
- **Every subagent runs with model `opus`.** This is a hard rule from Flavius.
- **Ledger:** keep it in the worktree's git-ignored `.superpowers/sdd/<plan>/progress.md`. Never keep it in `/tmp`: a reboot wiped `/tmp` once.
- **Commits:**
  - Conventional Commits.
  - Add named files only (`git add <paths>`, never `-a`).
  - No `Co-Authored-By` trailer.
  - Never mention plans, tasks, reviews or findings in code, comments or commit messages; comments state the behaviour that is the reason.
- **Python:** run `/work/surogates/.venv/bin/python -m pytest <files> -q -p no:cacheprovider` from the worktree root. Never use `uv run`.
- **Full suite:** run it at `-n 4` with the DEV stack stopped. At `-n 8`, together with DEV, it ran the 31 GB laptop out of memory and the machine rebooted. A bigger machine can go higher.
- **Expected failures:** about 81 `tests/test_local_history.py` failures are environmental when git is older than 2.44 (`fsck --no-references`).
- **runtime-office tests:**
  - `cd runtime-office && npx vitest run <file>`, `npm test`, `npm run typecheck`.
  - Measuring tests need Playwright's Chromium (`npx playwright install chromium`).
  - Integration tests (`*.it.test.ts`) run against the DEV document server when these are set: `OFFICE_IT_DS_URL=https://office.densemax.local`; `OFFICE_IT_JWT_SECRET`, read from `/work/surogates/config.dev.yaml` `office.jwt_secret` (never print it); and `OFFICE_IT_HOST=http://<this machine's vpn0 address>:8791`.
  - Run integration files one at a time, since they share port 8791. Don't export the integration env for `npm test` unless you mean to run every integration file at once.
- **Don't close an editor page while its pictures are still loading.** Euro-Office 9.3.5's docservice crashes on an aborted cache download. The fix is in our `office-server` fork, but DEV still runs the upstream image.

## On node1 (the machine taking over)

`ssh -i ~/digitalocean administrator@node1` (checked 2026-10-16):

- **Hardware:** 56 cores, 499 GB RAM, 192 GB free disk. The suites can run at full parallelism here (`-n 16` or more), and with git 2.53 the `test_local_history` failures should not appear.
- **Repositories:** in `/home/administrator/work/flavius/`, namely `surogates` (on `master` 9cba2c1b, behind origin), `surogate-ops` and `surogate-router`.
  - Fetch and make the worktree there: `git -C ~/work/flavius/surogates fetch origin && git -C ~/work/flavius/surogates worktree add ~/work/flavius/surogates/.worktrees/office-plan-5 docs/office-editors-plan-5`.
  - Work only in worktrees; leave the main checkout alone.
- **Tools:**
  - `surogates/.venv` is Python 3.12.15; use it for tests, not the system Python 3.14.
  - `uv` is in `~/.local/bin`.
  - Node v26.11.1 and npm come from nvm and are on the `PATH` only in a login shell: run Node commands through `bash -lc '…'`.
  - Playwright's Chromium is not installed yet; run `npx playwright install chromium` in runtime-office, which may need system libraries from root.
  - Claude Code is installed.
- **DEV document server:** node1 is the DEV host (`br0` 172.17.31.241, where DEV Postgres is too).
  - `office.densemax.local` does not resolve on node1 yet; `curl --resolve office.densemax.local:443:172.17.31.241` gets 200.
  - Needs, as root: an `/etc/hosts` line `172.17.31.241 office.densemax.local`.
  - Then `OFFICE_IT_DS_URL=https://office.densemax.local` and `OFFICE_IT_HOST=http://172.17.31.241:8791`. That address doesn't change, unlike the laptop's VPN address.
- **Still missing on node1:**
  - `config.dev.yaml` (secrets; the laptop has it at `/work/surogates/config.dev.yaml`), so put it at `~/work/flavius/surogates/config.dev.yaml` and point `SUROGATES_CONFIG` there;
  - the DEV kubeconfig, which wasn't in the home folder: `/etc/rancher/k3s/k3s.yaml` is root-only, and `~/work/flavius/prod-k3s.yml` is the **PROD** cluster's, never to be used for DEV work;
  - `.vscode/launch.json` (or just `SUROGATES_PLATFORM_API_TOKEN`), for the DEV processes.

## The other machine needs (general)

- **Network:** the VPN to DEV, so the DEV document server can reach this machine's `vpn0` address on port 8791, and the editor fixture server is reachable.
  - `vpn0`'s address changes when the VPN reconnects. Update `OFFICE_IT_HOST`, and `office.callback_base_url` in `config.dev.yaml` when running DEV processes.
- **CA trust:** the `densemax` mkcert CA trusted, by the system store and by Chrome's NSS database if a browser is used.
- **Repositories:**
  - surogates, with its venv at `/work/surogates/.venv`, and a worktree for the implementation branch. `/work/surogates` itself is shared by other sessions: never check out, pull or stash there; work only in worktrees.
  - surogate-ops (for Task 9).
- **Config:** `/work/surogates/config.dev.yaml` and `~/.surogate/kubeconfig` (DEV cluster), plus `.vscode/launch.json` in `/work/surogates`, which holds `SUROGATES_PLATFORM_API_TOKEN` for the DEV processes. Copy them from the laptop; they hold secrets, so never paste them anywhere.

## Running DEV for the walkthrough (Task 10, ask Flavius first)

From the implementation worktree:

1. **Build and start runtime-office:** `npm ci && npm run build`, then `RUNTIME_OFFICE_TOKEN=<config office.runtime_office_token> RUNTIME_OFFICE_IGNORE_TLS=1 PORT=8080 node dist/main.js`.
2. **Start the runtime** with `python -m surogates.cli.main api|worker|mcp-proxy` and `python -m surogates.scheduled.platform_ticker`. Set the env `SUROGATES_CONFIG=/work/surogates/config.dev.yaml SUROGATES_RUNTIME_MODE=shared SUROGATES_PLATFORM_API_URL=http://localhost:8888 SUROGATES_PLATFORM_API_TOKEN=<from launch.json>`.
   - mcp-proxy needs `SUROGATES_PORT=8002`; otherwise it takes the worker's health port 8001.
3. **Set up the ops venv:** install a surogates wheel built from the branch (`git archive HEAD`, then `SETUPTOOLS_SCM_PRETEND_VERSION=<x> uv build --wheel`, then `uv pip install --python .venv/bin/python --no-deps --reinstall <wheel>`), then `surogate-ops migrate upgrade`.
4. **Start ops:** `surogate-ops server` from `/work/surogate-ops`, on port 8888.
5. **Start the web client:**
   - copy `web/.env.local` (with `VITE_DEV_AGENT_ID`) from another surogates worktree;
   - run `npm ci` in `sdk/agent-chat-react` and in `web/`;
   - start it with `npm run dev`, then open http://localhost:5173.
6. **Publish the skills to the DEV Hub:** `/work/surogate-ops/.venv/bin/surogate-ops seed-builtin-skills --source <worktree>/skills`.
7. **Find a session's events:**
   - DEV Postgres: CNPG `surogate-pg` in namespace `default` on the DEV cluster;
   - run `kubectl exec <primary> -c postgres -- psql -d surogates` with `KUBECONFIG=~/.surogate/kubeconfig`;
   - `user.message` text is `data->>'content'`.

## Euro-Office forks (context; not part of plan 5)

- **Clones:** under `/work/invergent-office/` on the laptop. Notes are in `/work/invergent-office/notes/fork-setup.md`.
- **Branches:** `office-documentserver` has `surogate/9.3.5` as its default branch. It points `server`, `sdkjs` and `web-apps` at our forks; the first patch is ONLYOFFICE's docservice crash fix on `office-server` (`b8a2c2bf5`).
- **Image build:** workflow `.github/workflows/surogate-office-docs.yml`, Blacksmith runner, manual trigger only. It builds `ghcr.io/invergent-ai/office-docs`.
  - First run: https://github.com/invergent-ai/office-documentserver/actions/runs/38863771106. It takes about 2 h because the C++ core compiles cold.
  - After it pushes, Flavius sets the package to public in GitHub's web interface.
- **Decided:** stay on Euro-Office, not ONLYOFFICE Community Edition. CE 10.0 blocks the connector (`advancedApi` false) and runs as one in-memory process.

## Open items to carry

- Flavius vets plan 5 and chooses the execution method; subagent-driven is recommended.
- **Plan 4's parked minor most worth fixing:** a coloured pill inside a table cell gets the advice "draw it as a `<div>`", which is itself refused in a cell. The fix is one message change.
- **For plan 2 (PROD):**
  - check the PROD document server's restart count;
  - deploy the forked image;
  - add the slide fonts (e.g. Montserrat) to the document server;
  - give runtime-office its own chart.
- **Org-level setting for Flavius:** every Actions runner group in `invergent-ai` accepts public repositories, the self-hosted default group included.
