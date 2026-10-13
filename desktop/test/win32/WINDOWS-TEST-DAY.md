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

After each, run `native\windows\target\release\surogate-helper.exe trust | Select-String -Pattern "BEGIN CERTIFICATE" | Measure-Object` before and after, and `certutil -store root "Surogate test company root"`.

- Pass: the count goes up by one after 1, 2 and 3, and **not** after 4.
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
