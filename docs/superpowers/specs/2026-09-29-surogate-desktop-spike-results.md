# Surogate Desktop First Spike: Results

Spec: `2026-09-29-surogate-desktop-design.md`, Section 10.
Plan: `docs/superpowers/plans/2026-09-29-surogate-desktop-spike.md`.
Probe code: branch `spike/desktop-platform`, `spikes/desktop/` (not merged).

## Environment

| Item | Value |
|---|---|
| Electron | 44.5.0 |
| @anthropic-ai/sandbox-runtime | 0.0.77 |
| playwright-core | 1.63.0 |
| Ubuntu 24.04 VM | 24.04.5 LTS, cloud image 20260926, kernel 6.8.0-142-generic, AppArmor parser 4.0.1, apparmor_restrict_unprivileged_userns = 1 |
| Ubuntu 26.04 VM | 26.04.1 LTS, cloud image 20260927, kernel 7.0.0-34-generic, AppArmor parser 5.0.2, apparmor_restrict_unprivileged_userns = 1 |
| Browsers (both VMs) | Google Chrome 154.0.8037.92, Microsoft Edge 154.0.4258.37 (.deb); Chromium = Snap (`/usr/bin/chromium` → `/usr/bin/snap`) |
| Workstation (Q6) | Ubuntu 24.04.4 LTS, kernel 7.0.0-28-generic |

Setup notes:
- Electron 44 has no `postinstall`: `npm ci` leaves no binary, so `stage.sh` runs `node_modules/electron/install.js` itself. The real release build must do the same.
- Without an AppArmor profile, the staged app aborts on both releases: `FATAL:setuid_sandbox_host.cc:166] The SUID sandbox helper binary was found, but is not configured correctly. Rather than run without sandboxing I'm aborting now. You need to make sure that …/chrome-sandbox is owned by root and has mode 4755.` (exit 133). Q1 covers the fix.

## Summary

| # | Question | Result | Design change |
|---|---|---|---|
| 1 | Electron and srt under AppArmor | YES (with changes) | install-time AppArmor profile; app ships its own `bwrap` (`bwrapPath`); app folder readable in the sandbox |
| 2 | Per-root isolation, read policy, package installs | YES (with changes) | `denyRead: ["/"]` + explicit re-allows incl. srt's bridge sockets; spawn with app env only; per-host `CLAUDE_CODE_TMPDIR`; session allows are app state |
| 3 | Background servers across commands and from the browser | NO per command; YES with a runner | one long-lived sandboxed runner per root session; browser access by explicit forwarding over an inherited descriptor |
| 4 | Installed browsers, launch flags, downloads, network guard | PARTIAL | Chrome/Edge .deb verified; `ignoreDefaultArgs` for Playwright's weakening defaults; pinning proxy is the network boundary; no per-session private-network grants in a shared profile; `navigate` rejects non-http(s) |
| 5 | Worker recovery without repeating an effect | NO today; YES for the Section 2 protocol (model) | durable invocation replay between `_rebuild_messages` and the first LLM call, so `sanitize_tool_pairs` never stubs a desktop call; reuse `lease_token` fencing |
| 7 | File-operation boundary under races | PARTIAL | helper inside srt + `O_NOFOLLOW`/`fstat`/`nlink`; host `chdir`s to the folder; raw API enforces protected files itself; refuse multiply-linked files at binding |

<!-- One section per question follows, each with: Result, Environment, Evidence, Design consequence. -->

## Q1: Electron and srt under Ubuntu's AppArmor restriction

Result: YES, with three required changes (see Design consequence).

Environment: both VMs (24.04.5, AppArmor 4.0.1; 26.04.1, AppArmor 5.0.2), Electron 44.5.0, srt 0.0.77.

Evidence:
- Without our profile, the staged app aborts on both releases with Electron's SUID-helper FATAL (see Setup notes). Plain `bwrap --ro-bind / / --unshare-user --unshare-net -- true` fails on 24.04 (`bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`, exit 1) and succeeds on 26.04, which ships `/etc/apparmor.d/bwrap-userns-restrict`.
- With `q1/surogate-desktop.apparmor` loaded (`flags=(unconfined)` plus `userns`), Electron starts with its sandbox on both releases: the renderer runs in its own user namespace (24.04: `user:[4026532466]` against the main process's `user:[4026531837]`) with `Seccomp: 2`, no process carries `--no-sandbox`, and every process is labelled `surogate-desktop (unconfined)`.
- The first sandboxed command failed on both with `bash: …/sandbox-runtime/vendor/seccomp/x64/apply-seccomp: No such file or directory` (exit 127). srt runs that helper inside the sandbox, and with the home folder denied the app's own install folder was invisible. Re-allowing the install folder for reads fixed it.
- Then 24.04 passed (`1000 / surogate-desktop (unconfined) / user:[…] / wrote`, file created in the folder) and 26.04 failed: `apply-seccomp: write /proc/self/setgroups (nested userns is capability-restricted; caller must provide CAP_SYS_ADMIN): Permission denied`. On 26.04 every child of `/usr/bin/bwrap` runs in the stacked `unpriv_bwrap` profile, which has `audit deny capability`, so srt's seccomp helper cannot set up its nested user namespace.
- Plan Step 4's variant (`/usr/bin/bwrap rix,` in our profile) had no effect: a profile in `unconfined` mode does not apply exec rules, and `bwrap` still attaches to Ubuntu's profile.
- srt accepts a top-level `bwrapPath`. A copy of `bwrap` in the app's install folder (`<version>/bin/bwrap`) is not matched by Ubuntu's profile, inherits `surogate-desktop`, and passes on both releases. With `SPIKE_SYSTEM_BWRAP=1` (system `bwrap`) 26.04 fails again with the same error, so the attachment is the cause.
- The Task 1 smoke test (`q0`) passes on both releases after these changes (`sandboxed-ok`).
- JSON: `.superpowers/sdd/2026-09-29-surogate-desktop-spike/results/{u24,u26}/q1-ubuntu.json`, `q0-ubuntu.json` (both VMs have hostname `ubuntu`).

Design consequence:
1. Section 9: the install script writes this AppArmor profile (accepted by both parsers with `abi <abi/4.0>`) whenever `kernel.apparmor_restrict_unprivileged_userns=1`:
   `profile surogate-desktop /home/*/.local/share/surogate/versions/*/surogate flags=(unconfined) { userns, include if exists <local/surogate-desktop> }`.
2. Section 4 and 9: the app ships its own `bwrap` in `<version>/bin/bwrap` and passes it to srt as `bwrapPath`. Ubuntu 26.04's `bwrap-userns-restrict` otherwise breaks srt's seccomp helper. This deliberately gives the app's `bwrap` the same user-namespace permission the app itself has, which is exactly the 24.04 behaviour. bubblewrap is LGPL-2.0-or-later, so bundling needs its licence notice.
3. Section 4: the read policy always re-allows the app's install folder (read-only), because srt executes `vendor/seccomp/<arch>/apply-seccomp` from there inside the sandbox.

## Q2: Per-root isolation, the read policy and package installs

Result: YES, after four corrections to how the tool host drives srt (see Design consequence).

Environment: both VMs; three roots running concurrently, one `utilityProcess` host each: `A` (plain), `B My Files ü` (spaces and non-ASCII, Review Focus 2) and `C-link` → `/mnt/spike-data/C` (symlinked root on a separate loop-mounted ext4, Review Focus 3); both variants `root-deny` (`denyRead: ["/"]` + re-allows) and `home-deny` (`denyRead: [home, /mnt, /media, /srv, /tmp, /var/tmp]`).

Evidence (final run, both releases, both variants, all three roots, 0 failed checks; scoring script `q2-score.jq` in the plan workspace):
- Own folder write/read works; `~/Documents/private.txt`, `~/.ssh/spike_secret`, the other root's seed file and a write into the other root all fail with "No such file or directory": denied paths are absent inside the sandbox, not merely unreadable.
- `/tmp` is private per sandbox: a marker written to `/tmp` by one root is invisible to the other (`crossTmp` both directions fail).
- `TMPDIR` points at that root's session folder and is writable; `node` (nvm), `git` and `python3` run; `npm install left-pad` and `pip install six` succeed with caches in the session temp folder.
- `registry.npmjs.org` (allowed) returns 200; `example.com` triggers the ask callback per root; root A (allowed) gets 200, roots B and C (denied) do not; A's allow does not reach B or C.
- No Docker socket is reachable; `busctl --user` fails with "Failed to connect to bus: Operation not permitted".
- The symlinked root on another mount works with a policy built from its resolved path; the non-ASCII root behaves exactly like A.
- `getLinuxGlobPatternWarnings()` is empty in every host.

Failures found and corrected on the way (each confirmed by a before/after run, diagnostic probe `q9`):
1. `wrapWithSandboxArgv` returns the caller's own `process.env` as `env` on Linux (the proxy settings are baked into `argv`). Spreading it over the command's environment replaced our `PATH` (`node: command not found`) and would leak the host process's environment into every command. The host now spawns with the environment it built, only.
2. srt sets the sandbox's `TMPDIR` from the host process's `CLAUDE_CODE_TMPDIR`, defaulting to `/tmp/claude`, which is shared by every sandbox and did not exist. Each root's host now starts with `CLAUDE_CODE_TMPDIR` set to that session's temp folder.
3. With `/tmp` hidden (either variant), every connection failed with `curl: (56) Proxy CONNECT aborted` and the ask callback never ran: srt's in-sandbox relay reaches the host proxy through Unix sockets in the host's `/tmp` (`/tmp/claude-http-<id>.sock`). After `initialize`, the host re-allows exactly `getLinuxHttpSocketPath()` and `getLinuxSocksSocketPath()` through `updateConfig`; the rest of `/tmp` stays hidden.
4. The probe's `npm init -y` failed only in `B My Files ü` because npm derives an invalid package name (`b-my-files-ü`) from the folder name. Not a sandbox issue; the probe writes a fixed `package.json`.

Other observations:
- srt does not remember an "allow" answer: root A was asked again on its second request to `example.com`. "Allow for this session" must be implemented by the app (add the domain to that root's `allowedDomains` with `updateConfig`, which applies network changes live).
- `root-deny` is chosen for the remaining probes (`SPIKE_VARIANT=root-deny`): it passes everything and also hides other mounts without listing them.

JSON: `results/{u24,u26}/q2-ubuntu.json`, `q9-ubuntu.json` (plan workspace).

Design consequence:
1. Section 4, Reads: adopt `denyRead: ["/"]` with `allowRead` = system runtime paths (`/usr /bin /sbin /lib /lib64 /etc /opt /proc /sys /dev /run`), the app's install folder, the session folder (resolved path), the session temp folder, the discovered toolchain folders, and srt's two proxy-bridge sockets. Verified on both releases.
2. Section 4, Environment: spawn commands with the app-built environment only; never merge srt's returned `env` on Linux.
3. Section 4, Writes: set `CLAUDE_CODE_TMPDIR` per tool host to the session temp folder; never rely on srt's shared `/tmp/claude`.
4. Section 4, Approvals: "Allow for this session" is app state applied with `updateConfig`; srt's ask callback is asked every time otherwise.

## Q3: Background servers across commands and from the browser

Result: NO for one sandbox per command; YES with a session runner and explicit forwarding.

Environment: both VMs, `SPIKE_VARIANT=root-deny`, `allowLocalBinding: true`.

Evidence (identical on 24.04 and 26.04):
- One sandbox per command: `python3 -m http.server 18765` started in the background by one command is unreachable from the next command (`curl: (7) Failed to connect to 127.0.0.1 port 18765 … Couldn't connect to server`) and from outside the sandbox (`ECONNREFUSED`). Each srt command gets its own network namespace, so its loopback is private.
- Session runner (`q3/runner.cjs` inside one long-lived srt sandbox; commands are its children): a server started by one runner command is reachable by the next runner command (`200`).
- Forwarding: the host listens on `127.0.0.1:18768` and tunnels each connection over an inherited socketpair (fd 3) that bwrap passes into the sandbox; the runner connects it to `127.0.0.1:18767` inside. A request from outside the sandbox gets `200`. The browser is outside the sandbox, so it reaches the server the same way.
- Killing the runner with SIGKILL ends the whole tree: the runner is gone and no `http.server 18767` survives (only the `pgrep` itself matched), because the sandbox's PID namespace goes down with its init.
- JSON: `results/{u24,u26}/q3-ubuntu.json`.

Design consequence (Section 4):
1. Commands of one root session run inside one long-lived sandboxed runner, not one sandbox per command, whenever background processes are involved. A runner per root also matches the per-root tool host model from Q2.
2. Browser access to a session's local server goes through explicit forwarding: the tool host listens on a loopback port for a port the user approved and tunnels it into the runner over an inherited descriptor. The runner never exposes a port on the host's loopback by itself.
3. Killing the runner is a reliable way to end a session's process tree (confirmed again in Q8).

## Q4: Installed browsers, launch flags, downloads and the network guard

Result: PARTIAL. Launch, sandboxing, downloads and file inputs are YES. The network guard is YES only with the pinning proxy; per-session private-network grants are NOT enforceable in a shared profile.

Environment: both VMs, Google Chrome 154.0.8037.92 and Microsoft Edge 154.0.4258.37 (.deb), launched from the browser host (`utilityProcess`) by `playwright-core` 1.63.0 with `launchPersistentContext(…, { chromiumSandbox: true })`. Brave and Vivaldi were not installed (untested). Ubuntu's Chromium is the Snap (`/snap/bin/chromium` → `/usr/bin/snap`).

Evidence (same on 24.04 and 26.04 unless noted):

| Check | Chrome 154 | Edge 154 |
|---|---|---|
| launched over `--remote-debugging-pipe`, no `--remote-debugging-port` | yes | yes |
| no `--no-sandbox`; renderers `Seccomp: 2` | yes | yes |
| second launch on the same profile (Review Focus 4) | clean error: "Opening in existing browser session" | same |
| route guard, session B (no grant) → loopback canary | 0 hits | 0 hits |
| route guard, session A (granted `http://127.0.0.1:18081`) | 0 hits (Chrome's Local Network Access blocks it) | same |
| same, with `--disable-features=LocalNetworkAccessChecks` (24.04) | A: 10 request types reach it; **B leaks via SharedWorker and via a public→loopback redirect** | same |
| pinning proxy (`--proxy-server` + `--proxy-bypass-list=<-loopback>`), no grants | 0 hits; proxy refused fetch, XHR, img, iframe, prefetch, beacon, EventSource, workers, shared workers, WebSocket CONNECT, `localhost`, and the httpbin redirect target | same |
| `file:///etc/hostname` via `goto` | blocked (`ERR_BLOCKED_BY_CLIENT`) | blocked |
| `chrome://version` / `view-source:` via `goto` | not stopped by the route guard (commit late, then load) | 26.04: navigated to `edge://version/` and to `https://example.com/` |
| `window.open('file:///etc/hostname')` from the page | no popup | no popup |
| download named `../../escape.txt` | browser suggests `_.._escape.txt`; saved inside the folder; nothing outside | same |
| `<input type=file>` click with a `filechooser` listener | intercepted, no native dialog | same |
| service workers allowed | no canary hits | no canary hits |

Other observations:
- Chrome rewrites its process title: `/proc/<pid>/cmdline` of the browser process is one space-joined string, so flag checks must split it.
- Playwright's default arguments weaken the browser a person uses, among them `--disable-popup-blocking`, `--disable-client-side-phishing-detection`, `--disable-component-update` (no Safe Browsing or CRL updates), `--disable-background-networking`, `--password-store=basic` with `--use-mock-keychain` (saved logins without the OS keyring), and `HttpsUpgrades` in `--disable-features`.
- A download of a response fulfilled by Playwright's route is cancelled by Chromium (`download.saveAs: canceled`); downloads must be real responses, not intercepted ones.
- Route interception attributes requests to a session for pages and dedicated workers, but not for WebSockets (no page) and not for redirect targets or SharedWorker requests.
- JSON: `results/{u24,u26}/q4-ubuntu.json`, `results/u24/q4lna-ubuntu.json`.

Design consequence (Section 5):
1. Supported matrix for the first release: Google Chrome and Microsoft Edge `.deb` builds, verified at 154. Brave and Vivaldi stay "detected, unverified" until tested. Detection checks `/snap/bin/chromium` and treats any path resolving to `/usr/bin/snap` as unsupported.
2. Launch through `ignoreDefaultArgs` so the agent browser keeps the protections a person expects: drop at least `--disable-popup-blocking`, `--disable-client-side-phishing-detection`, `--disable-component-update`, `--disable-background-networking`, `--password-store=basic` and `--use-mock-keychain`, and the `HttpsUpgrades` disable. Needs its own verification when the browser host is built.
3. Network enforcement uses the pinning proxy with `--proxy-bypass-list=<-loopback>` as the boundary (it resolves once and connects to that address, so it also stops DNS rebinding), keeps Chrome's Local Network Access checks on, and keeps `serviceWorkers: 'block'`. The route guard stays only for fixture-style interception and logging, never as the security boundary.
4. Private-network access cannot be scoped to one session in a shared profile: redirects, SharedWorkers and WebSockets escape per-session attribution. The first release offers no private-network access in the agent browser except the session's own forwarded ports (Q3), which the proxy admits by exact loopback port. Per-session grants, if wanted later, need a separate browser (profile) per granted session.
5. The `navigate` operation itself rejects anything but `http:`/`https:`; the route guard does not stop `chrome://`, `edge://` or `view-source:`. Page-initiated navigation to those schemes and to `file:` is already refused by the browser.
6. Downloads: keep the browser's suggested name, take its basename, save inside the folder with the same conflict rules as file writes; never intercept a response that may become a download.

## Q5: Worker recovery without repeating an effect

Result: NO today (replay neither resumes nor reports the unfinished call); YES for the Section 2 protocol, whose executable model passes every fault case.

Environment: workstation, surogates venv (`/work/surogates/.venv/bin/python`), pytest.

Evidence, current replay:
- `q5/test_replay_unanswered_tool_call.py` (passes): for an event log `user.message → llm.request → llm.response(tool_calls=[call_1]) → tool.call(call_1)` with no `tool.result`, `AgentHarness._rebuild_messages` (`surogates/harness/loop_context_replay.py:194`) returns a history ending in the assistant's tool call with no tool message.
- Wake then rebuilds from the full event log (`surogates/harness/loop.py:1175`). Before every LLM request, `sanitize_tool_pairs` (`surogates/harness/llm_call.py:600`) inserts a stub tool result for each unmatched call (`surogates/harness/sanitize.py:146-168`): `"[Result unavailable — see context summary above]"`. The call is never re-dispatched; the model decides whether to retry, without knowing whether the effect happened.
- The harness cursor advances only through `advance_harness_cursor` (`loop.py:4500`), which is already fenced: it takes the lease row `FOR UPDATE` and refuses when `lease_token` differs (`surogates/session/store.py:2144-2152`).

Evidence, protocol model (`q5/journal_model.py`, `q5/test_journal_model.py`, 11 passed; written test-first, collection failed before the model existed):
- happy path runs once; a lost Redis nudge is recovered by the heartbeat reconcile; a laptop crash after *received* runs the operation exactly once after reconnect;
- a crash after *started* (before or after the effect) reports `{"interrupted": true, "outcome": "unknown"}` and never reruns (effects 0 and 1 respectively);
- a crash after *result* returns the stored result (effect once);
- a new worker after the result was committed reads it without re-dispatching;
- a worker holding a stale lease token can neither dispatch nor commit;
- the same operation ID with a changed payload is a protocol error;
- a cancellation persisted while offline is applied before pending work, and the operation never runs;
- a delayed retry after the payload was reclaimed returns the compact record, not a rerun.
- Mutation check: removing the *started → interrupted* rule turns exactly the two interrupted-outcome tests red.

Design consequence (Section 2):
1. Durable invocation replay plugs into `AgentHarness.wake` between `_rebuild_messages` (`loop.py:1175`) and the first LLM call: for every unanswered tool call of a desktop session that has `device_operations` rows, resume the recorded operation sequence (same operation IDs, read committed results) and emit its `tool.result` before the LLM runs. Otherwise `sanitize_tool_pairs` would stub it as "unavailable" and the model could redo a completed effect.
2. Lease fencing reuses the existing `lease_token` check pattern of `advance_harness_cursor` for journal dispatch and result commit.
3. No correction to the Section 2 state machine was needed; the model encodes it as written.

## Q7: The file-operation boundary under races

Result: PARTIAL. The candidate primitive holds against symlink and rename races, special files and magic links. Pre-existing hard links and protected files created after a sandbox starts are real gaps.

Environment: both VMs, `root-deny` policy. Candidate A: file-operation helper (`q7/fsop.cjs`, Electron as Node) running inside the session's srt sandbox, opening with `O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC` and refusing non-regular files and `nlink > 1` after `fstat`. Baseline B: `realpath` check, then an ordinary write, in the unsandboxed host.

Evidence (identical on 24.04 and 26.04):

| Case | Result |
|---|---|
| race: a sandboxed command flips `sub/` between a directory and a symlink to `~/.config/…`, 5000 writes each | A: 0 files outside. B: 0 files outside (race not reproduced; not evidence that B is safe) |
| sandboxed `ln ~/.nvm/nvm.sh ./hl` | fails: `Invalid cross-device link` (bind mounts are separate mounts) |
| helper write through a hard link the user made to `~/Documents/q7-outside.txt` | refused: `EMLINK` |
| **sandboxed command `echo … >> hl-user` through that hard link** | **succeeds; the outside file now contains `pwned-by-command`** (Review Focus 1) |
| helper read of a FIFO | refused `ENOTREG` in ≤ 1 ms, no hang |
| helper read through `z -> /dev/zero` | refused `ELOOP` |
| helper read through `pr -> /proc/self/root` to a denied file | `ENOENT` (denied paths do not exist in the sandbox) |
| `.git/config` in the folder, host started in `~/spike` | helper write succeeds; command write succeeds |
| same, host `chdir`ed to the session folder before srt runs | command write fails `Read-only file system`; the long-lived helper (sandboxed before the file existed) still writes it |

Finding on mandatory denies: srt computes its mandatory deny list (`.bashrc`, `.gitconfig`, `.git/config`, `.git/hooks`, …) from the host process's current directory and only for files that exist when a command is wrapped (the debug log showed `Skipping non-existent deny path … /home/spike/spike/.bashrc`). A sandbox that lives longer than one command does not protect files created after it started.

JSON: `results/{u24,u26}/q7-ubuntu.json` (host in the folder), `q7nochdir-ubuntu.json` (host in `~/spike`).

Design consequence (Section 4):
1. Adopt candidate A for raw file operations: the helper runs inside the session's srt sandbox with `O_NOFOLLOW`, `fstat`-based regular-file and `nlink` checks. The mount namespace, not a `realpath` check, is the containment boundary. No native `openat2` helper is needed for the first release.
2. Each per-root tool host `chdir`s to the session folder before initialising srt, so srt's mandatory denies apply to the folder.
3. The raw file API enforces the protected-file list itself (`.git/config`, `.git/hooks/*`, `.bashrc`, … plus the harness's `.env*` list) on every operation, because srt's denies are fixed at sandbox launch. For long-lived sandboxes (the Q3 session runner), the runner is restarted when a protected path appears in the folder, or commands that do not need background processes run in fresh per-command sandboxes. This choice goes to implementation planning with both options stated.
4. Hard links: at binding time the app scans the folder for regular files with `nlink > 1` and refuses to bind until the user removes them or confirms in the native prompt that those files may be changed; the helper refuses them always. Hard links created during a session by processes outside the sandbox remain a documented residual risk.
