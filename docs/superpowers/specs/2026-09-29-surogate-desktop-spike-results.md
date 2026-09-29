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

<!-- One section per question follows, each with: Result, Environment, Evidence, Design consequence. -->
