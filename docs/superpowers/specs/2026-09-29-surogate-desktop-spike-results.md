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
