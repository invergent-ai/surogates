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
   cat /run/surogate/rule /run/surogate/scope; ls -l /run/surogate/ports
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

- Get milestone B's x64 guest image into `images\guest\out` (`gh run download` of the image, initrd and agent-disk artifacts), so nothing is built in a Linux container. Point the measurements at it: `$env:SUROGATE_VM_IMAGE = "<repo>\images\guest\out"`, and `$env:SUROGATE_VM_AGENT_DISK` at the agent disk with its VHD footer.

### The measurements, in order

Each is a vitest file under `desktop\test\vm\win32\measure`, run one at a time by its own config, each writing `test\vm\win32\measure\out\<name>.json` with this PC's CPU, edition and build. Run them in this order:

```
cd desktop
npx vitest run -c test/vm/win32/measure/vitest.config.ts identity
```

1. **`identity` — whose rights the Plan 9 server serves a folder with. THE SECURITY STOP, FIRST.** A share added with your own token and `UseShareRootIdentity`: the guest reads your own file, cannot read a subfolder only an administrator can, and cannot write a file you may only read; another user's folder is refused at the service, and if forced, is unreadable in the guest. **A FAIL is a security finding: stop and report it before anything else. Folder sharing stays off, and the app's own Plan 9 server (C3b) is built.** Set `SUROGATE_OTHER_USER_FOLDER` to another user's folder, and run this on a standard account and on an administrator's. The admin-only subfolder is set with `icacls`; run elevated to set it, or have an administrator set it beforehand on the standard account.
2. **`boot`** — the guest booted directly by HCS with its initrd, three SCSI disks, the rule attached, the console on COM1; median of three, under 1.5 s.
3. **`ports`** — the agent's three ports relayed through the pipes: a round trip, a 261 KB result whole, and what an ended connection does.
4. **`disks`** — the SCSI addresses `:0:0:0`, `:0:0:1`, `:0:0:2`, the read-only flags, and discard on the sessions disk.
5. **`owners`** — the uid, gid and mode the guest sees; who owns what the guest makes; links, FIFOs, xattrs, a rename over. **And the security check: a junction and a symbolic link in the folder that lead outside it — a read through either that reaches outside is a security finding.** The names NTFS resolves to another are recorded.
6. **`readonly`** — a place's folder shared read-only: the guest's root cannot write it, however it remounts. **A write that reaches the folder is a security finding.**
7. **`shares`** — adding and removing shares while the guest runs: latency, 40 cycles with two roots, and a removal while a process holds the mount.
8. **`coherence`** — a host write then a guest read, 1 000 times; `git status` on 50 000 files after the host changed 10.
9. **`mmap`** — a shared map of a file in a share (EINVAL on 9p is the safe answer).
10. **`folders`** — Documents in OneDrive, an exFAT USB drive, a ReFS Dev Drive, a `subst` drive, a compressed or encrypted folder; each by its environment variable (`SUROGATE_ONEDRIVE_FOLDER` and the rest). A network drive's refusal at binding is milestone D's.
11. **`lifetime`** — `surogate-vm.exe` killed, the manager killed, the app ended, the service killed and restarted: look for a `vmmem` or `vmwp` left.
12. **`sleep`** — by hand: start it, put the PC to sleep for about a minute, wake it, and read what the command saw across the sleep. Do this on a modern-standby laptop and an S3 desktop, and a hibernate.
13. **`memory`** — `vmmem`'s working set idle, at a 1.5 GiB peak, and each minute after, for `MEASURE_MINUTES`.
14. **`beside`** — the guest boots and runs a command beside WSL2, Docker Desktop, VirtualBox, VMware, Memory Integrity, Credential Guard and Windows Sandbox; set each up, then run it.
15. **`defender`** — the unpack, the boot and an `npm install` in a share, with real-time protection on, and with a hand-set exclusion of the data and share folders; compare.
16. **`requirements`** — on a PC with Virtual Machine Platform off, with virtualization off in the firmware, and on a VM without nested virtualization: each case's HCS answer and the backend's words.

### The VM suite and the escape probes (C6)

With the backend on win32 and the development service running:

```
$env:SUROGATE_VM_TESTS = "1"
npx vitest run test/vm
```

Then the escape probes by hand: `ip link` shows only `lo`; `~/.ssh`, another root's folder and its processes are absent; a protected write fails; the network goes only through the proxy; a junction to `%USERPROFILE%` in the folder is refused by the file host. Record the counts.

**Send back** the `out` folder, the VM suite's counts, and the console logs each step names. **`identity`, `owners`'s link check and `readonly` are security checks: a FAIL in any is a finding — stop and report it.**
