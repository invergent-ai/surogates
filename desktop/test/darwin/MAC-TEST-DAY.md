# Surogate Desktop on a Mac: the test days

This runbook tests branch `test/macos-cde`: master with the macOS VM backend, the file host under Seatbelt, and the shell and browser joined. Run it on an Apple-silicon Mac with macOS 14 or later.

Steps 0 to 5 are the first day's. The second day runs them again, then the checks under "Second day" at the end.

Work in a fresh macOS user account made for this (System Settings › Users & Groups, an administrator), never your own. Everything below runs as that user.

Each step says what a pass looks like. When something fails, keep going where you can, and send back what the step says to send.

## 0. Install once

- **Xcode 16 or later**, from the App Store. Open it once and accept its licence. Then run `sudo xcode-select -s /Applications/Xcode.app`.
- **Homebrew**, then `brew install zstd gh nvm`, and `nvm install 22.14.0 && nvm use 22.14.0`. Check that `node -v` prints `v22.14.0`. Leave the PATH as Homebrew sets it, its own `bash` included: the app starts its sandbox through the system's `/bin/bash` whatever comes first.
- **Google Chrome and Microsoft Edge**, from their own sites. Open each once by hand and close it: a browser that is new, still quarantined, dies when a script starts it before its first normal launch.
- **A container runtime for the agent's disk:** OrbStack, Docker Desktop or colima, running, with arm64 Linux containers.
- **`gh auth login`**, so the guest image can be downloaded.

## 1. Get the branch and the guest image

```
git clone -b test/macos-cde https://github.com/invergent-ai/surogates.git && cd surogates
gh run download 38392091602 -R invergent-ai/surogates -n desktop-guest-arm64 -D images/guest/out
cd desktop && npm ci
```

- The guest image is the arm64 one built from this branch. With it in `images/guest/out`, step 3 doesn't build the image itself, which takes about an hour. GitHub keeps it a week after its run: once it is gone, leave `images/guest/out` empty and let step 3 build it.
- Pass: no error, and `images/guest/out` holds `rootfs.img.zst` and `manifest.json`.

## 2. The file host under Seatbelt (security first)

```
test/acceptance/macos/file-host.sh
```

- What it does:
  - builds surogate-spawn, fsat, surogate-helper, the app's rg and `bin/node`;
  - runs the file host's suites, then the macos project's files;
  - starts a host in Seatbelt on a folder in `~/Documents`, an exFAT disk image, and iCloud Drive if you're signed in;
  - races a link flip in each.
- On macOS 27, `hdiutil` can't make an exFAT image ("Operation not permitted"). The script then names the exFAT place as skipped, with `hdiutil`'s words, and goes on with the others.
- When macOS asks whether Terminal may reach Documents, or iCloud Drive, allow it. Note which you allowed.
- Pass:
  - the last line is `PASS; places skipped: …`, naming only the places you expect it to skip (iCloud Drive when you're not signed in, the exFAT image on macOS 27). Each skipped place is printed above it, with the words of what refused it;
  - `Seatbelt:` says `ok` only for the designed tries, and `load a copy of fsat in the folder`, which is recorded:
    - read, list and write the folder, and write the working folder;
    - the three hard links a landing makes: in the folder, from it into the working folder, and back;
    - load fsat, and run rg;
  - in `Seatbelt:`, `hard-link a file of the app's into the folder` and `hard-link a file outside into the folder` are refused (`EPERM`). **Either one `ok` is a security finding: stop and report it before anything else;**
  - `M8` shows `reached 0, wroteOutside 0, kept "OUTSIDE\n"`;
  - every place line shows `"reached":0` and `"left":["only-outside"]`.
- **Send back:** the `Seatbelt:` line, and the two logs the script names at its end.

## 3. The VM

```
test/vm/darwin/measure/prepare.sh
swift test --package-path native/macos && native/out/surogate-vm check
```

- `prepare.sh` builds the app's code and `native/out/surogate-vm`, signed ad hoc. In a container, it makes `dist/agent.img`, and the measurements' own agent disk, `test/vm/darwin/measure/probing/agent.img`. Its last lines say "ready".
- The check passes with 28 tests, 0 failures, and `{"supported":true}`.
- Each root runs on a uid of its own from 10000 up, as on Linux, where its folder shows it that uid. A measurement that runs a command and stops at "This chat's folder shows as root's in the sandbox, so no command runs in it", or "… as another user's …", means Apple's share did not show that uid: report it with `share-owners`' findings.

Then run each measurement in this order, as `npx vitest run -c test/vm/darwin/measure/vitest.config.ts <name>`. Each prints `MEASURE …` lines and a `PASS` or `FAIL` for each check, and writes `test/vm/darwin/measure/out/<name>.json`.

1. **`boot`:** a first boot, which makes the sessions disk, then three boots that find it there, and one whose console has the kernel's own log. Pass:
   - the three later boots say hello in under 1.5 s at their median;
   - the rule attached 15 of 15 at every boot;
   - the kernel gzipped is refused ("did not boot");
   - `surogate-vm check` says `"supported":true`, and `false` when signed without its entitlement.

   If it fails, its words name the reason, and `out/boot.json` says where the time went.
2. **`escape`:** the guest's walls, as a session meets them. Pass:
   - a session runs as a uid of its own, from 10000 up;
   - only `lo`, and no host `~/.ssh`;
   - no other root's folder or processes;
   - `.git/hooks/pre-commit` and `.git/hookſ/pre-commit` refused, with nothing reaching `.git/hooks`;
   - `.vscode` refused.

   **A FAIL of a protected write is a security finding: stop and report it before anything else.**
3. **`ports`:** the guest sees `ai.surogate.control`, `ai.surogate.net` and `ai.surogate.inbound`, and a 261 KB result comes back whole.
4. **`disks`:** vda, vdb and vdc in that order, read-only 1, 1, 0, and vdc 32 GiB.
5. **`shares`:** for each of `multiple` and `single`, every root reads its own folder and no other. If only `single` passes, say so: the backend then switches layout.
6. **`coherence`:** 0 stale reads of 1,000, the large file seen whole, and `git status` sees the 10 changes.
7. **`owners`:** a session runs as a uid from 10000 up, sees its folder as that uid's, and you own what the guest makes. Every pair of names APFS takes as one is folded as one. A FAIL names the alias missed.
8. **`mmap`:** recorded only.
9. **`descriptors`:** all 150,000 files found, 1,000 read, git commits them, and no EMFILE.
10. **`readonly`:** the guest's root can't write the read-only share, even after remounting it read-write, and the folder on the Mac is unchanged. It runs whether or not its chat's setup is refused.
11. **`tcc`:** watch for privacy prompts and note which app each names. Optionally, set `MEASURE_VOLUME=/Volumes/<a USB stick>` to include a removable drive.
12. **`network`:** if macOS asks about the local network, note the app it names, and allow it. Pass: a command reaches this Mac's LAN address through the proxy.
13. **`lifetime`:** nothing of the VM is left after `surogate-vm` is killed.
14. **`sleep`, by hand:** when it says so, sleep the Mac for a minute or more, then wake it. Pass: the command went on, and the guest's clock is within 2 s.
15. **`memory`:** about 20 minutes; recorded only.

**Send back** the whole `test/vm/darwin/measure/out/` folder.

## 4. The shell and the browser

```
scripts/mac-build.sh
test/darwin/dev-bundle.sh
```

- `mac-build.sh` builds the app and every native program.
- `dev-bundle.sh` makes and opens `~/Applications/Surogate Dev.app`.

Then:

1. **The helper:** `cd native/helper && swift test && cd ../..`. Pass: "Executed 11 tests, with 0 failures".
2. **The company CA:** `test/darwin/admin-trust.sh --keep`, which asks your password.
   - Pass: it prints `{"ssl":true,"smime":false,"denied":false}` and "Node reached https://localhost:<port> …: trusted".
   - Open that URL in Chrome and Safari: no warning. Ctrl+C removes the test roots.
   - A root from a configuration profile you install by a double-click goes in your own trust settings, not the administrator's, and the app doesn't take it: by design, as on Linux it takes a company's CA only from where an administrator alone can put it.
3. **By eye, in Surogate Dev:**
   - the window's buttons are inset top left;
   - the menu bar reads Surogate (About Surogate, Settings… ⌘,, Services, Hide, Hide Others, Show All, Quit ⌘Q), then File, Edit, View, Window, and Help with its search field;
   - the menu bar icon is monochrome and follows Appearance.
4. **Option+Space:** in TextEdit, ⌥Space opens quick entry with the keyboard in its box, and Esc hides it. Note whether a click was needed.
5. **Links:**
   - with no agent, `open 'surogate://open?url=https%3A%2F%2Fagent.example.com'` asks to connect to agent.example.com;
   - with an agent, a `surogate://open?url=<origin>%2Fchat%2F<id>` link opens that chat, whether the app is running or quit.
6. **The Dock:** ⌘W, then a click on the Dock icon brings the window back. `open -n "$HOME/Applications/Surogate Dev.app"` brings the running window forward.
7. **The Keychain:** sign in to your agent.
   - `security find-generic-password -s "Surogate Safe Storage"` finds the item, and `session.json` and `credentials.json` hold no `"plain"`.
   - Note any Keychain prompt, at sign-in and at the next start.
8. **Notifications:** first turn Focus off, and allow Surogate Dev's notifications in System Settings → Notifications: with Focus on, macOS shows none, and the app can't tell. Hide the window and let a chat ask a question. A notification appears, and clicking it opens the chat.
9. **The login item:** turn on Settings → Start at login, then log out and in. Note what System Settings → General → Login Items shows, and whether the window showed.
10. **This computer and the browser:**
    - Settings → This computer shows your Mac's Sharing name.
    - `tmutil isexcluded "$HOME/Library/Application Support/Surogate/vm"` prints `[Excluded]`.
    - Settings → Browser lists your Chrome and Edge with versions, and an app run from a mounted `.dmg` is refused.
11. **Headed tests on your own browsers:** open Chrome and Edge once by hand first, if you haven't (step 0). Then `CI=true SUROGATE_BROWSER_TESTS_MAC=1 SUROGATE_TEST_BROWSER="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" npx vitest run --project macos test/browser-host-darwin.test.ts`, then the same with Edge. Pass: 6 passed.

## 5. The whole app

In Surogate Dev, signed in to your agent:

1. Start a chat on a folder in `~/Documents`, and ask the agent to read, write and search a file there. Note any privacy prompt, and whose name it shows: Surogate, node or Electron.
2. Ask it to run a command in that folder, such as `ls -la && git status`. Pass: it runs in the guest, and sees the folder's files.
3. Create a project with a folder of this Mac, and start a thread from a proposed card. Pass: the thread works in its own copy, and its turn lands its change in the folder.

Clean up with `test/darwin/dev-bundle.sh --remove`, which quits Surogate Dev and takes out its login item first.

## What to send back

- Step 2: the `Seatbelt:` line and the two logs.
- Step 3: the whole `out/` folder.
- Every FAIL, with its words.
- Every privacy prompt and Keychain prompt you saw, and the app each named.
- Anything that looked wrong, even if no step asked about it.

## Second day

The same Mac and the same test account as the first day. First bring the checkout to the branch's newest head: `git pull` in `surogates`. Then the VM part, then the shell part. Each step says what a pass looks like; send back every one that fails, with its words.

### The VM

1. **The new guest image.** Each root now runs on its own uid, and the image loads its entropy device's driver, so the first day's image no longer boots. From the checkout's top:
   ```
   rm -rf images/guest/out
   gh run download 38392091602 -R invergent-ai/surogates -n desktop-guest-arm64 -D images/guest/out
   ```
   Pass: `images/guest/out` holds `rootfs.img.zst`, `vmlinuz.zst` and `manifest.json`.
2. **Prepare:** `cd desktop && npm ci && npm run electron:install && test/vm/darwin/measure/prepare.sh`. Pass: it ends with "ready", and `test/vm/darwin/measure/probing/agent.img` is there.
3. **surogate-vm's tests on this Xcode:** `swift test --package-path native/macos`. Pass: "Executed 28 tests, with 0 failures".
4. **The boot and its timings:** `npx vitest run -c test/vm/darwin/measure/vitest.config.ts boot`.
   - Pass: as the first day's step 3.1 says.
   - Whatever it says, keep `test/vm/darwin/measure/out/boot.json`: its "where the kernel's boot went", its timelines and its console say where the time goes.
5. **Owners on Apple's share, in both accounts.**
   - In this account (uid 502): `npx vitest run -c test/vm/darwin/measure/vitest.config.ts share-owners`. It takes a few minutes. Pass: `PASS the probe ran to its end`, and `the root's setup` says `set up`. Then `cp test/vm/darwin/measure/out/share-owners.json ~/share-owners-502.json`.
   - In the Mac's first account (uid 501). In this account, copy the prepared checkout and its Node where the other account can use them:
     ```
     mkdir -p /Users/Shared/surogate-measure
     cp -Rc ~/surogates "$(dirname "$(dirname "$(command -v node)")")" /Users/Shared/surogate-measure/
     chmod -R a+rwX /Users/Shared/surogate-measure
     ```
     Log in to the first account, and in its Terminal:
     ```
     cd /Users/Shared/surogate-measure/surogates/desktop
     PATH=/Users/Shared/surogate-measure/v22.14.0/bin:$PATH npx vitest run -c test/vm/darwin/measure/vitest.config.ts share-owners
     ```
     Pass: the same, and its `the Mac user` line says uid 501. Copy that `test/vm/darwin/measure/out/share-owners.json` to `share-owners-501.json`, then remove `/Users/Shared/surogate-measure` and that account's `~/SurogateMeasure`.
6. **Every measurement of the first day's step 3, the rest after `boot`,** back in the test account, each as `npx vitest run -c test/vm/darwin/measure/vitest.config.ts <name>`, in this order:
   1. **`escape` first.** Pass: as the first day's step 3.2 says. **A FAIL of a protected write is a security finding: stop and report it before anything else.**
   2. Then `ports`, `disks`, `shares`, `coherence`, `owners`, `mmap`, `descriptors`, `readonly`, `tcc`, `network`, `lifetime`, `sleep` (by hand) and `memory`, each with the first day's pass lines. For `owners`: a session runs as a uid from 10000 up and sees its folder as that uid's. **A write `readonly` allows is a security finding: stop and report it before anything else.**

**Send back:** the whole `test/vm/darwin/measure/out/` folder, `share-owners-502.json` and `share-owners-501.json`, and every FAIL with its words.

### The shell and the file host

First make the app again, as the first day's step 4 does (`scripts/mac-build.sh`, then `test/darwin/dev-bundle.sh`), and run the first day's step 5 again, now that commands can run in the guest, but leave Surogate Dev in place: skip its clean-up. Then these, in order.

1. **Homebrew's bash.** `brew install bash`, then check that `command -v bash` prints `/opt/homebrew/bin/bash`. With that PATH, as Homebrew leaves it, run `test/acceptance/macos/file-host.sh`.
   - Pass: no `execvp() of '/opt/homebrew/bin/bash' failed`, and the last line starts `PASS`.
2. **The macos project in file-host.sh.** In the same run, `computer-name-darwin` passes: surogate-helper is built before the macos project runs.
   - Pass: the last line names only the places you expected skipped, and on macOS 27 the exFAT image's line above it gives `hdiutil`'s words.
3. **Commands from the Dock.** Quit Surogate Dev. Start it from the Dock, or by a double-click in Finder, never from Terminal. In a chat on a folder in `~/Documents`, ask the agent to run `ls`.
   - Pass: neither the sidebar's sandbox line nor the command's answer says "Surogate's sandbox tools are missing". If the VM refuses the command, note its own words.
4. **The Dock.** With Surogate Dev running, press ⌘W, then click its Dock icon.
   - Pass: the window comes back.
   - If it doesn't, try each of these and note which brings the window back:
     - `open "$HOME/Applications/Surogate Dev.app"`, without `-n`;
     - `osascript -e 'tell application id "ai.invergent.surogate.development" to reopen'`.
5. **Settings' close button.** Open Settings, and click its close button (×) near the button's top edge. Then open a project's settings, and click its Cancel.
   - Pass: each closes at the first click.
6. **The company CA.** Run `test/darwin/admin-trust.sh`, as in step 4.
   - Pass: it prints `{"ssl":true,"smime":false,"denied":false}` and ends "The admin trust domain is read as it should be".
   - A root from a configuration profile installed by a double-click is not listed by `native/helper/.build/surogate-helper trust`. That is by design: such a profile puts its root in your own trust settings, and the app takes a company's root only from the administrator's.
   - If this Mac is enrolled in an MDM, a root from a device profile it installs must be listed by `surogate-helper trust`. Note whether it is.
7. **Removing the dev bundle.** Turn on Settings → Start at login, leave Surogate Dev running, and run `test/darwin/dev-bundle.sh --remove`.
   - Pass: it prints "Took out its login item", then "Removed …".
   - `pgrep -fl "Surogate Dev.app"` prints nothing.
   - System Settings → General → Login Items no longer lists Surogate Dev.
8. **The spelling dictionary.** Make the bundle again with `scripts/mac-build.sh && test/darwin/dev-bundle.sh`, and start it from Terminal with `"$HOME/Applications/Surogate Dev.app/Contents/MacOS/Electron"`.
   - Pass: its output has no `ENOENT` for `dictionaries/en-US-10-1.bdic`. Quit it with ⌘Q.
