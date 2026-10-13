# Surogate Desktop on a Windows PC: the test day

This runbook tests a branch of the Windows line on a Windows 11 x64 PC. Each part of the app that needs real hardware adds its steps here, in the order below; a step for a part the branch does not have yet is not run.

Work in a fresh local administrator account made for this (Settings › Accounts › Other users), never your own: its own profile, credentials and OneDrive. Everything below runs as that user, in PowerShell, unless a step says "elevated".

Each step says what a pass looks like. When something fails, keep going where you can, and send back what the step says to send.

## 0. Install once

- **Git for Windows**, with its bash (the default choices).
- **Node 22.14.0**: the `node-v22.14.0-win-x64.zip` from nodejs.org, unpacked, and its folder first on `PATH`. Check that `node -v` prints `v22.14.0`.
- **Rust**, through `rustup` (the default, `stable-x86_64-pc-windows-msvc`).
- **Visual Studio Build Tools**, with the "Desktop development with C++" workload.
- **`gh`**, then `gh auth login`.
- **Virtual Machine Platform** on: in an elevated PowerShell, `Enable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform`, then restart.

## 1. Get the branch

```
git config --global core.autocrlf false
git clone -b <branch> https://github.com/invergent-ai/surogates.git
cd surogates\desktop
$env:NPM_CONFIG_SCRIPT_SHELL = "C:\Program Files\Git\bin\bash.exe"
npm ci
npm run electron:install
```

- `core.autocrlf false` keeps each file's line ends as the repository has them; the tests read some files byte for byte.
- npm runs the package's scripts in cmd unless told otherwise, and they are a POSIX shell's: Git for Windows' bash, by its whole path, runs them.
- Pass: no error.

## 2. The seams (each OS's row of every part)

```
npm run typecheck
npx vitest run --project windows
```

- These are the suites the Windows workflow runs on GitHub's `windows-2025` runner, here on a PC's own Windows 11.
- Pass: the typecheck prints nothing, and the last lines say every test file passed.
- **Send back:** the last 30 lines of the vitest run.

## 3. What this PC offers the VM

In an elevated PowerShell, from `desktop\test\vm\win32\hcs-probe`:

```
Get-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform | Format-List FeatureName, State, RestartNeeded
cargo build --release --locked
.\target\release\hcs-probe.exe
.\target\release\hcs-probe.exe   # again, in a PowerShell that is not elevated
```

- The first line asks Windows whether Virtual Machine Platform is on; the probe asks the Host Compute Service which schema versions it supports (`HcsGetServiceProperties`).
- Pass: `State : Enabled`; the elevated probe prints `HRESULT 0x00000000` and a JSON document naming `SupportedSchemaVersions`, 2.6 among them on Windows 11.
- Record, whatever it is: what the probe that is not elevated prints. Microsoft's HCS lets in only administrators and members of Hyper-V Administrators (`HCS_E_ACCESS_DENIED`); whether that holds for this call too is not documented.
- **Send back:** all four outputs, with the PC's CPU and `winver`'s edition and build.

## 4. The VM, the file host, the shell, the browser, the MSI

Each comes here with its part, as a measurement of its own, run in this order. **Whose rights a shared folder uses comes first: a FAIL there is a security finding. Stop and report it before anything else.**

1. **Whose rights a shared folder uses.** A folder shared into the guest with the user's own token: a subfolder only an administrator can read, another user's folder, and a file the user may read but not write; on a standard account and an administrator's. The guest must reach what the user can, and nothing more.
2. **The boot.** From the start to the agent's hello: the kernel booted directly with the image and its initrd, three SCSI disks, the rule loaded, the console on COM1. Median of three; the target is under 1.5 s.
3. **The guest's sockets.** The three services bound as the user; a round trip, a 261 KB result through the helper's pipes, the throughput, and what an ended connection does.
4. **The disks.** The SCSI addresses the guest sees, the read-only flags, the sparse image taken, and `discard` reaching the sessions disk.
5. **Owners, names and links in a shared folder.** The uid, gid and mode the guest sees; a write by another guest uid; `chmod`, links, FIFOs, xattrs, a rename over a file. **A junction and a symlink in the folder that lead outside it: a read through either that reaches outside is a security finding.** And names: ASCII case and the fold's letters, a trailing dot, a trailing space, `:`, `NUL`, `CON.txt`, a short name (`GIT~1`).
6. **The read-only share.** A guest root's remount read-write, writes, `chmod`, `setxattr`, `truncate`, links and renames: none may change the folder. **A change is a security finding.**
7. **Shares while the guest runs.** Adding and removing them: their latency, 40 cycles with two roots, eight at once, and a removal while the guest still holds the mount.
8. **Coherence.** A write on the host, then a read in the guest, 1 000 times; a patch with its lint; `git status` on 50 000 files.
9. **A shared `mmap`** of a file in a shared folder.
10. **A large tree.** `npm install` and `git status` in a 150 000-file `node_modules`.
11. **Folders of every kind.** Documents moved into OneDrive with cloud-only files, read through the share and the file tools; a USB exFAT drive; a ReFS Dev Drive; a `subst` drive; a network drive (refused); a folder in a compressed or an encrypted folder.
12. **What is left behind.** The VM's helper killed, the VM manager killed, the app ended from Task Manager, the service killed and restarted: look for a `vmmem`, a `vmwp`, a pipe or an app process left.
13. **Sleep, by hand.** A real sleep during a command, on a modern-standby laptop and an S3 desktop, and a hibernate: the clock, the link's keepalive, the command's timeout.
14. **Memory.** The VM's memory over a working day.
15. **Beside other virtualization.** WSL 2 and Docker Desktop running; VirtualBox and VMware Workstation installed; Memory Integrity and Credential Guard on; Windows Sandbox.
16. **Defender.** The image's unpack, the first boot and `npm install` in a share, with real-time protection on, and with a test exclusion set by hand.
17. **The shell, by eye.** A toast and its click; AltGr+Space on a German layout; the login item; the tray; a `surogate://` link with the app running and quit; per-user Chrome and Edge found; a root certificate pushed by Group Policy, and one by Intune.
18. **Install and first run**, on Windows 11 Home and Pro: Virtual Machine Platform off, the MSI turning it on, the restart and the first boot; virtualization off in the firmware; a VM without nested virtualization; the boot's identity across a restart.
19. **Updates.** UAC's prompt and the publisher it names; a standard user's words; a dismissed prompt; nothing left waiting for a restart; the new app started as the user, not elevated.
20. **SmartScreen and Smart App Control** on the signed MSI's first download on a clean PC.

## The file host's walk on this PC (security first)

Windows has no sandbox for the file helper: the walk is the only thing that keeps the file tools inside the folder (`src/files/fd-paths-win32.ts`, `native/ntat/ntat.c`). These steps run its suites on this PC's own disks, and the race in each kind of folder a person binds. **A FAIL of the race, or of any test that names a junction, a link or a short name, is a security finding: stop and report it before anything else.**

### Build

From `desktop`, in PowerShell:

```
npx tsc -p tsconfig.build.json
powershell -File native\ntat\build.ps1
& "C:\Program Files\Git\bin\bash.exe" scripts/rg.sh
```

- `build.ps1` finds Visual Studio's C++ tools by itself, and fetches Node's headers and `node.lib`, each checked against its pinned sha256.
- Pass: `dist\native\ntat.node` and `bin\rg.exe` are there.

### The suites

```
npx vitest run --project unit --reporter=verbose test/ntat-win32.test.ts test/folder-lock-win32.test.ts test/fs-through-walk.test.ts test/file-host-win32.test.ts test/file-tools-race-win32.test.ts
```

- The short-name test sets a short name with `fsutil file setshortname`, which needs an administrator: run this one elevated, in the test account.
- Pass: every test passes. The race prints one line, `the race on Windows: {...}`, whose `contained` part is `{"reached":0,...,"wroteOutside":0,"kept":"OUTSIDE\n"}`.
- **Send back:** the whole output, and the `short name of .git` and `case sensitive` lines it prints.

### The race in each kind of folder

The suites work in the temp folder. Point it at each kind of folder in turn, and run the race there:

```
foreach ($dir in "$env:USERPROFILE\Documents\race", "$env:OneDrive\race", "E:\race") {
  New-Item -ItemType Directory -Force $dir | Out-Null
  $env:TEMP = $dir; $env:TMP = $dir
  npx vitest run --project unit test/file-tools-race-win32.test.ts test/file-host-win32.test.ts 2>&1 | Select-String "the race on Windows|Tests "
}
```

- `E:` stands for a USB drive formatted exFAT; use its letter. Add a ReFS Dev Drive's folder if the PC has one (`fsutil devdrv query`).
- Pass, on NTFS and ReFS: `"reached":0` and `"wroteOutside":0` in each `contained` part.
- On exFAT, which has no reparse points, no junction can be made: the control reaches nothing either, and its test fails with `expected 0 to be greater than 0`. That is the pass there; any other failure is not.
- In OneDrive, record whether the files the suites make stay "Available on this device", and whether OneDrive shows any of them as a conflict.

### OneDrive's placeholders

In a folder of OneDrive's with Files On-Demand on, make a file "Online-only" (right click › Free up space), then from `desktop`:

```
$folder = "$env:OneDrive\race"   # the folder that holds the online-only file
node --input-type=module -e "const w = await import('./dist/files/fd-paths-win32.js'); const root = w.guestPathOf(process.argv[1]); w.win32FdPaths.bind([root]); for (const name of w.win32FdPaths.readdir(root)) { const s = w.win32FdPaths.lstat(root + '/' + name); console.log(name, s.isFile(), s.isDirectory(), s.isSymbolicLink(), Number(s.size)); }" $folder
```

- Pass: the online-only file is listed as a file (`true false false`), not a link, with its size; Explorer still shows it online-only afterwards (a listing does not download it).
- Then read it with the file tools: `node --input-type=module -e "const w = await import('./dist/files/fd-paths-win32.js'); const { perform } = await import('./dist/files/operations.js'); const root = w.guestPathOf(process.argv[1]); w.win32FdPaths.bind([root]); console.log(JSON.stringify(await perform('read', { key: root + '/' + process.argv[2], max_bytes: 64 }, { folder: root, home: root, env: {} }, new AbortController().signal)))" $folder <name>`
- Record: whether it reads the file's bytes (OneDrive downloads it), or what error it answers. Either is safe; which one it is decides the words a person sees.

### A folder on a network volume

```
net use Z: \\<a PC or server>\<a share>
node --input-type=module -e "const { onNetworkVolume } = await import('./dist/binding/folder.js'); console.log(process.argv.slice(1).map(onNetworkVolume))" 'Z:\' '\\<a PC or server>\<a share>' $env:USERPROFILE
net use Z: /delete
```

- Pass: `[ true, true, false ]`.

### The boot's identity, across a restart, and in the app's own process

```
node -e "console.log(require(process.argv[1]).bootId())" (Resolve-Path dist\native\ntat.node)
Set-Content "$env:TEMP\ntat-electron.cjs" "const { app } = require('electron'); app.whenReady().then(() => { const n = require(process.argv.at(-1)); console.log('bootId', n.bootId(), 'sid', n.userSid()); app.quit(); });"
.\node_modules\electron\dist\electron.exe "$env:TEMP\ntat-electron.cjs" (Resolve-Path dist\native\ntat.node)
```

- The second loads ntat in Electron's main process, as the app's binder does, through the hook that finds node's exports in Electron's own program.
- Restart the PC, and run the first line again.
- Pass: Electron prints the same number as node; after the restart the number is one more (or larger).

### Defender's cost

With real-time protection on, then with the test's temp folder excluded by hand (Windows Security › Virus & threat protection › Exclusions), and the exclusion removed after:

```
Measure-Command { npx vitest run --project unit test/file-tools-race-win32.test.ts } | Select-Object TotalSeconds
```

- Record both times. Nothing passes or fails: the owner weighs an exclusion against the cost.
