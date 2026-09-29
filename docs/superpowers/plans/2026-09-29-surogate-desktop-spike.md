# Surogate Desktop First Spike Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Answer the eight platform and recovery questions in Section 10 of the design with a written result and evidence each, before any implementation planning.

**Architecture:** Throwaway probes under `spikes/desktop/` on a spike branch. One Electron app (`main.cjs`) dispatches to a probe per question; shared helpers in `spikes/desktop/lib/` fork `utilityProcess` tool hosts that initialise `srt` and run sandboxed commands. Probes run on clean Ubuntu 24.04 and 26.04 VMs (the oldest and newest supported LTS releases), except the sign-in probe (Q6), which runs on the developer workstation against the local dev stack. Each probe writes a JSON result under `~/surogate-spike-results/`, and each task records its conclusion in one results document.

**Tech Stack:** Electron (`utilityProcess`, `BrowserWindow`), `@anthropic-ai/sandbox-runtime` (`SandboxManager`), `playwright-core`, Node 22, libvirt/`virt-install` with cloud images, Python 3.12 + pytest (surogates venv), FastAPI (surogates api), React (surogates web).

**Spec:** `docs/superpowers/specs/2026-09-29-surogate-desktop-design.md` (Section 10 table, plus Sections 1–5 and 7 for the behaviour each probe checks).

## Global Constraints

- Probe code is throwaway. It lives on branch `spike/desktop-platform` under `spikes/desktop/` and is never merged. Only the results document and design edits move to `docs/desktop-client-design`.
- Record the exact Electron, `srt` and `playwright-core` versions. Install them with `npm install --save-exact`.
- Never disable a sandbox: no `--no-sandbox`, no `chromiumSandbox: false`, no `enableWeakerNestedSandbox`, no `ELECTRON_DISABLE_SANDBOX`.
- Test machines: clean Ubuntu 24.04 LTS and 26.04 LTS VMs (x64), both with `kernel.apparmor_restrict_unprivileged_userns = 1`. Only Ubuntu LTS releases from 24.04 on are supported; no other distribution or interim release is tested. Q6 runs on the workstation against the local dev stack only, never against PROD.
- Each question's result states YES, NO or PARTIAL, the versions, the commands run, output excerpts, the JSON result path, and the consequence for the design.
- Python probes run with `/work/surogates/.venv/bin/python`, never `uv run`.
- Commits follow Conventional Commits and carry no `Co-Authored-By` trailer.

## Review Focus

1. **A hard link inside the chosen folder to a file outside it.** File tools must refuse to write through it. Record whether a sandboxed command can modify the outside file this way (Task 8).
2. **A folder path with spaces and non-ASCII characters** (`B My Files ü`). Policy, working directory and commands must behave exactly as for a plain path (Task 3).
3. **A folder that is a symlink to another mount** (`C-link` → `/mnt/spike-data/C`). A policy built from the resolved path must work, and deny-by-default reads must still cover other mounts (Task 3).
4. **An agent browser profile that is already open.** A second launch on the same profile folder must fail cleanly and leave the profile intact (Task 5).
5. **No systemd user manager in the session.** Process-tree cleanup needs a defined behaviour; record whether `systemctl --user` works (Task 9).

---

## File Structure

All paths are relative to `/work/surogates` on branch `spike/desktop-platform`.

| Path | Responsibility |
|---|---|
| `spikes/desktop/package.json` | exact pins for `electron`, `@anthropic-ai/sandbox-runtime`, `playwright-core` |
| `spikes/desktop/main.cjs` | Electron entry; runs the probe named on the command line (`q1` … `q8`) |
| `spikes/desktop/stage.sh` | copies the app into `~/.local/share/surogate/versions/spike/` with the binary renamed `surogate`, matching the design's install layout |
| `spikes/desktop/lib/results.cjs` | writes `~/surogate-spike-results/<question>-<hostname>.json` |
| `spikes/desktop/lib/policy.cjs` | builds the `srt` policy under test (Section 4 rules) |
| `spikes/desktop/lib/env.cjs` | builds the command environment and per-session temp folder |
| `spikes/desktop/lib/host.cjs` | generic tool host (`utilityProcess`): initialises `srt`, runs commands, relays the network ask callback |
| `spikes/desktop/lib/hosts.cjs` | main-process side: fork a host, send commands, answer asks |
| `spikes/desktop/vm/create.sh` | creates a libvirt VM from a cloud image with cloud-init |
| `spikes/desktop/vm/provision-ubuntu.sh` | installs probe prerequisites inside either VM |
| `spikes/desktop/q1/…` … `spikes/desktop/q8/…` | one folder per question |
| `docs/superpowers/specs/2026-09-29-surogate-desktop-spike-results.md` | the written results, one section per question |

Q5 is Python and runs inside the surogates repo's test environment. Q6 adds throwaway files to the surogates api and web on the spike branch.

---

### Task 1: Spike workspace, shared helpers and VMs

**Files:**
- Create: `spikes/desktop/package.json`, `spikes/desktop/main.cjs`, `spikes/desktop/stage.sh`, `spikes/desktop/lib/results.cjs`, `spikes/desktop/lib/policy.cjs`, `spikes/desktop/lib/env.cjs`, `spikes/desktop/lib/host.cjs`, `spikes/desktop/lib/hosts.cjs`, `spikes/desktop/vm/create.sh`, `spikes/desktop/vm/provision-ubuntu.sh`, `spikes/desktop/.gitignore`
- Create: `docs/superpowers/specs/2026-09-29-surogate-desktop-spike-results.md`

**Interfaces:**
- Produces:
  - `writeResult(question: string, data: object): string` (returns the file path)
  - `buildPolicy({folder, tmp, allowedDomains, variant = 'root-deny', extraRead = []}): SandboxRuntimeConfig`
  - `sessionTmp(name: string): string`
  - `buildEnv({tmp}): Record<string,string>`
  - `forkHost(name, {policy, onAsk?}): {child, ready: Promise<msg>, run(command, {cwd, env, timeoutMs?, background?}): Promise<msg>}`
  - A `run` result message: `{type:'result', id, code, signal, stdout, stderr, argv}`. A background start: `{type:'started', id, pid, argv}`. A failure: `{type:'error', id?, message}`.
  - `main.cjs` runs `require('./<qN>/main.cjs').run({arg, argv})` for a command-line argument matching `/^q\d(-[a-z]+)?$/` (`arg` is the matched argument, such as `q8-recover`; `argv` is `process.argv`).

- [ ] **Step 1: Create the spike branch**

```bash
cd /work/surogates
git checkout docs/desktop-client-design
git checkout -b spike/desktop-platform
mkdir -p spikes/desktop/lib spikes/desktop/vm
```

- [ ] **Step 2: Pin the three packages**

```bash
cd /work/surogates/spikes/desktop
cat > package.json <<'EOF'
{
  "name": "surogate-desktop-spike",
  "private": true,
  "main": "main.cjs"
}
EOF
npm install --save-exact electron@latest @anthropic-ai/sandbox-runtime@latest playwright-core@latest
printf 'node_modules/\n' > .gitignore
node -e "const p=require('./package.json');console.log(p.dependencies)"
```

Expected: three dependencies with exact versions (no `^`). Copy them into the results document in Step 9.

- [ ] **Step 3: Write `lib/results.cjs`, `lib/policy.cjs` and `lib/env.cjs`**

`spikes/desktop/lib/results.cjs`:

```js
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const DIR = path.join(os.homedir(), 'surogate-spike-results');

function writeResult(question, data) {
  fs.mkdirSync(DIR, { recursive: true });
  const file = path.join(DIR, `${question}-${os.hostname()}.json`);
  const body = { question, host: os.hostname(), at: new Date().toISOString(), ...data };
  fs.writeFileSync(file, JSON.stringify(body, null, 2));
  return file;
}

module.exports = { writeResult, DIR };
```

`spikes/desktop/lib/policy.cjs` holds the policy under test, taken from the design's Section 4:

```js
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

// System runtime paths installed tools need. Q2 records what breaks without each.
const SYSTEM_READ = ['/usr', '/bin', '/sbin', '/lib', '/lib64', '/etc', '/opt', '/proc', '/sys', '/dev', '/run'];

// Specific toolchain folders only; never a whole tree like ~/.cargo that can hold credentials.
function toolchainDirs() {
  const h = os.homedir();
  return ['.nvm', '.pyenv', '.rustup', '.cargo/bin', '.local/bin', '.local/lib', 'go', '.bun', '.deno', '.sdkman']
    .map((d) => path.join(h, d))
    .filter((p) => fs.existsSync(p));
}

// variant 'root-deny': deny every read, then re-admit. 'home-deny': deny the home
// folder and other user-data mounts only. Q2 records which one srt accepts.
function buildPolicy({ folder, tmp, allowedDomains, variant = 'root-deny', extraRead = [] }) {
  const root = fs.realpathSync(folder);
  const denyRead = variant === 'root-deny'
    ? ['/']
    : [os.homedir(), '/mnt', '/media', '/srv', '/tmp', '/var/tmp'];
  return {
    network: { allowedDomains, deniedDomains: [], allowLocalBinding: true },
    filesystem: {
      denyRead,
      allowRead: [...SYSTEM_READ, root, tmp, ...toolchainDirs(), ...extraRead],
      allowWrite: [root, tmp],
      denyWrite: [],
    },
  };
}

module.exports = { buildPolicy, SYSTEM_READ, toolchainDirs };
```

`spikes/desktop/lib/env.cjs`:

```js
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

let cachedPath;

// PATH from the user's interactive login shell, so nvm and pyenv shims resolve.
function loginPath() {
  if (cachedPath) return cachedPath;
  const out = execFileSync(process.env.SHELL || '/bin/bash', ['-lic', 'printf "__P__%s__P__" "$PATH"'], {
    encoding: 'utf8', timeout: 10000, stdio: ['ignore', 'pipe', 'ignore'],
  });
  cachedPath = /__P__(.*)__P__/.exec(out)[1];
  return cachedPath;
}

// Per-session temp folder in the design's location.
function sessionTmp(name) {
  const dir = path.join(os.homedir(), '.local/share/surogate/tmp', `${name}-${process.pid}`);
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

function buildEnv({ tmp }) {
  for (const d of ['cache', 'npm', 'pip', 'uv']) fs.mkdirSync(path.join(tmp, d), { recursive: true });
  return {
    HOME: os.homedir(),
    LANG: process.env.LANG || 'C.UTF-8',
    TMPDIR: tmp,
    XDG_CACHE_HOME: path.join(tmp, 'cache'),
    npm_config_cache: path.join(tmp, 'npm'),
    PIP_CACHE_DIR: path.join(tmp, 'pip'),
    UV_CACHE_DIR: path.join(tmp, 'uv'),
    PATH: loginPath(),
  };
}

module.exports = { buildEnv, loginPath, sessionTmp };
```

- [ ] **Step 4: Write the tool host pair `lib/host.cjs` and `lib/hosts.cjs`**

`spikes/desktop/lib/host.cjs` runs inside a `utilityProcess`. One process holds one `SandboxManager`, because the runtime keeps its configuration and proxies in module globals:

```js
const { spawn } = require('node:child_process');

const port = process.parentPort;
const pendingAsks = new Map();
let SM;
let askSeq = 0;

const send = (msg) => port.postMessage(msg);

function ask({ host, port: p }) {
  const id = ++askSeq;
  send({ type: 'ask', id, host, port: p });
  return new Promise((resolve) => pendingAsks.set(id, resolve));
}

async function run({ id, command, cwd, env, timeoutMs = 180000, background = false }) {
  const { argv, env: sbEnv } = await SM.wrapWithSandboxArgv(command, undefined, undefined, undefined, cwd, { commandId: id });
  const child = spawn(argv[0], argv.slice(1), { cwd, env: { ...env, ...sbEnv }, stdio: ['ignore', 'pipe', 'pipe'] });
  if (background) {
    send({ type: 'started', id, pid: child.pid, argv });
    return;
  }
  let out = '';
  let err = '';
  child.stdout.on('data', (d) => { out += d; });
  child.stderr.on('data', (d) => { err += d; });
  const timer = setTimeout(() => child.kill('SIGKILL'), timeoutMs);
  child.on('close', (code, signal) => {
    clearTimeout(timer);
    send({
      type: 'result', id, code, signal, argv,
      stdout: out.slice(-20000),
      stderr: SM.annotateStderrWithSandboxFailures(command, err).slice(-20000),
    });
  });
}

port.on('message', async ({ data }) => {
  try {
    if (data.type === 'init') {
      ({ SandboxManager: SM } = await import('@anthropic-ai/sandbox-runtime'));
      await SM.initialize(data.policy, ask);
      send({
        type: 'ready', pid: process.pid,
        deps: SM.checkDependencies ? SM.checkDependencies() : null,
        globWarnings: SM.getLinuxGlobPatternWarnings ? SM.getLinuxGlobPatternWarnings() : [],
      });
    } else if (data.type === 'answer') {
      pendingAsks.get(data.id)?.(data.allow);
      pendingAsks.delete(data.id);
    } else if (data.type === 'run') {
      await run(data);
    }
  } catch (e) {
    send({ type: 'error', id: data.id, message: String((e && e.stack) || e) });
  }
});
```

`spikes/desktop/lib/hosts.cjs`:

```js
const { utilityProcess } = require('electron');
const path = require('node:path');

function forkHost(name, { policy, onAsk }) {
  const child = utilityProcess.fork(path.join(__dirname, 'host.cjs'), [], { serviceName: name, stdio: 'inherit' });
  const waiters = new Map();
  let seq = 0;
  let readyResolve;
  const ready = new Promise((r) => { readyResolve = r; });
  child.on('message', (msg) => {
    if (msg.type === 'ready' || (msg.type === 'error' && !msg.id)) readyResolve(msg);
    else if (msg.type === 'ask') {
      Promise.resolve(onAsk ? onAsk(msg) : false)
        .then((allow) => child.postMessage({ type: 'answer', id: msg.id, allow }));
    } else if (waiters.has(msg.id)) {
      waiters.get(msg.id)(msg);
      waiters.delete(msg.id);
    }
  });
  child.postMessage({ type: 'init', policy });
  return {
    child,
    ready,
    run(command, opts = {}) {
      const id = `${name}-${++seq}`;
      child.postMessage({ type: 'run', id, command, ...opts });
      return new Promise((r) => waiters.set(id, r));
    },
  };
}

module.exports = { forkHost };
```

- [ ] **Step 5: Write `main.cjs` and `stage.sh`**

`spikes/desktop/main.cjs`:

```js
const { app } = require('electron');

const arg = process.argv.find((a) => /^q\d(-[a-z]+)?$/.test(a));

app.whenReady().then(async () => {
  try {
    if (!arg) throw new Error('usage: surogate q1|q2|…|q8[-mode]');
    await require(`./${arg.split('-')[0]}/main.cjs`).run({ arg, argv: process.argv });
  } catch (e) {
    console.error(e);
    process.exitCode = 1;
  } finally {
    app.quit();   // q8's start mode never resolves, so it stays alive until killed
  }
});
```

`spikes/desktop/stage.sh`, which installs into the design's layout (Section 9) so AppArmor sees a realistic path:

```bash
#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.local/share/surogate/versions/spike"
rm -rf "$DEST"
mkdir -p "$DEST/resources/app"
cp -a "$HERE/node_modules/electron/dist/." "$DEST/"
mv "$DEST/electron" "$DEST/surogate"
rsync -a --exclude node_modules/electron --exclude .git "$HERE/" "$DEST/resources/app/"
echo "$DEST/surogate"
```

```bash
chmod +x /work/surogates/spikes/desktop/stage.sh
```

- [ ] **Step 6: Write the VM scripts**

`spikes/desktop/vm/create.sh`:

```bash
#!/usr/bin/env bash
# usage: create.sh <vm-name> <cloud-image-url>
set -euo pipefail
NAME=$1
IMAGE_URL=$2
KEY="${SSH_PUBKEY:-$HOME/.ssh/id_ed25519.pub}"
IMAGES=/var/lib/libvirt/images
BASE="$IMAGES/$(basename "$IMAGE_URL")"
[ -f "$BASE" ] || sudo curl -fL -o "$BASE" "$IMAGE_URL"
sudo qemu-img create -f qcow2 -F qcow2 -b "$BASE" "$IMAGES/$NAME.qcow2" 30G
USERDATA=$(mktemp)
cat > "$USERDATA" <<EOF
#cloud-config
users:
  - name: spike
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - $(cat "$KEY")
EOF
virt-install --connect qemu:///system --name "$NAME" --memory 6144 --vcpus 4 \
  --disk "$IMAGES/$NAME.qcow2",bus=virtio --import --os-variant linux2022 \
  --network network=default --cloud-init user-data="$USERDATA" \
  --noautoconsole --graphics none
echo "wait ~60s, then: virsh -c qemu:///system domifaddr $NAME"
```

`spikes/desktop/vm/provision-ubuntu.sh` (runs inside either VM; the `t64` package names exist on both 24.04 and 26.04):

```bash
#!/usr/bin/env bash
set -euo pipefail
sudo apt-get update
sudo apt-get install -y bubblewrap socat ripgrep xvfb xauth curl git jq rsync python3-venv \
  libnss3-tools libgtk-3-0t64 libnss3 libxss1 libasound2t64 libgbm1 dbus-user-session
curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt-get install -y /tmp/chrome.deb
curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | sudo gpg --dearmor -o /usr/share/keyrings/microsoft-edge.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-edge.gpg] https://packages.microsoft.com/repos/edge stable main" \
  | sudo tee /etc/apt/sources.list.d/microsoft-edge.list
sudo apt-get update && sudo apt-get install -y microsoft-edge-stable
sudo snap install chromium   # Ubuntu's Chromium is a snap: used to check it is reported unsupported
curl -fsSL -o- https://raw.githubusercontent.com/nvm-sh/nvm/master/install.sh | bash
bash -lic 'nvm install 22 && node --version'
sysctl kernel.apparmor_restrict_unprivileged_userns
```

```bash
chmod +x /work/surogates/spikes/desktop/vm/*.sh
```

- [ ] **Step 7: Create both VMs and copy the spike in**

Run on the workstation. It needs `sudo` for `/var/lib/libvirt/images`. Record each image's build serial (`/etc/cloud/build.info` inside the VM) in the results document.

```bash
cd /work/surogates/spikes/desktop
./vm/create.sh sgd-u24 https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-amd64.img
./vm/create.sh sgd-u26 https://cloud-images.ubuntu.com/releases/26.04/release/ubuntu-26.04-server-cloudimg-amd64.img
sleep 90
for vm in sgd-u24 sgd-u26; do virsh -c qemu:///system domifaddr "$vm"; done
```

Put the two IPs in `U24` and `U26`, then:

```bash
for ip in "$U24" "$U26"; do rsync -a --exclude node_modules ./ "spike@$ip:spike/"; done
for ip in "$U24" "$U26"; do ssh "spike@$ip" 'bash spike/vm/provision-ubuntu.sh && cd spike && bash -lic "npm ci" && ./stage.sh'; done
```

Expected on both VMs: `sysctl` prints `kernel.apparmor_restrict_unprivileged_userns = 1`, and `stage.sh` prints `/home/spike/.local/share/surogate/versions/spike/surogate`. If 26.04 prints `0`, record it and set it to `1` with `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=1`, since the probes must run under the restriction.

- [ ] **Step 8: Smoke-test the host pair on both VMs**

Create `spikes/desktop/q0/main.cjs`:

```js
const os = require('node:os');
const fs = require('node:fs');
const path = require('node:path');
const { forkHost } = require('../lib/hosts.cjs');
const { buildPolicy } = require('../lib/policy.cjs');
const { buildEnv, sessionTmp } = require('../lib/env.cjs');
const { writeResult } = require('../lib/results.cjs');

exports.run = async () => {
  const folder = fs.mkdtempSync(path.join(os.homedir(), 'spike-q0-'));
  const tmp = sessionTmp('q0');
  const host = forkHost('q0', { policy: buildPolicy({ folder, tmp, allowedDomains: [], variant: 'home-deny' }) });
  const ready = await host.ready;
  const echo = await host.run('echo sandboxed-ok && pwd', { cwd: folder, env: buildEnv({ tmp }) });
  console.log('q0 result:', writeResult('q0', { ready, echo }));
};
```

Then:

```bash
for ip in "$U24" "$U26"; do rsync -a --exclude node_modules ./ "spike@$ip:spike/"; ssh "spike@$ip" 'cd spike && ./stage.sh >/dev/null && xvfb-run -a ~/.local/share/surogate/versions/spike/surogate q0; cat ~/surogate-spike-results/q0-*.json'; done
```

Expected on both VMs: Electron may abort before `q0` runs, because the AppArmor profile doesn't exist yet. Q1 covers that; record the exact error text for Task 2. Rerun this step after Task 2 Step 3 installs the profile: `echo.stdout` must then contain `sandboxed-ok`, which confirms the helpers work before the other tasks rely on them.

- [ ] **Step 9: Start the results document**

Create `docs/superpowers/specs/2026-09-29-surogate-desktop-spike-results.md`:

```markdown
# Surogate Desktop First Spike: Results

Spec: `2026-09-29-surogate-desktop-design.md`, Section 10.
Plan: `docs/superpowers/plans/2026-09-29-surogate-desktop-spike.md`.
Probe code: branch `spike/desktop-platform`, `spikes/desktop/` (not merged).

## Environment

| Item | Value |
|---|---|
| Electron | (exact version from package.json) |
| @anthropic-ai/sandbox-runtime | (exact version) |
| playwright-core | (exact version) |
| Ubuntu 24.04 VM | build serial, kernel (uname -r), AppArmor version (apparmor_parser --version), apparmor_restrict_unprivileged_userns = 1 |
| Ubuntu 26.04 VM | build serial, kernel (uname -r), AppArmor version (apparmor_parser --version), apparmor_restrict_unprivileged_userns = 1 |
| Workstation (Q6) | (os-release, kernel) |

## Summary

| # | Question | Result | Design change |
|---|---|---|---|

<!-- One section per question follows, each with: Result, Environment, Evidence, Design consequence. -->
```

Fill in the Environment table with the real values from Steps 2 and 7.

- [ ] **Step 10: Commit**

```bash
cd /work/surogates
git add spikes/desktop docs/superpowers/specs/2026-09-29-surogate-desktop-spike-results.md
git commit -m "chore(spike): scaffold the desktop platform spike"
```

---

### Task 2: Q1 — Electron and `srt` under Ubuntu's AppArmor restriction

**Question:** Do Electron and `srt` start under Ubuntu's AppArmor restriction with their sandboxes enabled? If not: adjust the profile and retest, without disabling sandboxing.

**Files:**
- Create: `spikes/desktop/q1/main.cjs`, `spikes/desktop/q1/surogate-desktop.apparmor`
- Modify: `docs/superpowers/specs/2026-09-29-surogate-desktop-spike-results.md` (add the Q1 section)

**Interfaces:**
- Consumes: `forkHost`, `buildPolicy`, `buildEnv`, `sessionTmp`, `writeResult` (Task 1).
- Produces: the AppArmor profile text later tasks install on the Ubuntu VM before running.

- [ ] **Step 1: Write the probe**

`spikes/desktop/q1/main.cjs`:

```js
const { app, BrowserWindow } = require('electron');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { forkHost } = require('../lib/hosts.cjs');
const { buildPolicy } = require('../lib/policy.cjs');
const { buildEnv, sessionTmp } = require('../lib/env.cjs');
const { writeResult } = require('../lib/results.cjs');

const read = (p) => { try { return fs.readFileSync(p, 'utf8').trim(); } catch (e) { return `unreadable: ${e.code}`; } };
const link = (p) => { try { return fs.readlinkSync(p); } catch (e) { return `unreadable: ${e.code}`; } };
const seccomp = (pid) => /Seccomp:\s+(\d)/.exec(read(`/proc/${pid}/status`))?.[1] ?? null;

exports.run = async () => {
  const folder = fs.mkdtempSync(path.join(os.homedir(), 'spike-q1-'));
  const tmp = sessionTmp('q1');
  const win = new BrowserWindow({ show: false, webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } });
  await win.loadURL('about:blank');
  const host = forkHost('q1', { policy: buildPolicy({ folder, tmp, allowedDomains: [], variant: 'home-deny' }) });
  const ready = await host.ready;
  const cmd = await host.run(
    'id -u; cat /proc/self/attr/current; readlink /proc/self/ns/user; touch ok && echo wrote',
    { cwd: folder, env: buildEnv({ tmp }) },
  );
  const processes = app.getAppMetrics().map((m) => ({
    type: m.type, pid: m.pid,
    apparmor: read(`/proc/${m.pid}/attr/current`),
    userns: link(`/proc/${m.pid}/ns/user`),
    seccomp: seccomp(m.pid),
    cmdline: read(`/proc/${m.pid}/cmdline`).split('\0').filter((a) => a.includes('sandbox')),
  }));
  const file = writeResult('q1', {
    restrictUserns: read('/proc/sys/kernel/apparmor_restrict_unprivileged_userns'),
    mainUserns: link(`/proc/${process.pid}/ns/user`),
    processes, ready, cmd,
    wroteInFolder: fs.existsSync(path.join(folder, 'ok')),
  });
  console.log('q1 result:', file);
};
```

`spikes/desktop/q1/surogate-desktop.apparmor`:

```
abi <abi/4.0>,
include <tunables/global>

profile surogate-desktop /home/*/.local/share/surogate/versions/*/surogate flags=(unconfined) {
  userns,

  include if exists <local/surogate-desktop>
}
```

- [ ] **Step 2: Run on both VMs without the profile and record the failure**

```bash
for ip in "$U24" "$U26"; do
  rsync -a --exclude node_modules spikes/desktop/ "spike@$ip:spike/"
  ssh "spike@$ip" 'cd spike && ./stage.sh >/dev/null; xvfb-run -a ~/.local/share/surogate/versions/spike/surogate q1 2>&1 | tail -20'
  ssh "spike@$ip" 'bwrap --ro-bind / / --unshare-user --unshare-net -- true; echo "bwrap exit=$?"'
done
```

Expected on both: Electron aborts with a sandbox error, such as the SUID helper message or "No usable sandbox". Record the exact text per release, and the plain `bwrap` exit code and message.

- [ ] **Step 3: Install the profile and rerun on both VMs**

```bash
for ip in "$U24" "$U26"; do
  ssh "spike@$ip" 'sudo cp spike/q1/surogate-desktop.apparmor /etc/apparmor.d/surogate-desktop && sudo apparmor_parser -r /etc/apparmor.d/surogate-desktop && sudo aa-status | grep surogate'
  ssh "spike@$ip" 'xvfb-run -a ~/.local/share/surogate/versions/spike/surogate q1 && cat ~/surogate-spike-results/q1-*.json'
done
```

If 26.04's AppArmor rejects `abi <abi/4.0>`, record the parser error and retry with the ABI that release ships (`ls /etc/apparmor.d/abi/`). The install script must write a profile both releases accept.

The result passes when all of these hold:
- the renderer (`type: "Tab"`) has a `userns` different from `mainUserns`, and `seccomp` `"2"`;
- no process's command line contains `--no-sandbox`;
- `cmd.code` is 0 and `cmd.stdout` contains `wrote`;
- `wroteInFolder` is `true`.

Record the AppArmor label printed inside the sandboxed command (the second line of `cmd.stdout`).

- [ ] **Step 4: If `srt`'s `bwrap` still fails inside the app, test a child-profile variant**

Only if Step 3 shows the command failing with a user-namespace error. Add to `surogate-desktop.apparmor`, inside the profile block:

```
  /usr/bin/bwrap rix,
```

Reload with `apparmor_parser -r`, rerun Step 3, and record which variant works on each release. Never add `--no-sandbox` or `enableWeakerNestedSandbox`.

- [ ] **Step 5: Rerun the Task 1 smoke test**

Rerun Task 1 Step 8 on both VMs. `echo.stdout` must contain `sandboxed-ok` before Task 3 starts.

- [ ] **Step 6: Record Q1**

Append to the results document:

```markdown
## Q1: Electron and srt under Ubuntu's AppArmor restriction

Result: YES | NO | PARTIAL
Environment: (both VMs, versions)
Evidence:
- Without the profile: (Electron error text), plain bwrap exit (code, message)
- With the profile: renderer userns (value) ≠ main (value), seccomp 2, command stdout (excerpt), AppArmor label inside the command (value)
- Differences between 24.04 and 26.04: (ABI, parser errors, labels, or "none")
- JSON: ~/surogate-spike-results/q1-sgd-u24.json, q1-sgd-u26.json
Design consequence: (the exact profile the install script must write, or "none")
```

Add a row to the Summary table.

- [ ] **Step 7: Commit**

```bash
git add spikes/desktop/q1 docs/superpowers/specs/2026-09-29-surogate-desktop-spike-results.md
git commit -m "chore(spike): answer Q1 on Electron and srt under AppArmor"
```

---

### Task 3: Q2 — per-root isolation, the read policy and package installs

**Question:** Do separate utility processes isolate concurrent roots' filesystem and network grants, including implicit temp paths? Do package installs work with redirected caches? If not: correct the policy and the process model.

**Files:**
- Create: `spikes/desktop/q2/seed.sh`, `spikes/desktop/q2/main.cjs`
- Modify: the results document (Q2 section)

**Interfaces:**
- Consumes: `forkHost`, `buildPolicy` (both variants), `buildEnv`, `sessionTmp`, `writeResult`.
- Produces: the policy variant that works, which Tasks 4, 8 and 9 use through `variant`.

- [ ] **Step 1: Write the seed script**

It covers Review Focus 2 and 3: a non-ASCII path with spaces, and a symlinked root on another mount.

`spikes/desktop/q2/seed.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
mkdir -p ~/Documents ~/.ssh ~/spike-roots/A "$HOME/spike-roots/B My Files ü"
echo private > ~/Documents/private.txt
echo secret > ~/.ssh/spike_secret && chmod 600 ~/.ssh/spike_secret
echo seedA > ~/spike-roots/A/seed.txt
echo seedB > "$HOME/spike-roots/B My Files ü/seed.txt"
sudo mkdir -p /mnt/spike-data
if ! mountpoint -q /mnt/spike-data; then
  truncate -s 512M ~/spike-data.img && mkfs.ext4 -q -F ~/spike-data.img
  sudo mount -o loop ~/spike-data.img /mnt/spike-data
fi
sudo mkdir -p /mnt/spike-data/C && sudo chown "$USER" /mnt/spike-data/C
echo seedC > /mnt/spike-data/C/seed.txt
ln -sfn /mnt/spike-data/C ~/spike-roots/C-link
```

- [ ] **Step 2: Write the probe**

`spikes/desktop/q2/main.cjs`:

```js
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { forkHost } = require('../lib/hosts.cjs');
const { buildPolicy } = require('../lib/policy.cjs');
const { buildEnv, sessionTmp } = require('../lib/env.cjs');
const { writeResult } = require('../lib/results.cjs');

const base = path.join(os.homedir(), 'spike-roots');
const roots = { A: path.join(base, 'A'), B: path.join(base, 'B My Files ü'), C: path.join(base, 'C-link') };
const DOMAINS = ['registry.npmjs.org', 'pypi.org', 'files.pythonhosted.org'];
const CURL = (url) => `curl -sS -o /dev/null -w '%{http_code}' ${url}`;

exports.run = async () => {
  const asks = [];
  const results = {};
  for (const variant of ['root-deny', 'home-deny']) {
    const tmps = {};
    const hosts = {};
    for (const k of Object.keys(roots)) {
      tmps[k] = sessionTmp(`q2-${k}-${variant}`);
      hosts[k] = forkHost(`q2-${k}-${variant}`, {
        policy: buildPolicy({ folder: roots[k], tmp: tmps[k], allowedDomains: DOMAINS, variant }),
        onAsk: (m) => { asks.push({ variant, root: k, host: m.host, port: m.port }); return k === 'A'; },
      });
    }
    const readies = Object.fromEntries(await Promise.all(Object.entries(hosts).map(async ([k, h]) => [k, await h.ready])));
    const run = (k, cmd) => hosts[k].run(cmd, { cwd: fs.realpathSync(roots[k]), env: buildEnv({ tmp: tmps[k] }) });
    const other = (k) => (k === 'A' ? roots.B : roots.A);
    const checks = {};
    await Promise.all(Object.keys(roots).map(async (k) => {
      const c = {};
      c.ownWrite = await run(k, 'echo hi > own.txt && cat own.txt');
      c.privateDoc = await run(k, 'cat ~/Documents/private.txt');
      c.sshSecret = await run(k, 'cat ~/.ssh/spike_secret');
      c.otherRead = await run(k, `cat "${other(k)}/seed.txt"`);
      c.otherWrite = await run(k, `echo x > "${other(k)}/pwn.txt"`);
      c.tmpMarker = await run(k, `echo ${k} > /tmp/spike-shared-${k}; ls /tmp | head -20`);
      c.tmpdirWrite = await run(k, 'echo t > "$TMPDIR/t" && cat "$TMPDIR/t"');
      c.tools = await run(k, 'node --version; git --version; python3 -c "print(1)"');
      c.allowedNet = await run(k, CURL('https://registry.npmjs.org/'));
      c.askNet = await run(k, CURL('https://example.com/'));
      c.askNetAgain = await run(k, CURL('https://example.com/'));
      c.npm = await run(k, 'npm init -y >/dev/null && npm install left-pad --no-audit --no-fund && ls "$npm_config_cache"');
      c.pip = await run(k, 'python3 -m venv .venv && .venv/bin/pip install -q six && ls "$PIP_CACHE_DIR"');
      c.dockerSock = await run(k, 'if [ -S /var/run/docker.sock ]; then curl -sS --unix-socket /var/run/docker.sock http://x/version; else echo no-docker-socket; fi');
      c.sessionBus = await run(k, 'busctl --user list 2>&1 | head -3');
      checks[k] = c;
    }));
    checks.crossTmp = {
      AreadsB: await run('A', 'cat /tmp/spike-shared-B'),
      BreadsA: await run('B', 'cat /tmp/spike-shared-A'),
    };
    results[variant] = { readies, checks };
    for (const h of Object.values(hosts)) h.child.kill();
  }
  console.log('q2 result:', writeResult('q2', { roots, asks, results }));
};
```

The three roots run concurrently, each in its own host, while the checks within one root run in order.

- [ ] **Step 3: Run on both VMs**

```bash
for ip in "$U24" "$U26"; do
  rsync -a --exclude node_modules spikes/desktop/ "spike@$ip:spike/"
  ssh "spike@$ip" 'bash spike/q2/seed.sh && cd spike && ./stage.sh >/dev/null && xvfb-run -a ~/.local/share/surogate/versions/spike/surogate q2'
  ssh "spike@$ip" 'jq ".results | map_values(.checks | map_values(if type==\"object\" and has(\"code\") then {code, out: .stdout[0:120], err: .stderr[0:200]} else . end))" ~/surogate-spike-results/q2-*.json'
done
```

The pass criteria for each root in the working variant:

| Check | Pass when |
|---|---|
| `ownWrite` | code 0 and `hi` |
| `privateDoc`, `sshSecret`, `otherRead`, `otherWrite` | non-zero code |
| `crossTmp.AreadsB`, `crossTmp.BreadsA` | non-zero code, meaning `/tmp` is not shared between roots |
| `tmpdirWrite`, `tools`, `npm`, `pip` | code 0, with caches inside that root's temp folder |
| `allowedNet` | `200` |
| `askNet` | A gets `200` after allow; B fails after deny |
| `askNetAgain` | B is asked again, and A's grant doesn't reach B |
| `dockerSock`, `sessionBus` | fail, or report no socket |
| roots B and C | behave exactly like A |

- [ ] **Step 4: Record Q2**

Append the Q2 section with the same four fields as Q1. Name the working variant (`root-deny` or `home-deny`), list every check that failed and why (quoting `stderr`, which carries `srt`'s violation annotations), and include `globWarnings` from `readies`. Design consequence: the exact `allowRead` and `denyRead` lists Section 4 should specify, and anything that has to change in the host-per-root model.

- [ ] **Step 5: Commit**

```bash
git add spikes/desktop/q2 docs/superpowers/specs/2026-09-29-surogate-desktop-spike-results.md
git commit -m "chore(spike): answer Q2 on per-root isolation and read policy"
```

---

### Task 4: Q3 — background servers across commands and from the browser

**Question:** Can later commands and the local browser reach a session's background server through authorized endpoints? If not: design a session runner or explicit forwarding, then retest grants and process cleanup.

**Files:**
- Create: `spikes/desktop/q3/main.cjs`, `spikes/desktop/q3/runner-host.cjs`, `spikes/desktop/q3/runner.cjs`
- Modify: the results document (Q3 section)

**Interfaces:**
- Consumes: `forkHost`, `buildPolicy` (working variant from Q2 via `SPIKE_VARIANT`), `buildEnv`, `sessionTmp`, `writeResult`.
- Produces: `runner.cjs`'s line protocol. It reads `{id, cmd, background?}` or `{id, forward: {port}}` and writes `{id, code, out, err}`, `{id, pid}` or `{id, forwarding: true}`.

- [ ] **Step 1: Write the runner, which runs inside one long-lived sandbox**

`spikes/desktop/q3/runner.cjs`:

```js
const { spawn } = require('node:child_process');
const net = require('node:net');

const reply = (m) => process.stdout.write(`${JSON.stringify(m)}\n`);

function handle(msg) {
  if (msg.forward) {
    const tunnel = new net.Socket({ fd: 3, readable: true, writable: true });
    const upstream = net.connect(msg.forward.port, '127.0.0.1', () => {
      tunnel.pipe(upstream);
      upstream.pipe(tunnel);
      reply({ id: msg.id, forwarding: true });
    });
    upstream.on('error', (e) => reply({ id: msg.id, error: String(e) }));
    return;
  }
  const child = spawn('bash', ['-c', msg.cmd], { stdio: ['ignore', 'pipe', 'pipe'] });
  if (msg.background) {
    reply({ id: msg.id, pid: child.pid });
    return;
  }
  let out = '';
  let err = '';
  child.stdout.on('data', (d) => { out += d; });
  child.stderr.on('data', (d) => { err += d; });
  child.on('close', (code) => reply({ id: msg.id, code, out, err }));
}

let buf = '';
process.stdin.on('data', (d) => {
  buf += d;
  let i;
  while ((i = buf.indexOf('\n')) >= 0) {
    handle(JSON.parse(buf.slice(0, i)));
    buf = buf.slice(i + 1);
  }
});
```

- [ ] **Step 2: Write the runner host**

`spikes/desktop/q3/runner-host.cjs`:

```js
const { spawn } = require('node:child_process');
const http = require('node:http');
const net = require('node:net');
const path = require('node:path');

const port = process.parentPort;
const statusOf = (url) => new Promise((resolve) => {
  http.get(url, (r) => { r.resume(); resolve(r.statusCode); }).on('error', (e) => resolve(String(e.code || e)));
});
const alive = (pid) => { try { process.kill(pid, 0); return true; } catch { return false; } };

port.on('message', async ({ data }) => {
  const { SandboxManager: SM } = await import('@anthropic-ai/sandbox-runtime');
  await SM.initialize(data.policy);
  const runnerCmd = `ELECTRON_RUN_AS_NODE=1 '${process.execPath}' '${path.join(__dirname, 'runner.cjs')}'`;
  const { argv, env } = await SM.wrapWithSandboxArgv(runnerCmd, undefined, undefined, undefined, data.cwd);
  const child = spawn(argv[0], argv.slice(1), { cwd: data.cwd, env: { ...data.env, ...env }, stdio: ['pipe', 'pipe', 'pipe', 'pipe'] });
  const replies = [];
  let buf = '';
  let stderr = '';
  child.stderr.on('data', (d) => { stderr += d; });
  child.stdout.on('data', (d) => {
    buf += d;
    let i;
    while ((i = buf.indexOf('\n')) >= 0) { replies.push(JSON.parse(buf.slice(0, i))); buf = buf.slice(i + 1); }
  });
  const call = (msg) => new Promise((resolve) => {
    child.stdin.write(`${JSON.stringify(msg)}\n`);
    const started = Date.now();
    const t = setInterval(() => {
      const m = replies.find((r) => r.id === msg.id);
      if (m || Date.now() - started > 15000) { clearInterval(t); resolve(m || { id: msg.id, timeout: true }); }
    }, 50);
  });
  const res = {};
  res.start = await call({ id: 1, cmd: 'python3 -m http.server 18767 --bind 127.0.0.1', background: true });
  await new Promise((r) => setTimeout(r, 1500));
  res.curlSameSandbox = await call({ id: 2, cmd: "curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:18767/" });
  const tunnel = child.stdio[3];
  const server = net.createServer((sock) => { sock.pipe(tunnel); tunnel.pipe(sock); });
  await new Promise((r) => server.listen(18768, '127.0.0.1', r));
  res.forwardSetup = await call({ id: 3, forward: { port: 18767 } });
  res.forwardedFromOutside = await statusOf('http://127.0.0.1:18768/');
  res.runnerPid = child.pid;
  res.serverPidInsideRunner = res.start.pid;
  child.kill('SIGKILL');
  server.close();
  await new Promise((r) => setTimeout(r, 1500));
  res.runnerAliveAfterKill = alive(child.pid);
  res.anyHttpServerLeft = require('node:child_process')
    .execSync("pgrep -af 'http.server 18767' || true", { encoding: 'utf8' }).trim();
  res.stderr = stderr.slice(-4000);
  port.postMessage(res);
});
```

- [ ] **Step 3: Write the probe**

`spikes/desktop/q3/main.cjs`:

```js
const { utilityProcess } = require('electron');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { forkHost } = require('../lib/hosts.cjs');
const { buildPolicy } = require('../lib/policy.cjs');
const { buildEnv, sessionTmp } = require('../lib/env.cjs');
const { writeResult } = require('../lib/results.cjs');

const statusOf = (url) => new Promise((resolve) => {
  http.get(url, (r) => { r.resume(); resolve(r.statusCode); }).on('error', (e) => resolve(String(e.code || e)));
});
const variant = process.env.SPIKE_VARIANT || 'home-deny';
const appDir = path.dirname(process.execPath);

exports.run = async () => {
  const folder = fs.mkdtempSync(path.join(os.homedir(), 'spike-q3-'));
  const tmp = sessionTmp('q3');
  const env = buildEnv({ tmp });
  const policy = buildPolicy({ folder, tmp, allowedDomains: [], variant, extraRead: [appDir] });
  const r = {};

  // Separate commands, each in its own sandbox.
  const host = forkHost('q3', { policy });
  r.ready = await host.ready;
  r.background = await host.run('python3 -m http.server 18765 --bind 127.0.0.1', { cwd: folder, env, background: true });
  await new Promise((res) => setTimeout(res, 1500));
  r.nextCommand = await host.run("curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:18765/", { cwd: folder, env });
  r.fromOutside = await statusOf('http://127.0.0.1:18765/');
  host.child.kill();

  // One long-lived sandbox for the session, plus forwarding over an inherited descriptor.
  const runnerHost = utilityProcess.fork(path.join(__dirname, 'runner-host.cjs'), [], { serviceName: 'q3-runner', stdio: 'inherit' });
  r.runner = await new Promise((resolve) => {
    runnerHost.once('message', resolve);
    runnerHost.postMessage({ policy, cwd: fs.realpathSync(folder), env });
  });
  runnerHost.kill();
  console.log('q3 result:', writeResult('q3', r));
};
```

- [ ] **Step 4: Run on both VMs**

```bash
for ip in "$U24" "$U26"; do
  rsync -a --exclude node_modules spikes/desktop/ "spike@$ip:spike/"
  ssh "spike@$ip" 'cd spike && ./stage.sh >/dev/null && SPIKE_VARIANT=<variant from Q2> xvfb-run -a ~/.local/share/surogate/versions/spike/surogate q3 && jq . ~/surogate-spike-results/q3-*.json'
done
```

Record these findings:
- `nextCommand` and `fromOutside` for separate commands. The expectation is that both fail, because each command has its own network namespace.
- `runner.curlSameSandbox`: `200` means commands inside one sandbox can reach each other.
- `runner.forwardedFromOutside`: `200` means forwarding over the inherited descriptor works.
- `runner.runnerAliveAfterKill` and `runner.anyHttpServerLeft`: whether killing the runner ends the whole tree. That feeds into Q8.

- [ ] **Step 5: Record Q3**

Append the Q3 section. Design consequence: either "each command in its own sandbox is enough" (only if `nextCommand` is 200), or the session-runner model. For the runner model, record:
- one long-lived sandbox per root session;
- commands run as its children;
- browser access through explicit forwarding of a port the user approved;
- the cleanup findings for Q8.

- [ ] **Step 6: Commit**

```bash
git add spikes/desktop/q3 docs/superpowers/specs/2026-09-29-surogate-desktop-spike-results.md
git commit -m "chore(spike): answer Q3 on background servers and forwarding"
```

---

### Task 5: Q4 — installed browsers, launch flags, downloads and the network guard

**Question:** Can the pinned Playwright launch the supported installed browsers over a pipe, with Chromium sandboxing, authorized downloads and complete navigation and network enforcement? If not: narrow browser support, or revise isolation before enabling the feature.

**Files:**
- Create: `spikes/desktop/q4/main.cjs`, `spikes/desktop/q4/browser-host.cjs`, `spikes/desktop/q4/fixture.cjs`, `spikes/desktop/q4/guard-proxy.cjs`
- Modify: the results document (Q4 section)

**Interfaces:**
- Consumes: `writeResult`.
- Produces: `isPrivate(ip: string): boolean` and `guardProxy(log): http.Server` (in `guard-proxy.cjs`), plus `fixture(pathname, token): {status, contentType, body}` (in `fixture.cjs`).

- [ ] **Step 1: Write the fixture served under a fake public origin**

`spikes/desktop/q4/fixture.cjs`. Every request in the page targets the canary on loopback, carrying the session token:

```js
const C = 'http://127.0.0.1:18081';

function page(token) {
  return `<!doctype html><title>fixture</title><body>
<a id="dl" href="/file.txt" download="../../escape.txt">download</a>
<input id="file" type="file">
<iframe src="${C}/iframe?t=${token}"></iframe>
<img src="${C}/img?t=${token}">
<link rel="prefetch" href="${C}/prefetch?t=${token}">
<script>
const T = ${JSON.stringify(token)}, C = ${JSON.stringify(C)};
const q = (p) => C + p + '?t=' + T;
fetch(q('/fetch')).catch(() => {});
fetch('http://localhost:18081/localhost?t=' + T).catch(() => {});
fetch('http://127.0.0.1.nip.io:18081/nip?t=' + T).catch(() => {});
fetch('https://httpbin.org/redirect-to?url=' + encodeURIComponent(q('/redirect'))).catch(() => {});
const x = new XMLHttpRequest(); x.open('GET', q('/xhr')); x.send();
navigator.sendBeacon(q('/beacon'), 'x');
try { new EventSource(q('/sse')); } catch (e) {}
try { new WebSocket('ws://127.0.0.1:18082/ws?t=' + T); } catch (e) {}
new Worker(URL.createObjectURL(new Blob(["fetch('" + q('/worker') + "').catch(() => {})"])));
try { new SharedWorker(URL.createObjectURL(new Blob(["fetch('" + q('/shared') + "').catch(() => {})"]))); } catch (e) {}
if (navigator.serviceWorker) navigator.serviceWorker.register('/sw.js?t=' + T).catch(() => {});
</script>`;
}

function fixture(pathname, token) {
  if (pathname === '/file.txt') return { status: 200, contentType: 'text/plain', body: 'download body' };
  if (pathname === '/sw.js') {
    return { status: 200, contentType: 'text/javascript', body: `self.addEventListener('install', () => fetch('${C}/sw?t=${token}').catch(() => {}));` };
  }
  return { status: 200, contentType: 'text/html', body: page(token) };
}

module.exports = { fixture };
```

- [ ] **Step 2: Write the IP classifier and the pinning guard proxy**

`spikes/desktop/q4/guard-proxy.cjs`. The proxy resolves each host once and connects to that exact address, which is the defence against DNS rebinding that route interception cannot give:

```js
const dns = require('node:dns/promises');
const http = require('node:http');
const net = require('node:net');

function isPrivate(ip) {
  if (net.isIPv4(ip)) {
    const [a, b] = ip.split('.').map(Number);
    return a === 10 || a === 127 || a === 0 || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31)
      || (a === 192 && b === 168) || (a === 100 && b >= 64 && b <= 127);
  }
  const v = ip.toLowerCase();
  if (v.startsWith('::ffff:')) return isPrivate(v.slice(7));
  return v === '::1' || v === '::' || v.startsWith('fc') || v.startsWith('fd') || v.startsWith('fe80');
}

function guardProxy(log) {
  const server = http.createServer(async (req, res) => {
    const u = new URL(req.url);
    const { address } = await dns.lookup(u.hostname);
    if (isPrivate(address)) { log.push({ proxyBlocked: req.url, address }); res.writeHead(403); res.end(); return; }
    const up = http.request({ host: address, port: u.port || 80, path: u.pathname + u.search, method: req.method, headers: { ...req.headers, host: u.host } },
      (r) => { res.writeHead(r.statusCode, r.headers); r.pipe(res); });
    up.on('error', () => { res.writeHead(502); res.end(); });
    req.pipe(up);
  });
  server.on('connect', async (req, sock, head) => {
    const [host, portStr] = req.url.split(':');
    const { address } = await dns.lookup(host);
    if (isPrivate(address)) { log.push({ proxyBlocked: req.url, address }); sock.end('HTTP/1.1 403 Forbidden\r\n\r\n'); return; }
    const up = net.connect(Number(portStr), address, () => {
      sock.write('HTTP/1.1 200 Connection Established\r\n\r\n');
      up.write(head);
      up.pipe(sock);
      sock.pipe(up);
    });
    up.on('error', () => sock.destroy());
  });
  return server;
}

module.exports = { isPrivate, guardProxy };
```

- [ ] **Step 3: Write the browser host**

`spikes/desktop/q4/browser-host.cjs` runs in a `utilityProcess`, as the design's browser host does:

```js
const { execFileSync } = require('node:child_process');
const dns = require('node:dns/promises');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require('playwright-core');
const { fixture } = require('./fixture.cjs');
const { isPrivate, guardProxy } = require('./guard-proxy.cjs');

const ORIGIN = 'https://spike.example';
const CANDIDATES = [['chrome', '/usr/bin/google-chrome'], ['chromium', '/usr/bin/chromium'], ['chromium-browser', '/usr/bin/chromium-browser'],
  ['edge', '/usr/bin/microsoft-edge'], ['brave', '/usr/bin/brave-browser'], ['vivaldi', '/usr/bin/vivaldi']];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function processes(match) {
  return fs.readdirSync('/proc').filter((p) => /^\d+$/.test(p)).map((pid) => {
    try {
      const argv = fs.readFileSync(`/proc/${pid}/cmdline`, 'utf8').split('\0').filter(Boolean);
      const seccomp = /Seccomp:\s+(\d)/.exec(fs.readFileSync(`/proc/${pid}/status`, 'utf8'))?.[1];
      return { pid: Number(pid), argv, seccomp };
    } catch { return null; }
  }).filter((p) => p && p.argv.some((a) => a.includes(match)));
}

function canary() {
  const hits = [];
  const web = http.createServer((req, res) => { hits.push(req.url); res.end('canary'); });
  const ws = net.createServer((s) => { hits.push('ws-connect'); s.destroy(); });
  return {
    hits,
    start: () => Promise.all([new Promise((r) => web.listen(18081, '127.0.0.1', r)), new Promise((r) => ws.listen(18082, '127.0.0.1', r))]),
    stop: () => { web.close(); ws.close(); },
  };
}

// Session-scoped guard on the whole context; popups inherit their opener's session.
async function installGuard(context, sessionOf, grants, log) {
  const decide = async (page, rawUrl) => {
    const url = new URL(rawUrl);
    const session = page ? sessionOf(page) : null;
    if (!['http:', 'https:', 'ws:', 'wss:'].includes(url.protocol)) return { allow: false, why: 'scheme', session };
    let addrs = [];
    try { addrs = (await dns.lookup(url.hostname, { all: true })).map((a) => a.address); } catch { /* unresolvable */ }
    const priv = url.hostname === 'localhost' || addrs.some(isPrivate);
    const granted = session && grants.get(session)?.has(`${url.protocol === 'ws:' ? 'http:' : url.protocol}//${url.host}`);
    return { allow: !priv || Boolean(granted), why: priv ? 'private' : 'public', session, addrs };
  };
  await context.route('**/*', async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    let page = null;
    try { page = req.frame().page(); } catch { /* worker or service-worker request: no frame */ }
    if (url.origin === ORIGIN) {
      const f = fixture(url.pathname, page ? sessionOf(page) : 'none');
      return route.fulfill({ status: f.status, contentType: f.contentType, body: f.body });
    }
    const d = await decide(page, req.url());
    log.push({ kind: 'http', url: req.url(), noFrame: !page, ...d });
    return d.allow ? route.continue() : route.abort('blockedbyclient');
  });
  await context.routeWebSocket(/.*/, async (ws) => {
    const d = await decide(null, ws.url());
    log.push({ kind: 'ws', url: ws.url(), ...d });
    if (d.allow) ws.connectToServer(); else ws.close();
  });
}

async function probeBrowser(name, exe, folder) {
  const out = { name, exe };
  const real = fs.realpathSync(exe);
  if (real.startsWith('/snap/') || real.includes('/flatpak/')) return { ...out, skipped: `unsupported package: ${real}` };
  const profile = fs.mkdtempSync(path.join(os.homedir(), `.config/surogate-spike-profile-${name}-`));
  const downloads = fs.mkdtempSync(path.join(os.tmpdir(), 'spike-dl-'));
  const cn = canary();
  await cn.start();
  const context = await chromium.launchPersistentContext(profile, {
    executablePath: exe, headless: false, chromiumSandbox: true,
    acceptDownloads: true, downloadsPath: downloads, serviceWorkers: 'block',
  });
  try {
    // context.browser() is null for persistent contexts; the spike may run --version.
    out.version = execFileSync(exe, ['--version'], { encoding: 'utf8', timeout: 10000 }).trim();
    const procs = processes(profile);
    const main = procs.find((p) => !p.argv.some((a) => a.startsWith('--type=')));
    out.flags = {
      pipe: main?.argv.includes('--remote-debugging-pipe') ?? false,
      port: main?.argv.some((a) => a.startsWith('--remote-debugging-port')) ?? false,
      noSandbox: procs.some((p) => p.argv.includes('--no-sandbox')),
      rendererSeccomp: procs.filter((p) => p.argv.includes('--type=renderer')).map((p) => p.seccomp),
    };
    try {
      await chromium.launchPersistentContext(profile, { executablePath: exe, headless: false, chromiumSandbox: true, timeout: 15000 });
      out.secondLaunch = 'unexpectedly succeeded';
    } catch (e) { out.secondLaunch = String(e.message).split('\n')[0]; }

    const sessions = new Map();
    const grants = new Map([['A', new Set(['http://127.0.0.1:18081'])]]);
    const log = [];
    await installGuard(context, (p) => sessions.get(p) ?? null, grants, log);
    const pageA = await context.newPage(); sessions.set(pageA, 'A');
    const pageB = await context.newPage(); sessions.set(pageB, 'B');
    await Promise.all([pageA.goto(`${ORIGIN}/`), pageB.goto(`${ORIGIN}/`)]);
    await sleep(6000);
    out.canaryHits = [...cn.hits];
    out.bLeaks = cn.hits.filter((h) => h.includes('t=B'));
    out.guardLogSample = log.slice(0, 60);

    out.schemes = {};
    for (const u of ['file:///etc/hostname', 'chrome://version', 'view-source:https://example.com']) {
      try { await pageB.goto(u, { timeout: 5000 }); out.schemes[u] = `navigated: ${pageB.url()}`; } catch (e) { out.schemes[u] = `blocked: ${String(e.message).split('\n')[0]}`; }
    }
    await pageB.goto(`${ORIGIN}/`);
    const popupWait = context.waitForEvent('page', { timeout: 5000 }).catch(() => null);
    await pageB.evaluate(() => window.open('file:///etc/hostname'));
    const popup = await popupWait;
    out.popupFile = popup ? `popup url: ${popup.url()}` : 'no popup';

    const dl = await Promise.all([pageB.waitForEvent('download', { timeout: 10000 }), pageB.click('#dl')]).then(([d]) => d).catch((e) => e);
    if (dl && dl.suggestedFilename) {
      const raw = dl.suggestedFilename();
      const safe = path.basename(raw).replace(/[\u0000-\u001f]/g, '') || 'download';
      const target = path.join(folder, safe);
      await dl.saveAs(target);
      out.download = { raw, saved: target, inFolder: fs.realpathSync(target).startsWith(fs.realpathSync(folder)) };
    } else out.download = `no download: ${String(dl)}`;

    let chooser = false;
    pageB.on('filechooser', () => { chooser = true; });
    await pageB.click('#file').catch(() => {});
    await sleep(1000);
    out.fileChooserIntercepted = chooser;
  } finally {
    await context.close();
  }

  const swProfile = fs.mkdtempSync(path.join(os.homedir(), `.config/surogate-spike-sw-${name}-`));
  const swLog = [];
  const swCtx = await chromium.launchPersistentContext(swProfile, { executablePath: exe, headless: false, chromiumSandbox: true, serviceWorkers: 'allow' });
  const swSessions = new Map();
  await installGuard(swCtx, (p) => swSessions.get(p) ?? null, new Map(), swLog);
  const swPage = await swCtx.newPage(); swSessions.set(swPage, 'S');
  const before = cn.hits.length;
  await swPage.goto(`${ORIGIN}/`);
  await sleep(6000);
  out.serviceWorkersAllowedHits = cn.hits.slice(before);
  await swCtx.close();

  const proxyLog = [];
  const proxy = guardProxy(proxyLog);
  await new Promise((r) => proxy.listen(18090, '127.0.0.1', r));
  const pxProfile = fs.mkdtempSync(path.join(os.homedir(), `.config/surogate-spike-px-${name}-`));
  const pxCtx = await chromium.launchPersistentContext(pxProfile, {
    executablePath: exe, headless: false, chromiumSandbox: true, serviceWorkers: 'block',
    args: ['--proxy-server=http://127.0.0.1:18090', '--proxy-bypass-list=<-loopback>'],
  });
  await pxCtx.route(`${ORIGIN}/**`, (route) => {
    const f = fixture(new URL(route.request().url()).pathname, 'P');
    return route.fulfill({ status: f.status, contentType: f.contentType, body: f.body });
  });
  const pxPage = await pxCtx.newPage();
  const beforePx = cn.hits.length;
  await pxPage.goto(`${ORIGIN}/`);
  await sleep(6000);
  out.proxyHits = cn.hits.slice(beforePx);
  out.proxyBlocked = proxyLog;
  await pxCtx.close();
  proxy.close();
  cn.stop();
  return out;
}

process.parentPort.on('message', async ({ data }) => {
  const results = [];
  for (const [name, exe] of CANDIDATES) {
    if (!fs.existsSync(exe)) { results.push({ name, exe, missing: true }); continue; }
    try { results.push(await probeBrowser(name, exe, data.folder)); } catch (e) { results.push({ name, exe, error: String((e && e.stack) || e) }); }
  }
  process.parentPort.postMessage(results);
});
```

Popups and requests without a frame (workers, service workers, WebSockets) have no entry in `sessions`, so they get no grants. The probe checks that they are treated as ungranted, which is fail-closed; the results must say whether that blocks legitimate traffic for a granted session.

- [ ] **Step 4: Write the probe**

`spikes/desktop/q4/main.cjs`:

```js
const { utilityProcess } = require('electron');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { writeResult } = require('../lib/results.cjs');

exports.run = async () => {
  const folder = fs.mkdtempSync(path.join(os.homedir(), 'spike-q4-'));
  const hostProc = utilityProcess.fork(path.join(__dirname, 'browser-host.cjs'), [], { serviceName: 'q4-browser', stdio: 'inherit' });
  const results = await new Promise((resolve) => { hostProc.once('message', resolve); hostProc.postMessage({ folder }); });
  hostProc.kill();
  const outside = path.join(os.homedir(), 'escape.txt');
  console.log('q4 result:', writeResult('q4', { folder, escapedFileExists: fs.existsSync(outside), results }));
};
```

- [ ] **Step 5: Run on both VMs**

```bash
for ip in "$U24" "$U26"; do
  rsync -a --exclude node_modules spikes/desktop/ "spike@$ip:spike/"
  ssh "spike@$ip" 'cd spike && ./stage.sh >/dev/null && xvfb-run -a -s "-screen 0 1280x800x24" ~/.local/share/surogate/versions/spike/surogate q4'
  ssh "spike@$ip" 'jq ".escapedFileExists, (.results[] | {name, skipped, missing, error, version, flags, secondLaunch, bLeaks, schemes, popupFile, download, fileChooserIntercepted, serviceWorkersAllowedHits, proxyHits})" ~/surogate-spike-results/q4-*.json'
done
```

The pass criteria for each supported browser:

| Finding | Pass when |
|---|---|
| launch flags | `flags.pipe` is `true`, `flags.port` and `flags.noSandbox` are `false`, and every renderer seccomp value is `"2"` |
| `secondLaunch` | a clean error message (Review Focus 4) |
| `bLeaks` | empty: session B reached no loopback target through any request type |
| `canaryHits` | contains only `t=A` entries |
| `schemes` and `popupFile` | never navigate to `file:`, `chrome:` or `view-source:` |
| `download.inFolder` | `true`, with `escapedFileExists` `false` |
| `fileChooserIntercepted` | `true` |
| `serviceWorkersAllowedHits` | shows whether service workers bypass the guard. Hits here mean `serviceWorkers: 'block'` is mandatory |
| `proxyHits` | shows whether the pinning proxy alone stops loopback traffic when `<-loopback>` is set |
| snap Chromium (Ubuntu) | appears as `skipped` |

- [ ] **Step 6: Record Q4**

Append the Q4 section with a per-browser table: supported, launch flags, sandbox, guard leaks, schemes, downloads.

The design consequence must decide the browser network model. Choose between:
- **Route guard on a shared profile.** It needs session attribution for every request type. Requests without a frame, meaning workers and service workers, must be refused.
- **Pinning proxy plus route guard.** The proxy protects against rebinding; the route guard applies session scope.
- **A separate browser context or profile per session that holds a private-network grant.** This is needed if session attribution fails.

Also record the supported browser matrix, with versions.

- [ ] **Step 7: Commit**

```bash
git add spikes/desktop/q4 docs/superpowers/specs/2026-09-29-surogate-desktop-spike-results.md
git commit -m "chore(spike): answer Q4 on local browser launch and network guard"
```

---

### Task 6: Q5 — worker recovery without repeating an effect

**Question:** Can worker recovery resume the recorded operation sequence without repeating an effect after result-delivery or Redis failures? If not: implement durable invocation replay before shipping the relay.

This task has two parts: a characterization test of what replay does today, and an executable model of the Section 2 protocol under fault injection.

**Files:**
- Create: `spikes/desktop/q5/test_replay_unanswered_tool_call.py`, `spikes/desktop/q5/journal_model.py`, `spikes/desktop/q5/test_journal_model.py`, and empty `spikes/__init__.py`, `spikes/desktop/__init__.py`, `spikes/desktop/q5/__init__.py`
- Modify: the results document (Q5 section)

**Interfaces:**
- Consumes: `AgentHarness._rebuild_messages(self, events, workspace_path=None) -> list[dict]` (`surogates/harness/loop_context_replay.py:194`), `_harness(store)` from `tests/test_wake_stranded_user_message.py:43`, and `EventType` from `surogates/session/events.py`.
- Produces: the classes `Journal`, `Laptop`, `Api`, `Lease`, `Worker` and exception `Crash` in `journal_model.py`, for the results document to reference.

- [ ] **Step 1: Write the characterization test for today's replay**

`spikes/desktop/q5/test_replay_unanswered_tool_call.py`:

```python
"""What replay rebuilds for a tool call that never got a result.

Characterizes current behaviour for the spike; not a regression test.
"""

from __future__ import annotations

from types import SimpleNamespace

from surogates.harness.loop import AgentHarness
from surogates.session.events import EventType
from tests.test_wake_stranded_user_message import _harness


def _ev(event_id: int, etype: EventType, data: dict) -> SimpleNamespace:
    return SimpleNamespace(id=event_id, type=etype.value, data=data)


def test_unanswered_tool_call_is_left_dangling() -> None:
    call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "write_file", "arguments": '{"path": "a.txt", "content": "x"}'},
    }
    events = [
        _ev(1, EventType.USER_MESSAGE, {"content": "write a.txt"}),
        _ev(2, EventType.LLM_REQUEST, {}),
        _ev(3, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": None, "tool_calls": [call]}}),
        _ev(4, EventType.TOOL_CALL, {"name": "write_file", "arguments": call["function"]["arguments"], "tool_call_id": "call_1"}),
    ]
    messages = AgentHarness._rebuild_messages(_harness(store=None), events)
    print(messages)
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["tool_calls"][0]["id"] == "call_1"
    assert not any(m.get("role") == "tool" for m in messages)
```

- [ ] **Step 2: Run it**

```bash
cd /work/surogates && touch spikes/__init__.py spikes/desktop/__init__.py spikes/desktop/q5/__init__.py && PYTHONPATH=/work/surogates /work/surogates/.venv/bin/python -m pytest spikes/desktop/q5/test_replay_unanswered_tool_call.py -s -q
```

The expected result is PASS: the rebuilt history ends with the assistant's tool call and no tool result. If the test fails, the failure output shows what replay does instead, and that output becomes the evidence.

- [ ] **Step 3: Trace what happens after the rebuild**

Read `surogates/harness/loop.py` from the call to `_rebuild_messages` to the first LLM call. Record:
1. the file:line of each step between them;
2. whether any step adds a result for an unanswered tool call or re-dispatches it;
3. where the harness cursor advances relative to tool execution:

```bash
cd /work/surogates && grep -n "_rebuild_messages\|harness_cursor\|advance_cursor\|set_cursor" surogates/harness/loop.py surogates/session/store.py | head -40
```

The recorded answer to "does replay re-run the call, drop it, or send a dangling tool call to the provider" states the file and line numbers it relies on.

- [ ] **Step 4: Write the protocol model**

`spikes/desktop/q5/journal_model.py`:

```python
"""Executable model of the device-operation protocol (design Section 2).

Checks the state machine, not the transport: an effect happens at most once,
a crash before the effect lets recovery perform it exactly once, a crash
between *started* and *result* reports ``interrupted`` and never reruns, a
cancelled operation never runs, and a worker with a stale lease can neither
dispatch nor commit.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


class Crash(Exception):
    """The laptop process died at a named point."""


def digest(request: dict) -> str:
    return hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()


@dataclass
class Journal:
    """Stand-in for the Postgres ``device_operations`` table."""

    rows: dict = field(default_factory=dict)

    def insert(self, op_id: str, request: dict, lease_token: str) -> dict:
        d = digest(request)
        row = self.rows.get(op_id)
        if row is not None:
            if row["digest"] != d:
                raise ValueError("digest mismatch for an existing operation id")
            return row
        row = {"state": "pending", "digest": d, "request": request, "lease_token": lease_token, "result": None}
        self.rows[op_id] = row
        return row

    def cancel(self, op_id: str) -> None:
        row = self.rows[op_id]
        if row["state"] == "pending":
            row["state"] = "cancelled"

    def commit_result(self, op_id: str, result: dict) -> None:
        row = self.rows[op_id]
        if row["state"] == "pending":
            row["state"], row["result"] = "completed", result

    def pending(self) -> list[str]:
        return [k for k, r in self.rows.items() if r["state"] == "pending"]

    def cancelled(self) -> list[str]:
        return [k for k, r in self.rows.items() if r["state"] == "cancelled"]


@dataclass
class Laptop:
    """The tool host's durable local operation record plus a real-effect counter."""

    records: dict = field(default_factory=dict)
    effects: dict = field(default_factory=dict)
    crash_at: str | None = None

    def _maybe_crash(self, point: str) -> None:
        if self.crash_at == point:
            self.crash_at = None
            raise Crash(point)

    def cancel(self, op_id: str, d: str) -> None:
        rec = self.records.get(op_id)
        if rec is None or rec["state"] == "received":
            self.records[op_id] = {"digest": d, "state": "cancelled", "result": {"cancelled": True}}

    def compact(self, op_id: str) -> None:
        rec = self.records[op_id]
        if rec["state"] == "result":
            rec["result"] = {"completed": True, "payload": "reclaimed"}

    def handle(self, op_id: str, request: dict) -> dict:
        d = digest(request)
        rec = self.records.get(op_id)
        if rec is not None:
            if rec["digest"] != d:
                return {"error": "protocol: digest changed"}
            if rec["state"] in ("result", "cancelled"):
                return rec["result"]
            if rec["state"] == "started":
                rec.update(state="result", result={"interrupted": True, "outcome": "unknown"})
                return rec["result"]
        self.records[op_id] = {"digest": d, "state": "received", "result": None}
        self._maybe_crash("after_received")
        self.records[op_id]["state"] = "started"
        self._maybe_crash("after_started")
        self.effects[op_id] = self.effects.get(op_id, 0) + 1
        self._maybe_crash("after_effect")
        result = {"ok": True, "op": op_id}
        self.records[op_id].update(state="result", result=result)
        self._maybe_crash("after_result")
        return result


@dataclass
class Api:
    """Reconciles the journal with the laptop on connect and at each heartbeat."""

    journal: Journal
    laptop: Laptop
    online: bool = True

    def reconcile(self) -> None:
        if not self.online:
            return
        for op_id in self.journal.cancelled():
            self.laptop.cancel(op_id, self.journal.rows[op_id]["digest"])
        for op_id in self.journal.pending():
            row = self.journal.rows[op_id]
            try:
                result = self.laptop.handle(op_id, row["request"])
            except Crash:
                self.online = False
                return
            self.journal.commit_result(op_id, result)


@dataclass
class Lease:
    token: str = "t1"


@dataclass
class Worker:
    journal: Journal
    api: Api
    lease: Lease
    my_token: str
    committed: dict = field(default_factory=dict)

    def _check_lease(self) -> None:
        if self.my_token != self.lease.token:
            raise PermissionError("stale lease")

    def dispatch(self, op_id: str, request: dict) -> None:
        self._check_lease()
        self.journal.insert(op_id, request, self.my_token)
        self.api.reconcile()  # stands in for the Redis nudge, which may be lost

    def commit(self, op_id: str, tool_call_id: str) -> dict | None:
        row = self.journal.rows[op_id]
        if row["state"] != "completed":
            return None
        self._check_lease()
        self.committed[tool_call_id] = row["result"]
        row["state"] = "consumed"
        return row["result"]
```

- [ ] **Step 5: Write the fault-injection tests**

`spikes/desktop/q5/test_journal_model.py`:

```python
from __future__ import annotations

import pytest

from spikes.desktop.q5.journal_model import Api, Journal, Laptop, Lease, Worker, digest

REQ = {"op": "write", "path": "a.txt", "bytes": "x"}
OP = "call_1:1"


def _world(online: bool = True):
    journal, laptop, lease = Journal(), Laptop(), Lease()
    api = Api(journal, laptop, online=online)
    return journal, laptop, api, lease, Worker(journal, api, lease, my_token=lease.token)


def test_happy_path_runs_once() -> None:
    _, laptop, _, _, worker = _world()
    worker.dispatch(OP, REQ)
    assert worker.commit(OP, "call_1") == {"ok": True, "op": OP}
    assert laptop.effects == {OP: 1}


def test_lost_nudge_is_recovered_by_heartbeat_reconcile() -> None:
    _, laptop, api, _, worker = _world(online=False)
    worker.dispatch(OP, REQ)
    assert laptop.effects == {}
    api.online = True
    api.reconcile()
    assert worker.commit(OP, "call_1")["ok"] is True
    assert laptop.effects == {OP: 1}


def test_crash_before_start_runs_exactly_once_after_reconnect() -> None:
    _, laptop, api, _, worker = _world()
    laptop.crash_at = "after_received"
    worker.dispatch(OP, REQ)
    api.online = True
    api.reconcile()
    assert laptop.effects == {OP: 1}
    assert worker.commit(OP, "call_1")["ok"] is True


@pytest.mark.parametrize("point, expected_effects", [("after_started", 0), ("after_effect", 1)])
def test_crash_between_started_and_result_reports_interrupted(point: str, expected_effects: int) -> None:
    _, laptop, api, _, worker = _world()
    laptop.crash_at = point
    worker.dispatch(OP, REQ)
    api.online = True
    api.reconcile()
    assert worker.commit(OP, "call_1") == {"interrupted": True, "outcome": "unknown"}
    assert laptop.effects.get(OP, 0) == expected_effects


def test_crash_after_result_returns_stored_result() -> None:
    _, laptop, api, _, worker = _world()
    laptop.crash_at = "after_result"
    worker.dispatch(OP, REQ)
    api.online = True
    api.reconcile()
    assert worker.commit(OP, "call_1") == {"ok": True, "op": OP}
    assert laptop.effects == {OP: 1}


def test_new_worker_after_result_commit_reads_result_without_redispatch() -> None:
    journal, laptop, api, lease, old = _world()
    old.dispatch(OP, REQ)                  # laptop ran it and the api committed the result
    lease.token = "t2"                     # old worker died; its lease was stolen
    new = Worker(journal, api, lease, my_token="t2")
    new.dispatch(OP, REQ)                  # recovery resumes the recorded sequence
    assert new.commit(OP, "call_1")["ok"] is True
    assert laptop.effects == {OP: 1}


def test_stale_worker_can_neither_dispatch_nor_commit() -> None:
    journal, _, api, lease, old = _world(online=False)
    old.dispatch(OP, REQ)
    lease.token = "t2"
    with pytest.raises(PermissionError):
        old.dispatch("call_1:2", REQ)
    api.online = True
    api.reconcile()
    with pytest.raises(PermissionError):
        old.commit(OP, "call_1")


def test_same_id_with_changed_payload_is_a_protocol_error() -> None:
    _, _, _, _, worker = _world()
    worker.dispatch(OP, REQ)
    with pytest.raises(ValueError):
        worker.dispatch(OP, {**REQ, "bytes": "y"})


def test_cancel_while_offline_never_runs() -> None:
    journal, laptop, api, _, worker = _world(online=False)
    worker.dispatch(OP, REQ)
    journal.cancel(OP)
    api.online = True
    api.reconcile()
    assert laptop.effects == {}
    assert laptop.records[OP]["state"] == "cancelled"


def test_delayed_retry_after_payload_reclaim_does_not_rerun() -> None:
    journal, laptop, api, _, worker = _world()
    worker.dispatch(OP, REQ)
    worker.commit(OP, "call_1")
    laptop.compact(OP)
    assert laptop.handle(OP, REQ) == {"completed": True, "payload": "reclaimed"}
    assert laptop.effects == {OP: 1}
    assert laptop.handle(OP, {**REQ, "bytes": "y"}) == {"error": "protocol: digest changed"}
    assert digest(REQ) == journal.rows[OP]["digest"]
```

- [ ] **Step 6: Run the model tests**

```bash
cd /work/surogates && PYTHONPATH=/work/surogates /work/surogates/.venv/bin/python -m pytest spikes/desktop/q5/test_journal_model.py -q
```

Expected: 11 passed. A failure is a flaw in the protocol as written in Section 2. Fix the model only where it drifts from Section 2. Where the model shows Section 2 itself is wrong, record the finding.

- [ ] **Step 7: Record Q5**

Append the Q5 section with:
- today's replay behaviour for an unanswered tool call, with file:line evidence from Steps 2 and 3;
- the model's results;
- the design consequence: where durable invocation replay must plug into the wake path (the file:line from Step 3), and any Section 2 correction the model forced.

- [ ] **Step 8: Commit**

```bash
git add spikes/__init__.py spikes/desktop/__init__.py spikes/desktop/q5 docs/superpowers/specs/2026-09-29-surogate-desktop-spike-results.md
git commit -m "chore(spike): answer Q5 on replay and the operation protocol"
```

---

### Task 7: Q6 — loopback + S256 PKCE sign-in with Firebase Google and GitHub

**Question:** Does loopback plus S256 PKCE sign-in work with Firebase Google and GitHub on a real agent, including rejection of replayed or mismatched codes? If not: correct the login-page flow.

**Prerequisites** (the human partner confirms these before Step 1, otherwise Q6 is recorded as "not run" with what's missing):
- The local dev stack is running: surogate-ops on :8888, the surogates api on :8000, and the web dev server with mkcert TLS (`cd /work/surogates/web && npm run dev`).
- A dev agent whose Firebase project has Google and GitHub providers enabled, with the web dev origin in its authorized domains.
- `mkcert -install` has been run on the workstation, so Chromium trusts the dev certificate.

**Files:**
- Create: `surogates/api/routes/desktop_auth.py` (throwaway, spike branch only), `spikes/desktop/q6/main.cjs`, `spikes/desktop/q6/negative.mjs`
- Modify: `surogates/api/app.py:689-726` (import and register the router), `web/src/features/auth/login-page.tsx:194` (desktop handoff after `storeAuthTokens`), the results document (Q6 section)

**Interfaces:**
- Consumes: `create_access_token(org_id, user_id, permissions)`, `create_refresh_token(org_id, user_id)` (`surogates/tenant/auth/jwt.py`), `get_current_tenant` (`surogates/tenant/auth/middleware.py`), `TokenResponse` (`surogates/api/routes/auth.py`), `request.app.state.redis`, `authFetch` (`web/src/api/auth.ts:50`).
- Produces:
  - `POST /api/v1/auth/desktop/code {state, code_challenge, port} -> {code, redirect_uri}` (authenticated)
  - `POST /api/v1/auth/desktop/token {code, code_verifier, state, redirect_uri} -> TokenResponse` (public)

`/v1/auth/` is already public in the auth middleware (`_PUBLIC_PATH_PREFIXES`, `middleware.py:68`). The code endpoint therefore authenticates through `Depends(get_current_tenant)` itself.

- [ ] **Step 1: Write the throwaway api routes**

`surogates/api/routes/desktop_auth.py`:

```python
"""Spike only: desktop sign-in handoff (loopback redirect + S256 PKCE)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from surogates.api.routes.auth import TokenResponse
from surogates.tenant.auth.jwt import create_access_token, create_refresh_token
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext

router = APIRouter()

_CODE_TTL_SECONDS = 60
_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class CodeRequest(BaseModel):
    state: str
    code_challenge: str
    port: int


class CodeResponse(BaseModel):
    code: str
    redirect_uri: str


class TokenRequest(BaseModel):
    code: str
    code_verifier: str
    state: str
    redirect_uri: str


def _origin(request: Request) -> str:
    return f"{request.url.scheme}://{request.headers.get('host', '')}"


def _key(code: str) -> str:
    return "surogates:desktop_code:" + hashlib.sha256(code.encode()).hexdigest()


def _bad() -> HTTPException:
    return HTTPException(status_code=400, detail="invalid_grant")


@router.post("/auth/desktop/code", response_model=CodeResponse)
async def desktop_code(
    body: CodeRequest, request: Request, tenant: TenantContext = Depends(get_current_tenant),
) -> CodeResponse:
    if not 1024 <= body.port <= 65535 or not _CHALLENGE_RE.match(body.code_challenge) or not _STATE_RE.match(body.state):
        raise HTTPException(status_code=400, detail="invalid_request")
    redirect_uri = f"http://127.0.0.1:{body.port}/callback"
    code = secrets.token_urlsafe(32)
    record = {
        "challenge": body.code_challenge, "state": body.state, "origin": _origin(request),
        "redirect_uri": redirect_uri, "org_id": str(tenant.org_id), "user_id": str(tenant.user_id),
    }
    await request.app.state.redis.set(_key(code), json.dumps(record), ex=_CODE_TTL_SECONDS, nx=True)
    return CodeResponse(code=code, redirect_uri=redirect_uri)


@router.post("/auth/desktop/token", response_model=TokenResponse)
async def desktop_token(body: TokenRequest, request: Request) -> TokenResponse:
    raw = await request.app.state.redis.getdel(_key(body.code))  # any attempt consumes the code
    if raw is None:
        raise _bad()
    record = json.loads(raw)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(body.code_verifier.encode()).digest()).rstrip(b"=").decode()
    checks = [
        hmac.compare_digest(challenge, record["challenge"]),
        hmac.compare_digest(body.state, record["state"]),
        body.redirect_uri == record["redirect_uri"],
        _origin(request) == record["origin"],
    ]
    if not all(checks):
        raise _bad()
    org_id, user_id = UUID(record["org_id"]), UUID(record["user_id"])
    return TokenResponse(
        access_token=create_access_token(org_id=org_id, user_id=user_id, permissions={"sessions:read", "sessions:write", "tools:read"}),
        refresh_token=create_refresh_token(org_id=org_id, user_id=user_id),
    )
```

In `surogates/api/app.py`, add `desktop_auth` to the `from surogates.api.routes import (...)` list at line 689. Add this line beside `auth.router` at line 726:

```python
    app.include_router(desktop_auth.router, prefix="/v1", tags=["auth"])
```

The token route works only when the browser-facing origin and the origin the desktop posts to match. The desktop therefore posts through the same web origin (`https://<dev web origin>/api/v1/...`), which the Vite dev server proxies to :8000.

- [ ] **Step 2: Add the login-page handoff**

In `web/src/features/auth/login-page.tsx`, replace line 194 (`storeAuthTokens(tokens.access_token, tokens.refresh_token);`) with:

```tsx
    storeAuthTokens(tokens.access_token, tokens.refresh_token);
    const handoff = new URLSearchParams(window.location.search);
    const desktopState = handoff.get("desktop_state");
    if (desktopState) {
      const response = await authFetch("/api/v1/auth/desktop/code", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          state: desktopState,
          code_challenge: handoff.get("code_challenge"),
          port: Number(handoff.get("port")),
        }),
      });
      if (response.ok) {
        const { code, redirect_uri } = (await response.json()) as { code: string; redirect_uri: string };
        window.location.assign(`${redirect_uri}?code=${encodeURIComponent(code)}&state=${encodeURIComponent(desktopState)}`);
        return;
      }
    }
```

Add `authFetch` to the imports from `../../api/auth` at the top of the file if it isn't already imported.

- [ ] **Step 3: Write the desktop side**

`spikes/desktop/q6/main.cjs`:

```js
const { BrowserWindow, shell } = require('electron');
const crypto = require('node:crypto');
const http = require('node:http');
const { writeResult } = require('../lib/results.cjs');

exports.run = async ({ argv }) => {
  const agent = process.env.SPIKE_AGENT_URL;
  const provider = argv.includes('github') ? 'github' : 'google';
  const win = new BrowserWindow({ width: 1100, height: 800, webPreferences: { sandbox: true, contextIsolation: true, partition: 'persist:agent-spike' } });
  await win.loadURL(agent);
  const verifier = crypto.randomBytes(32).toString('base64url');
  const challenge = crypto.createHash('sha256').update(verifier).digest('base64url');
  const state = crypto.randomBytes(24).toString('base64url');
  const { code, redirectUri } = await new Promise((resolve, reject) => {
    const srv = http.createServer((req, res) => {
      const u = new URL(req.url, 'http://127.0.0.1');
      if (u.pathname !== '/callback' || u.searchParams.get('state') !== state) { res.writeHead(400); res.end('bad callback'); return; }
      res.end('Signed in, you can return to Surogate');
      const p = srv.address().port;
      srv.close();
      resolve({ code: u.searchParams.get('code'), redirectUri: `http://127.0.0.1:${p}/callback` });
    });
    srv.listen(0, '127.0.0.1', () => {
      const p = srv.address().port;
      shell.openExternal(`${agent}/login?desktop_state=${state}&code_challenge=${challenge}&port=${p}`);
    });
    setTimeout(() => { srv.close(); reject(new Error('sign-in timed out')); }, 5 * 60000);
  });
  const resp = await fetch(`${agent}/api/v1/auth/desktop/token`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ code, code_verifier: verifier, state, redirect_uri: redirectUri }),
  });
  const tokens = await resp.json();
  if (!resp.ok) throw new Error(`token exchange failed: ${JSON.stringify(tokens)}`);
  if (new URL(win.webContents.getURL()).origin !== new URL(agent).origin) throw new Error('window left the agent origin');
  // Spike shortcut: the design hands tokens to a preload completion handler instead.
  await win.webContents.executeJavaScript(
    `localStorage.setItem('surogates_auth_token', ${JSON.stringify(tokens.access_token)});`
    + `localStorage.setItem('surogates_auth_refresh_token', ${JSON.stringify(tokens.refresh_token)});`,
  );
  win.reload();
  await new Promise((r) => win.webContents.once('did-finish-load', r));
  const meStatus = await win.webContents.executeJavaScript(
    "fetch('/api/v1/auth/me', { headers: { Authorization: 'Bearer ' + localStorage.getItem('surogates_auth_token') } }).then((r) => r.status)",
  );
  console.log('q6 result:', writeResult(`q6-${provider}`, { provider, exchangeStatus: resp.status, meStatus }));
};
```

- [ ] **Step 4: Write the rejection checks**

`spikes/desktop/q6/negative.mjs` needs `ACCESS_TOKEN`: a fresh access token copied from a signed-in browser tab (`localStorage.surogates_auth_token`).

```js
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const AGENT = process.env.SPIKE_AGENT_URL;
const TOKEN = process.env.ACCESS_TOKEN;
const PORT = 49152;
const REDIRECT = `http://127.0.0.1:${PORT}/callback`;

const pkce = () => {
  const verifier = crypto.randomBytes(32).toString('base64url');
  return { verifier, challenge: crypto.createHash('sha256').update(verifier).digest('base64url') };
};
const state = () => crypto.randomBytes(24).toString('base64url');
const post = async (p, body, auth) => {
  const r = await fetch(`${AGENT}/api/v1/auth/desktop/${p}`, {
    method: 'POST', headers: { 'content-type': 'application/json', ...(auth ? { authorization: `Bearer ${TOKEN}` } : {}) },
    body: JSON.stringify(body),
  });
  return { status: r.status, body: await r.json().catch(() => null) };
};
const mint = async (s, challenge, port = PORT) => post('code', { state: s, code_challenge: challenge, port }, true);
const exchange = (code, verifier, s, redirect = REDIRECT) => post('token', { code, code_verifier: verifier, state: s, redirect_uri: redirect });

const out = {};
{ const k = pkce(); const s = state(); const { body } = await mint(s, k.challenge);
  out.wrongVerifier = (await exchange(body.code, pkce().verifier, s)).status;
  out.correctAfterFailedAttempt = (await exchange(body.code, k.verifier, s)).status; }
{ const k = pkce(); const s = state(); const { body } = await mint(s, k.challenge);
  out.wrongRedirect = (await exchange(body.code, k.verifier, s, 'http://127.0.0.1:1/callback')).status; }
{ const k = pkce(); const s = state(); const { body } = await mint(s, k.challenge);
  out.wrongState = (await exchange(body.code, k.verifier, state())).status; }
{ const k = pkce(); const s = state(); const { body } = await mint(s, k.challenge);
  out.valid = (await exchange(body.code, k.verifier, s)).status;
  out.replay = (await exchange(body.code, k.verifier, s)).status; }
out.lowPort = (await mint(state(), pkce().challenge, 80)).status;
out.badChallenge = (await mint(state(), 'short', PORT)).status;
out.noAuthMint = (await post('code', { state: state(), code_challenge: pkce().challenge, port: PORT }, false)).status;
{ const k = pkce(); const s = state(); const { body } = await mint(s, k.challenge);
  await new Promise((r) => setTimeout(r, 61000));
  out.expired = (await exchange(body.code, k.verifier, s)).status; }

const dir = path.join(os.homedir(), 'surogate-spike-results');
fs.mkdirSync(dir, { recursive: true });
fs.writeFileSync(path.join(dir, `q6-negative-${os.hostname()}.json`), JSON.stringify(out, null, 2));
console.log(out);
```

- [ ] **Step 5: Run on the workstation**

The workstation is Ubuntu 24.04 with the user-namespace restriction on. Q6 tests sign-in, not sandboxing, so it uses Electron's standard SUID helper for the dev binary. That keeps the sandbox on, unlike `--no-sandbox`.

```bash
cd /work/surogates/spikes/desktop
sudo chown root:root node_modules/electron/dist/chrome-sandbox && sudo chmod 4755 node_modules/electron/dist/chrome-sandbox
export SPIKE_AGENT_URL=https://<dev web origin>
export NODE_EXTRA_CA_CERTS="$(mkcert -CAROOT)/rootCA.pem"   # the main process's Node fetch trusts the dev CA
npx electron . q6-google
npx electron . q6-github
ACCESS_TOKEN=<paste> node q6/negative.mjs
```

The human partner completes each sign-in in the system browser that opens. The expected results:

| Result file | Pass when |
|---|---|
| `q6-google-*.json`, `q6-github-*.json` | `exchangeStatus` 200 and `meStatus` 200 |
| `q6-negative-*.json` | `valid` 200; `noAuthMint` 401; `wrongVerifier`, `correctAfterFailedAttempt`, `wrongRedirect`, `wrongState`, `replay`, `lowPort`, `badChallenge` and `expired` all 400 |

Note whether the Node side needed `NODE_EXTRA_CA_CERTS`. That's evidence for Section 9's point that there are two TLS stacks.

- [ ] **Step 6: Record Q6, then revert the dev machine change**

Append the Q6 section with both providers' results, the negative-check table, and any login-page flow corrections. Then:

```bash
sudo chown "$USER":"$USER" /work/surogates/spikes/desktop/node_modules/electron/dist/chrome-sandbox && sudo chmod 0755 /work/surogates/spikes/desktop/node_modules/electron/dist/chrome-sandbox
```

- [ ] **Step 7: Commit**

```bash
git add surogates/api/routes/desktop_auth.py surogates/api/app.py web/src/features/auth/login-page.tsx spikes/desktop/q6 docs/superpowers/specs/2026-09-29-surogate-desktop-spike-results.md
git commit -m "chore(spike): answer Q6 on loopback PKCE sign-in"
```

---

### Task 8: Q7 — the file-operation boundary under races

**Question:** Does the actual file-operation boundary reject symlink and rename races, special files and protected paths during concurrent commands? If not: replace the unsafe filesystem primitive before proceeding.

It compares candidate A, a file-operation helper that runs inside `srt` with the session's policy (so the mount namespace is the boundary) and uses `O_NOFOLLOW`, `fstat` and a hard-link check, against baseline B, a `realpath` check followed by an ordinary open in the unsandboxed host. B is expected to lose races.

**Files:**
- Create: `spikes/desktop/q7/fsop.cjs`, `spikes/desktop/q7/fs-host.cjs`, `spikes/desktop/q7/main.cjs`
- Modify: the results document (Q7 section)

**Interfaces:**
- Consumes: `buildPolicy` (working variant, `extraRead: [appDir]`), `buildEnv`, `sessionTmp`, `writeResult`.
- Produces: `fsop.cjs`'s line protocol. It reads `{id, op: 'read'|'write', path, data?}` and writes `{id, ok, data?, code?, message?}`.

- [ ] **Step 1: Write candidate A's helper**

`spikes/desktop/q7/fsop.cjs`:

```js
const fs = require('node:fs');

const C = fs.constants;
const fail = (code, message) => Object.assign(new Error(message), { code });

function checkRegular(fd) {
  const st = fs.fstatSync(fd);
  if (!st.isFile()) throw fail('ENOTREG', 'not a regular file');
  if (st.nlink > 1) throw fail('EMLINK', 'file has more than one hard link');
}

const ops = {
  write(p, data) {
    const fd = fs.openSync(p, C.O_WRONLY | C.O_CREAT | C.O_NOFOLLOW | C.O_NONBLOCK | C.O_CLOEXEC, 0o644);
    try { checkRegular(fd); fs.ftruncateSync(fd, 0); fs.writeSync(fd, data); return { ok: true }; } finally { fs.closeSync(fd); }
  },
  read(p) {
    const fd = fs.openSync(p, C.O_RDONLY | C.O_NOFOLLOW | C.O_NONBLOCK | C.O_CLOEXEC);
    try { checkRegular(fd); return { ok: true, data: fs.readFileSync(fd, 'utf8').slice(0, 200) }; } finally { fs.closeSync(fd); }
  },
};

let buf = '';
process.stdin.on('data', (d) => {
  buf += d;
  let i;
  while ((i = buf.indexOf('\n')) >= 0) {
    const m = JSON.parse(buf.slice(0, i));
    buf = buf.slice(i + 1);
    let r;
    try { r = ops[m.op](m.path, m.data); } catch (e) { r = { ok: false, code: e.code, message: e.message }; }
    process.stdout.write(`${JSON.stringify({ id: m.id, ...r })}\n`);
  }
});
```

- [ ] **Step 2: Write the host that runs the attacker, candidate A and baseline B**

`spikes/desktop/q7/fs-host.cjs`:

```js
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const port = process.parentPort;

function lineClient(child) {
  const replies = new Map();
  let buf = '';
  child.stdout.on('data', (d) => {
    buf += d;
    let i;
    while ((i = buf.indexOf('\n')) >= 0) { const m = JSON.parse(buf.slice(0, i)); buf = buf.slice(i + 1); replies.get(m.id)?.(m); replies.delete(m.id); }
  });
  let seq = 0;
  return (msg) => new Promise((resolve) => { const id = ++seq; replies.set(id, resolve); child.stdin.write(`${JSON.stringify({ id, ...msg })}\n`); });
}

function baselineWrite(root, rel, data) {
  const target = path.join(root, rel);
  const dir = fs.realpathSync(path.dirname(target));
  if (!dir.startsWith(root)) throw new Error('outside');
  fs.writeFileSync(target, data);   // time-of-check / time-of-use gap on purpose
}

port.on('message', async ({ data }) => {
  const { SandboxManager: SM } = await import('@anthropic-ai/sandbox-runtime');
  await SM.initialize(data.policy);
  const wrap = async (cmd) => SM.wrapWithSandboxArgv(cmd, undefined, undefined, undefined, data.root);
  const spawnWrapped = async (cmd, stdio) => { const { argv, env } = await wrap(cmd); return spawn(argv[0], argv.slice(1), { cwd: data.root, env: { ...data.env, ...env }, stdio }); };
  const runWrapped = async (cmd) => new Promise(async (resolve) => {
    const c = await spawnWrapped(cmd, ['ignore', 'pipe', 'pipe']);
    let out = ''; c.stdout.on('data', (d) => { out += d; }); c.stderr.on('data', (d) => { out += d; });
    c.on('close', (code) => resolve({ code, out: out.slice(-2000) }));
  });
  const res = {};
  const outside = data.outside;
  const helper = await spawnWrapped(`ELECTRON_RUN_AS_NODE=1 '${process.execPath}' '${path.join(__dirname, 'fsop.cjs')}'`, ['pipe', 'pipe', 'pipe']);
  const op = lineClient(helper);

  // Race: a sandboxed command flips sub/ between a directory and a symlink to outside.
  const attackerCmd = `while :; do rm -rf sub; mkdir sub; rm -rf sub; ln -s '${outside}' sub; done`;
  for (const [name, write] of [
    ['A', (i) => op({ op: 'write', path: `sub/t-A-${i}.txt`, data: 'x' })],
    ['B', async (i) => { try { baselineWrite(data.root, `sub/t-B-${i}.txt`, 'x'); } catch { /* lost the race cleanly */ } }],
  ]) {
    const attacker = await spawnWrapped(attackerCmd, ['ignore', 'ignore', 'ignore']);
    for (let i = 0; i < 5000; i++) await write(i);
    attacker.kill('SIGKILL');
    await new Promise((r) => setTimeout(r, 500));
    res[`landedOutside_${name}`] = fs.readdirSync(outside).filter((f) => f.startsWith(`t-${name}-`)).length;
  }

  // Hard links (Review Focus 1).
  res.sandboxedHardlinkToToolchain = await runWrapped(`ln '${data.toolchainFile}' ./hl-toolchain; echo "exit=$?"`);
  fs.linkSync(data.outsideFile, path.join(data.root, 'hl-user'));   // made outside the sandbox, as a user could
  res.helperWriteThroughUserHardlink = await op({ op: 'write', path: 'hl-user', data: 'pwned-by-helper' });
  res.commandWriteThroughUserHardlink = await runWrapped("echo pwned-by-command >> hl-user; echo \"exit=$?\"");
  res.outsideFileAfter = fs.readFileSync(data.outsideFile, 'utf8');

  // Special files and magic links.
  await runWrapped('mkfifo fifo; ln -sf /dev/zero z; ln -sfn /proc/self/root pr');
  const started = Date.now();
  res.readFifo = { ...(await op({ op: 'read', path: 'fifo' })), ms: Date.now() - started };
  res.readDevZeroLink = await op({ op: 'read', path: 'z' });
  res.readThroughMagicLink = await op({ op: 'read', path: `pr${data.outsideFile}` });

  // Protected paths inside the folder.
  fs.mkdirSync(path.join(data.root, '.git'), { recursive: true });
  fs.writeFileSync(path.join(data.root, '.git/config'), '[core]\n');
  res.helperWriteGitConfig = await op({ op: 'write', path: '.git/config', data: 'x' });
  res.commandWriteGitConfig = await runWrapped('echo x >> .git/config; echo "exit=$?"');

  helper.kill();
  port.postMessage(res);
});
```

- [ ] **Step 3: Write the probe**

`spikes/desktop/q7/main.cjs`:

```js
const { utilityProcess } = require('electron');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { buildPolicy, toolchainDirs } = require('../lib/policy.cjs');
const { buildEnv, sessionTmp } = require('../lib/env.cjs');
const { writeResult } = require('../lib/results.cjs');

exports.run = async () => {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.homedir(), 'spike-q7-')));
  const outside = fs.mkdtempSync(path.join(os.homedir(), '.config/spike-q7-outside-'));
  const outsideFile = path.join(os.homedir(), 'Documents/q7-outside.txt');
  fs.mkdirSync(path.dirname(outsideFile), { recursive: true });
  fs.writeFileSync(outsideFile, 'original\n');
  const toolchainFile = path.join(toolchainDirs().find((d) => d.endsWith('.nvm')) || '/usr/bin', 'nvm.sh');
  const tmp = sessionTmp('q7');
  const policy = buildPolicy({ folder: root, tmp, allowedDomains: [], variant: process.env.SPIKE_VARIANT || 'home-deny', extraRead: [path.dirname(process.execPath)] });
  const host = utilityProcess.fork(path.join(__dirname, 'fs-host.cjs'), [], { serviceName: 'q7-fs', stdio: 'inherit' });
  const res = await new Promise((resolve) => {
    host.once('message', resolve);
    host.postMessage({ policy, root, env: buildEnv({ tmp }), outside, outsideFile, toolchainFile });
  });
  host.kill();
  console.log('q7 result:', writeResult('q7', res));
};
```

- [ ] **Step 4: Run on both VMs**

```bash
for ip in "$U24" "$U26"; do
  rsync -a --exclude node_modules spikes/desktop/ "spike@$ip:spike/"
  ssh "spike@$ip" 'cd spike && ./stage.sh >/dev/null && SPIKE_VARIANT=<variant from Q2> xvfb-run -a ~/.local/share/surogate/versions/spike/surogate q7 && jq . ~/surogate-spike-results/q7-*.json'
done
```

Candidate A passes when all of these hold:

| Finding | Pass when |
|---|---|
| `landedOutside_A` | 0 |
| `landedOutside_B` | any value; record it as the baseline's exposure |
| `sandboxedHardlinkToToolchain` | fails (`EXDEV` or not found) |
| `helperWriteThroughUserHardlink` | `EMLINK` |
| `readFifo` | fails with `ENOTREG` in under 100 ms, instead of hanging |
| `readDevZeroLink` | fails with `ELOOP` |
| `readThroughMagicLink` | fails |
| `helperWriteGitConfig`, `commandWriteGitConfig` | both fail |

The critical Review Focus 1 finding is `commandWriteThroughUserHardlink` together with `outsideFileAfter`. If `outsideFileAfter` contains `pwned-by-command`, a sandboxed command can modify a file outside the folder through a hard link that already existed.

- [ ] **Step 5: Record Q7**

Append the Q7 section. Design consequence: which primitive Section 4 adopts (candidate A, or a native `openat2(RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS)` helper if A fails), and what to do about pre-existing hard links. One option is to refuse or warn at binding time when the folder contains multiply-linked files; another is to accept and document the risk. Include `landedOutside_B` as the reason the plain `realpath` check was rejected.

- [ ] **Step 6: Commit**

```bash
git add spikes/desktop/q7 docs/superpowers/specs/2026-09-29-surogate-desktop-spike-results.md
git commit -m "chore(spike): answer Q7 on the file-operation boundary"
```

---

### Task 9: Q8 — process trees after a host or app crash

**Question:** Does a killed tool host or Electron main process leave no running command descendants, and does restart report ambiguous effects as interrupted? If not: add process supervision and prove cleanup before replay.

**Files:**
- Create: `spikes/desktop/q8/tree-host.cjs`, `spikes/desktop/q8/main.cjs`, `spikes/desktop/q8/scenario.sh`
- Modify: the results document (Q8 section)

**Interfaces:**
- Consumes: `buildPolicy`, `buildEnv`, `sessionTmp`, `writeResult`, `DIR` from `lib/results.cjs`.
- Produces: the journal file `~/surogate-spike-results/q8-journal.jsonl`, one JSON line per event: `{op, state: 'started'|'result', mode, unit?}`.

- [ ] **Step 1: Write the tree host**

It starts the same process tree twice: once spawned directly ("plain") and once inside a systemd user scope ("scope"). It writes and syncs a *started* record before each start.

`spikes/desktop/q8/tree-host.cjs`:

```js
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const { DIR } = require('../lib/results.cjs');

const port = process.parentPort;
const JOURNAL = path.join(DIR, 'q8-journal.jsonl');

function journal(entry) {
  const fd = fs.openSync(JOURNAL, 'a');
  fs.writeSync(fd, `${JSON.stringify(entry)}\n`);
  fs.fsyncSync(fd);
  fs.closeSync(fd);
}

port.on('message', async ({ data }) => {
  fs.writeFileSync(path.join(DIR, 'q8-host.pid'), String(process.pid));
  const { SandboxManager: SM } = await import('@anthropic-ai/sandbox-runtime');
  await SM.initialize(data.policy);
  const tree = "sleep 600 & setsid sleep 600 & (sleep 600 &); wait";
  const { argv, env } = await SM.wrapWithSandboxArgv(tree, undefined, undefined, undefined, data.cwd);
  const info = { dieWithParent: argv.join(' ').includes('--die-with-parent'), argvSample: argv.slice(0, 12) };
  for (const mode of ['plain', 'scope']) {
    const op = `q8-${mode}`;
    const unit = `surogate-spike-${mode}-${process.pid}`;
    journal({ op, state: 'started', mode, unit: mode === 'scope' ? unit : undefined });
    const cmd = mode === 'scope' ? ['systemd-run', '--user', '--scope', '--quiet', `--unit=${unit}`, '--', ...argv] : argv;
    spawn(cmd[0], cmd.slice(1), { cwd: data.cwd, env: { ...data.env, ...env, SPIKE_MARK: op }, stdio: 'ignore' });
  }
  port.postMessage({ type: 'running', ...info });
});
```

- [ ] **Step 2: Write the probe, which has start and recover modes**

`spikes/desktop/q8/main.cjs`:

```js
const { utilityProcess } = require('electron');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { buildPolicy } = require('../lib/policy.cjs');
const { buildEnv, sessionTmp } = require('../lib/env.cjs');
const { writeResult, DIR } = require('../lib/results.cjs');

const JOURNAL = path.join(DIR, 'q8-journal.jsonl');

function survivors() {
  const byMark = {};
  for (const pid of fs.readdirSync('/proc').filter((p) => /^\d+$/.test(p))) {
    try {
      const mark = fs.readFileSync(`/proc/${pid}/environ`, 'utf8').split('\0').find((e) => e.startsWith('SPIKE_MARK=q8-'));
      if (mark) byMark[mark.slice(11)] = (byMark[mark.slice(11)] || 0) + 1;
    } catch { /* process gone or not ours */ }
  }
  return byMark;
}

exports.run = async ({ arg, argv }) => {
  fs.mkdirSync(DIR, { recursive: true });
  if (arg === 'q8') {
    fs.writeFileSync(path.join(DIR, 'q8-main.pid'), String(process.pid));
    const folder = fs.mkdtempSync(path.join(os.homedir(), 'spike-q8-'));
    const tmp = sessionTmp('q8');
    const host = utilityProcess.fork(path.join(__dirname, 'tree-host.cjs'), [], { serviceName: 'q8-tree', stdio: 'inherit' });
    const info = await new Promise((resolve) => {
      host.once('message', resolve);
      host.postMessage({ policy: buildPolicy({ folder, tmp, allowedDomains: [], variant: process.env.SPIKE_VARIANT || 'home-deny' }), cwd: folder, env: buildEnv({ tmp }) });
    });
    fs.writeFileSync(path.join(DIR, 'q8-start-info.json'), JSON.stringify(info, null, 2));
    await new Promise(() => {});   // stay alive until the scenario kills us
  }
  // q8-recover: what a replacement host does before accepting work.
  const victim = argv.includes('main') ? 'main' : 'host';
  const before = survivors();
  const entries = fs.readFileSync(JOURNAL, 'utf8').trim().split('\n').map((l) => JSON.parse(l));
  const done = new Set(entries.filter((e) => e.state === 'result').map((e) => e.op));
  const interrupted = entries.filter((e) => e.state === 'started' && !done.has(e.op)).map((e) => e.op);
  const stops = {};
  for (const e of entries.filter((x) => x.unit)) {
    try { execFileSync('systemctl', ['--user', 'stop', `${e.unit}.scope`], { stdio: 'pipe' }); stops[e.unit] = 'stopped'; } catch (err) { stops[e.unit] = String(err.stderr || err); }
  }
  await new Promise((r) => setTimeout(r, 1500));
  let userManager;
  try { userManager = execFileSync('systemctl', ['--user', 'is-system-running'], { encoding: 'utf8' }).trim(); } catch (err) { userManager = String(err.stdout || err).trim(); }
  const startInfo = JSON.parse(fs.readFileSync(path.join(DIR, 'q8-start-info.json'), 'utf8'));
  console.log('q8 result:', writeResult(`q8-${victim}`, { victim, startInfo, survivorsBeforeRecover: before, interrupted, stops, survivorsAfterRecover: survivors(), userManager }));
};
```

- [ ] **Step 3: Write the scenario driver**

`spikes/desktop/q8/scenario.sh` runs inside a VM, over an SSH login session so a systemd user manager exists:

```bash
#!/usr/bin/env bash
set -euo pipefail
APP=~/.local/share/surogate/versions/spike/surogate
R=~/surogate-spike-results
for victim in host main; do
  rm -f "$R/q8-journal.jsonl" "$R/q8-host.pid" "$R/q8-main.pid"
  xvfb-run -a "$APP" q8 & DRIVER=$!
  for _ in $(seq 1 30); do [ -s "$R/q8-host.pid" ] && [ -s "$R/q8-start-info.json" ] && break; sleep 1; done
  sleep 3
  kill -9 "$(cat "$R/q8-$victim.pid")"
  sleep 3
  xvfb-run -a "$APP" q8-recover "$victim"
  kill "$DRIVER" 2>/dev/null || true
  pkill -9 -f "$APP" 2>/dev/null || true
  sleep 2
done
jq . "$R"/q8-host-*.json "$R"/q8-main-*.json
```

```bash
chmod +x /work/surogates/spikes/desktop/q8/scenario.sh
```

- [ ] **Step 4: Run on both VMs, then without a user manager**

```bash
for ip in "$U24" "$U26"; do
  rsync -a --exclude node_modules spikes/desktop/ "spike@$ip:spike/"
  ssh "spike@$ip" 'cd spike && ./stage.sh >/dev/null && SPIKE_VARIANT=<variant from Q2> ./q8/scenario.sh'
done
# Review Focus 5: a session with no systemd user manager.
ssh "spike@$U24" 'sudo systemd-run --uid=$(id -u) --pty --quiet env -u XDG_RUNTIME_DIR -u DBUS_SESSION_BUS_ADDRESS bash -lc "cd spike && ./q8/scenario.sh" || true'
```

Record these findings for each victim:
- `survivorsBeforeRecover`: whether `plain` trees survive the kill;
- `stops` and `survivorsAfterRecover`: scope trees should be at 0;
- `interrupted`: both ops appear, since neither wrote a result;
- `startInfo.dieWithParent`: whether `srt`'s `bwrap` uses `--die-with-parent`, and whether that covers the `setsid` and subshell descendants;
- `userManager`, and what happens in the run without a user manager.

- [ ] **Step 5: Record Q8**

Append the Q8 section. Design consequence: the supervision mechanism Section 1 must specify. Name the mechanism, for example one systemd user scope per root session stopped before a replacement host accepts work, and the fallback when no user manager exists, for example refusing local sessions with a clear message, or a cgroup-free fallback that the evidence shows works. Confirm or correct "a crash yields interrupted and never reruns".

- [ ] **Step 6: Commit**

```bash
git add spikes/desktop/q8 docs/superpowers/specs/2026-09-29-surogate-desktop-spike-results.md
git commit -m "chore(spike): answer Q8 on process-tree cleanup"
```

---

### Task 10: Consolidate results and update the design

**Files:**
- Modify: `docs/superpowers/specs/2026-09-29-surogate-desktop-spike-results.md` (Summary table, a decision list)
- Modify: `docs/superpowers/specs/2026-09-29-surogate-desktop-design.md` (only the sections the results change)

**Interfaces:**
- Consumes: the eight result sections.
- Produces: a spec that planning can proceed from, and a clear list of design changes.

- [ ] **Step 1: Complete the Summary table**

Give each question one row: `| # | question | YES/NO/PARTIAL | design change or "none" |`. Below the table, add "Decisions for planning": one bullet per design change, naming the spec section.

- [ ] **Step 2: Edit the design**

For each "Decisions for planning" bullet, change the named spec section so it states the decided mechanism: the AppArmor profile text in Section 9, the read and write lists in Section 4, the runner model in Section 4, the browser network model and supported browser matrix in Section 5, the replay hook in Section 2, the file primitive in Section 4, and the supervision mechanism in Section 1. Where a question's answer is NO with no workable fallback, mark the dependent feature as blocked in the Build order instead of inventing a design.

Update the spec's status line to `Status: design approved and spike-validated. Ready for implementation planning` only when every question is YES or has a decided fallback.

- [ ] **Step 3: Self-check the edited spec**

```bash
cd /work/surogates && grep -n "spike must\|must verify\|The spike" docs/superpowers/specs/2026-09-29-surogate-desktop-design.md
```

Every remaining "the spike must …" sentence has to be replaced by the result, or deliberately kept with a pointer to the results section that explains why it is still open.

- [ ] **Step 4: Commit on the spike branch, then move the documents to the docs branch**

```bash
cd /work/surogates
git add docs/superpowers/specs/2026-09-29-surogate-desktop-spike-results.md docs/superpowers/specs/2026-09-29-surogate-desktop-design.md
git commit -m "docs(desktop): record spike results and update the design"
SPIKE_DOCS=$(git rev-parse HEAD)
git checkout docs/desktop-client-design
git checkout spike/desktop-platform -- docs/superpowers/specs/2026-09-29-surogate-desktop-spike-results.md docs/superpowers/specs/2026-09-29-surogate-desktop-design.md
git commit -m "docs(desktop): record spike results and update the design"
git log -1 --format='%h %s'
```

Expected: the docs branch has the results document and the updated design, but none of `spikes/`, `surogates/api/routes/desktop_auth.py`, or the login-page change.

- [ ] **Step 5: Tear down the VMs** (only after the human partner has reviewed the results)

```bash
for vm in sgd-u24 sgd-u26; do virsh -c qemu:///system destroy "$vm"; virsh -c qemu:///system undefine "$vm" --remove-all-storage; done
```
