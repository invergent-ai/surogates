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

## The guest on Hyper-V

The guest image the Windows app boots is the x64 one Linux boots, with Hyper-V's initrd beside it. On Linux it was tested under a stand-in for Hyper-V's devices: QEMU with SCSI disks, vsock and a Plan 9 server of the tests' own. What only a Windows PC runs is Hyper-V's own drivers and HCS's own Plan 9 server. These checks need the development service and `surogate-vm.exe` of the Windows VM backend, which boot the guest; run them as soon as those boot it.

1. **The guest image, with its initrd.** `gh run download` the x64 image built from the branch into `images/guest/out`. Pass: it holds `initrd.zst`, `rootfs.img.zst`, `vmlinuz.zst` and a `manifest.json` that names all three.
2. **The boot.** Boot the guest with the HCS document's `LinuxKernelDirect`, `InitRdPath` the image's `initrd`, and the kernel's line `kernelLine({ disks: "scsi", transport: "vsock", names: "windows" })` (`desktop/src/vm/qemu.ts`), with the image, the agent disk and the sessions disk at SCSI LUNs 0, 1 and 2. Pass:
   - the console, COM1, has no line containing `surogate:` but the rule's (the guest's words that stop a boot come through the kernel's log, so a line reads `[    3.14…] surogate: …`, after the timestamp, not at the start): the one line is `surogate: the protected-names rule attached 16 of 16 hooks, under the LSMs …`;
   - no line says `no disk came at LUN`, `the kernel did not load` or `cannot connect to 2:500`;
   - the agent says hello within 15 s. Send back the console log and the time from the boot to hello, three times.
3. **What Hyper-V gave the guest.** With the boot's guest held at its console, or through a debug build that runs a command as the guest's root, run:

   ```
   ls /sys/class/scsi_disk/; for d in /sys/class/scsi_disk/*; do echo "$d $(ls $d/device/block) $(cat $d/device/block/*/ro)"; done
   cat /proc/modules | cut -d' ' -f1 | sort
   cat /run/surogate/rule; ls -l /run/surogate/ports
   ```

   Pass: three disks, at `…:0:0:0`, `…:0:0:1` and `…:0:0:2`, the first two read-only; the modules `hv_vmbus`, `hv_storvsc`, `scsi_transport_fc`, `vsock`, `hv_sock`, `hv_balloon`, `netfs`, `9pnet`, `9pnet_fd`, `9p` and `overlay`, and never `hv_utils` or `virtiofs`; the rule attached; no socket left in `/run/surogate/ports`, as each goes once the agent has connected. Send back the output.
4. **A folder on a Plan 9 share (security).** Bind a chat to a folder, run `id; ls -ln; touch made; stat -f -c %T .` in it, and from Windows look at `made`'s owner. Pass: the files show as the root's own uid, from 10000, and the filesystem is `v9fs`. Then run, as a command, `touch desktop.ini x.lnk y.url z.library-ms; python3 -c 'import socket; socket.socket(40, 1)'`. Pass: each `touch` is refused, `Operation not permitted`, and the socket is refused with `PermissionError`.
   - **The aliases NTFS may resolve to another name**, which the guest's rule refuses under `surogate.names=windows` so HCS's Plan 9 server is not the only wall. In the folder, make a `.git/hooks` first (`mkdir -p .git/hooks`), then run each of these as a command and check it is refused with `Operation not permitted`, nothing written:
     ```
     mkdir 'GIT~1'; touch '.git./x' '.git /x' '.git::$INDEX_ALLOCATION'; touch 'x.lnk::$DATA' 'desktop.ini.' 'desktop.ini '; touch 'sub\desktop.ini'
     ```
     Pass: every one refused, and `ls -laR .git GIT~1 sub 2>/dev/null` shows no file made under any of these names, and `.git/hooks` holds no `x`.
   - **What is HCS's server's, not the guest's, recorded, not a guest finding:** a device name (`touch CON NUL`) and a symlink to a UNC path (`ln -s '//server/share/x' unc`). The guest's rule does not judge these; the file tools refuse the device names on Windows, and this step records what the server makes of each. Send back what the server did with them.

   **A file made in the folder under a protected name, an Explorer name or one of the aliases above, or a vsock socket made, is a security finding: stop and report it.**
5. **The memory given back.** Leave the guest idle for ten minutes after a command that took 1 GiB (`python3 -c 'b = bytearray(1 << 30)'`). Pass: `vmmem`'s memory in Task Manager falls back within a minute of the command's end. Send back the two numbers.

## The Windows VM backend, measured on a PC

The VM backend boots the guest through the Host Compute Service: a LocalSystem service makes every HCS call, and `surogate-vm.exe`, as you, carries the guest's ports. These steps need a real PC: GitHub's runners have no nested virtualization. Work in a fresh local administrator account made for this, never your own.

### Install the service and build the programs

In an elevated PowerShell, from `desktop\native\windows`:

```
powershell -ExecutionPolicy Bypass -File dev-service.ps1
```

It builds `surogate-service.exe` and `surogate-vm.exe` (`cargo build --locked --release`), installs the development service as LocalSystem on demand, lets interactive users start it, and grants the VM group read on `desktop\dist\agent.vhd`. Remove it afterwards with `-Remove`.

- Get the guest image build's x64 image into `images\guest\out` (`gh run download` of the image, initrd and agent-disk artifacts), so nothing is built in a Linux container. Point the measurements at it: `$env:SUROGATE_VM_IMAGE = "<repo>\images\guest\out"`, and `$env:SUROGATE_VM_AGENT_DISK` at the agent disk with its VHD footer.

### The measurements, in order

Each is a vitest file under `desktop\test\vm\win32\measure`, run one at a time by its own config, each writing `test\vm\win32\measure\out\<name>.json` with this PC's CPU, edition and build. Run them in this order:

```
cd desktop
npx vitest run -c test/vm/win32/measure/vitest.config.ts identity
```

1. **`identity` — whose rights the Plan 9 server serves a folder with. THE SECURITY STOP, FIRST.** A share added with your own token and `UseShareRootIdentity`: the guest reads your own file, cannot read a subfolder only an administrator can, and cannot write a file you may only read; another user's folder is refused at the service, and if forced, is unreadable in the guest. **A FAIL is a security finding: stop and report it before anything else. Folder sharing stays off, and the app's own Plan 9 server is built instead.** Set `SUROGATE_OTHER_USER_FOLDER` to another user's folder, and run this on a standard account and on an administrator's. The admin-only subfolder is set with `icacls`; run elevated to set it, or have an administrator set it beforehand on the standard account.
2. **`boot`** — the guest booted directly by HCS with its initrd, three SCSI disks, the rule attached, the console on COM1; median of three, under 1.5 s.
3. **`ports`** — the agent's three ports relayed through the pipes: a round trip, a 261 KB result whole, and what an ended connection does.
4. **`disks`** — the SCSI addresses `:0:0:0`, `:0:0:1`, `:0:0:2`, the read-only flags, and discard on the sessions disk.
5. **`owners`** — the uid, gid and mode the guest sees; who owns what the guest makes; links, FIFOs, xattrs, a rename over. **And the security check: a junction and a symbolic link in the folder that lead outside it — a read through either that reaches outside is a security finding.** The names NTFS resolves to another are recorded.
6. **`readonly`** — a place's folder shared read-only: the guest's root cannot write it, however it remounts. **A write that reaches the folder is a security finding.**
7. **`shares`** — adding and removing shares while the guest runs: latency, 40 cycles with two roots, and a removal while a process holds the mount.
8. **`coherence`** — a host write then a guest read, 1 000 times; `git status` on 50 000 files after the host changed 10.
9. **`mmap`** — a shared map of a file in a share (EINVAL on 9p is the safe answer).
10. **`folders`** — Documents in OneDrive, an exFAT USB drive, a ReFS Dev Drive, a `subst` drive, a compressed or encrypted folder; each by its environment variable (`SUROGATE_ONEDRIVE_FOLDER` and the rest). A network drive's refusal at binding is the file host's.
11. **`lifetime`** — `surogate-vm.exe` killed, the manager killed, the app ended, the service killed and restarted: look for a `vmmem` or `vmwp` left.
12. **`sleep`** — by hand: start it, put the PC to sleep for about a minute, wake it, and read what the command saw across the sleep. Do this on a modern-standby laptop and an S3 desktop, and a hibernate.
13. **`memory`** — `vmmem`'s working set idle, at a 1.5 GiB peak, and each minute after, for `MEASURE_MINUTES`.
14. **`beside`** — the guest boots and runs a command beside WSL2, Docker Desktop, VirtualBox, VMware, Memory Integrity, Credential Guard and Windows Sandbox; set each up, then run it.
15. **`defender`** — the unpack, the boot and an `npm install` in a share, with real-time protection on, and with a hand-set exclusion of the data and share folders; compare.
16. **`requirements`** — on a PC with Virtual Machine Platform off, with virtualization off in the firmware, and on a VM without nested virtualization: each case's HCS answer and the backend's words.

Two of the service's safeguards rest on undocumented Host Compute Service behaviour; measure each by hand, and treat a FAIL as a security finding.

17. **Whose rights a granted disk uses. SECURITY STOP.** The service grants the VM group access to the caller's disk files while impersonating the caller (`HcsGrantVmAccess`), so a caller can grant only on files whose ACL the caller may edit — its own. Confirm it: as a standard user, ask the service (through a development build) to boot with a disk path the user can read but whose ACL the user may **not** change (e.g. a file `icacls`-set to deny the user `WRITE_DAC`), and with a path to a file the user cannot read at all. **A boot that grants the VM group on a file the caller could not have ACL-edited, or on a file the caller cannot read, is a security finding** (it would mean the grant runs as SYSTEM, not the caller): stop and report it. Record, for a normal boot, the VM-group ACE that appears on each disk and that it is gone after the VM ends.
18. **Which account opens the disks and the kernel. SECURITY STOP.** For direct kernel boot, confirm the kernel, the initrd and the disks are opened by the low-privilege VM worker account (granted above), not by SYSTEM or `vmcompute`. Boot with the kernel/initrd/rootfs readable by that worker account alone (not by your user), through a path with **no** reparse point, and check the guest comes up; then put a kernel the worker account can read but **your user cannot** behind an ancestor your user controls, and confirm the service refuses the path (the walk refuses any reparse point on the way) and the file never loads. **A SYSTEM-only file loading into a guest you control is a security finding:** stop and report it.

### The VM suite and the escape probes

With the backend on win32 and the development service running:

```
$env:SUROGATE_VM_TESTS = "1"
npx vitest run test/vm
```

Then the escape probes by hand: `ip link` shows only `lo`; `~/.ssh`, another root's folder and its processes are absent; a protected write fails; the network goes only through the proxy; a junction to `%USERPROFILE%` in the folder is refused by the file host. Record the counts.

**Send back** the `out` folder, the VM suite's counts, and the console logs each step names. **`identity`, `owners`'s link check and `readonly` are security checks: a FAIL in any is a finding — stop and report it.**

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

## 5. The shell and the browser, by hand

These run in the test account of step 0, after step 1. Each says what a pass looks like. **The first is a security check: a FAIL there is a security finding. Stop and report it before anything else.**

```
$env:NPM_CONFIG_SCRIPT_SHELL = "C:\Program Files\Git\bin\bash.exe"
npm run build:win32
```

- This builds the app's code and `native\windows\target\release\surogate-helper.exe`. Pass: no error.

### 5.1 The user's Group Policy certificates are not the user's to write (security)

The app trusts the roots Group Policy gives this user, from a key under `HKCU\Software\Policies`, as Group Policy writes it. A standard user must not be able to write there. In a **standard** account (`net user surogate-std <a password> /add`, then sign in as it), in a PowerShell that is not elevated:

```
New-Item -Path "HKCU:\Software\Policies\Microsoft\SystemCertificates\Root\Certificates\surogate-check" -Force
```

- Pass: PowerShell refuses it, with "Requested registry access is not allowed" or another access error.
- **FAIL, a security finding:** the key is made. Remove it (`Remove-Item -Path "HKCU:\Software\Policies\Microsoft\SystemCertificates\Root\Certificates\surogate-check"`) and report it.
- **Send back:** the output.

### 5.2 The helper's own tests, with the computer's certificate stores

In an elevated PowerShell, in the administrator test account, from `desktop\native\windows\helper`:

```
cargo test --locked -- --include-ignored
```

- They add five test roots to this computer's and this user's stores, list them, and take them out again.
- Pass: `test result: ok` for each part, 0 failed.
- **Send back:** the last 20 lines.

### 5.3 The development app

```
test\win32\dev-app.ps1
```

- It makes "Surogate Dev" in the Start menu, registers `surogate://` for this user to start it, and starts it. Pass: the window opens, "Surogate Dev" is in the Start menu, and the script says it registered `surogate://`.
- Quit it from its icon in the notification area, and start "Surogate Dev" from the Start menu, so that it has no console of PowerShell's. Open Settings › Browser, close Settings, and open it again. Pass: no console window flashes up at any time.
- Sign in to an agent, as the app asks.

### 5.4 The window and the notification area

1. In Settings › Appearance, take the light theme, then the dark one. Pass: the window's own minimise, maximise and close buttons are in the window's colours each time, and nothing of the page lies under them.
2. Open Settings, and press its close button (its upper half too). Pass: Settings closes at the first press.
3. In Windows's Settings › Personalization › Colors, take "Custom", then "Dark" for Windows mode and "Light" for app mode, then the other way round. Pass: Surogate's icon in the notification area is light on the dark taskbar and dark on the light one. If it does not change until Surogate is started again, record that.
4. Close the window with its close button. Pass: it goes, and Surogate's icon stays in the notification area. Click the icon. Pass: the window comes back.
5. Pin "Surogate Dev" to the taskbar. Close the window, then click the pinned button. Pass: the window comes back. Minimise it, then start "Surogate Dev" from the Start menu. Pass: the window comes back, in front of the other windows, not only flashing in the taskbar.

### 5.5 Notifications

1. Close the window. In a chat on the agent's own site, ask the agent something it must ask you about, as a file it wants to change.
2. Pass: a toast names "Surogate Dev" and says what the agent asks. Click it. Pass: the window opens on that chat.
3. Again, but wait until the toast has left the screen, then click it in the notification centre (Win+N). Pass: the window opens on that chat.

### 5.6 Links and start at login

1. With the app running, open `surogate://open?url=<the agent's address>` from the Run box (Win+R). Pass: the window comes forward, on that agent.
2. Quit the app from its icon's menu, and open the link again. Pass: the app starts, on that agent.
3. In Settings, turn Start at login on. In Task Manager › Startup apps, record the name it is listed by, and its status. Sign out and in again. Pass: Surogate is in the notification area, its window not shown.
4. In Task Manager › Startup apps, disable it. Open Settings again. Pass: Start at login shows off.

### 5.7 Quick entry

1. With another app in front, as Notepad, press Ctrl+Alt+Space. Pass: quick entry opens in front of it, and takes what you type.
2. Add the German keyboard (Settings › Time & language › Language & region), switch to it with Win+Space, and press AltGr+Space in Notepad. Record whether quick entry opens, and whether Notepad loses a character it would have typed.

### 5.8 The company's certificate authority

Make a test root with `New-SelfSignedCertificate -Subject "CN=Surogate test company root" -KeyUsage CertSign -TextExtension @("2.5.29.19={critical}{text}ca=1") -CertStoreLocation Cert:\CurrentUser\My`, export it with `Export-Certificate`, and remove it from `Cert:\CurrentUser\My`. Then, each in turn:

1. In an elevated PowerShell: `Import-Certificate -FilePath <the .cer> -CertStoreLocation Cert:\LocalMachine\Root`.
2. By Group Policy: `gpedit.msc` › Computer Configuration › Windows Settings › Security Settings › Public Key Policies › Trusted Root Certification Authorities › Import; then `gpupdate /force`.
3. Where the PC is enrolled in Intune: a trusted certificate profile to the computer's Root store.
4. As the user, in a PowerShell that is not elevated: `Import-Certificate -FilePath <the .cer> -CertStoreLocation Cert:\CurrentUser\Root`, answering Windows's question Yes.
5. With the root in `Cert:\LocalMachine\Root` as in 1: in `certlm.msc`, open its Properties and choose "Disable all purposes for this certificate"; then, again, "Enable only the following purposes" with Server Authentication unticked.
6. With the root in `Cert:\LocalMachine\Root` as in 1, its purposes as they were: import it into `Cert:\LocalMachine\Disallowed` too (certlm's Untrusted Certificates), then instead by Group Policy (Public Key Policies › Untrusted Certificates).

After each, run `native\windows\target\release\surogate-helper.exe trust | Select-String -Pattern "BEGIN CERTIFICATE" | Measure-Object` before and after, and `certutil -store root "Surogate test company root"`.

- Pass: the count goes up by one after 1, 2 and 3, and **not** after 4, nor in either step of 5, nor in either step of 6.
- Take each root out again (`certlm.msc` and `certmgr.msc`, or `Remove-Item` on its `Cert:\` path).
- **Send back:** the counts.

### 5.9 The person's browsers

1. Install Chrome for this user alone (its installer run from a PowerShell that is not elevated puts it in `%LOCALAPPDATA%\Google\Chrome`). Open Settings › Browser. Pass: Google Chrome and Microsoft Edge are listed, and neither says its signature does not check out.
2. Take Edge in Settings › Browser. In a chat, ask the agent to open a page in the browser. Pass: Edge opens the page, in a window of its own profile, and the agent says what the page shows.
3. In Settings › Browser, Custom…: the dialog offers programs (.exe) alone. Pick `C:\Windows\notepad.exe`. Pass: it is refused, and Settings says why.

### 5.10 The development app's removal

```
test\win32\dev-app.ps1 -Remove
```

- Pass: the app quits (where a thread is working, it asks first: answer it), and the script says it removed the shortcut.
- Pass: Task Manager › Startup apps no longer lists it; the Start menu has no "Surogate Dev"; `surogate://open?url=<the agent's address>` from the Run box starts nothing.
- **Send back:** the script's output, and what Startup apps listed in 5.6.
