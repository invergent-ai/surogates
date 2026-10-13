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
gh run download 38293661923 -R invergent-ai/surogates -n desktop-guest-arm64 -D images/guest/out
cd desktop && npm ci
```

- The guest image is the arm64 one built from this branch. With it in `images/guest/out`, step 3 doesn't build the image itself, which takes about an hour.
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

- `prepare.sh` builds the app's code and `native/out/surogate-vm`, signed ad hoc. In a container, it makes `dist/agent.img` and `dist/agent-measure.img`. Its last lines say "ready".
- The check passes with 27 tests, 0 failures, and `{"supported":true}`.

Then run each measurement in this order, as `npx vitest run -c test/vm/darwin/measure/vitest.config.ts <name>`. Each prints `MEASURE …` lines and a `PASS` or `FAIL` for each check, and writes `test/vm/darwin/measure/out/<name>.json`.

1. **`boot`:** the first real boot. Pass: the guest says hello in under 1.5 s at the median of three, and the rule attached 15 of 15. If it fails, its words name the reason.
2. **`escape`:** the guest's walls, as a session meets them. Pass:
   - only `lo`, and no host `~/.ssh`;
   - no other root's folder or processes;
   - `.git/hooks/pre-commit` and `.git/hookſ/pre-commit` refused, with nothing reaching `.git/hooks`;
   - `.vscode` refused.

   **A FAIL of a protected write is a security finding: stop and report it before anything else.**
3. **`ports`:** the guest sees `ai.surogate.control`, `ai.surogate.net` and `ai.surogate.inbound`, and a 261 KB result comes back whole.
4. **`disks`:** vda, vdb and vdc in that order, read-only 1, 1, 0, and vdc 32 GiB.
5. **`shares`:** for each of `multiple` and `single`, every root reads its own folder and no other. If only `single` passes, say so: the backend then switches layout.
6. **`coherence`:** 0 stale reads of 1,000, the large file seen whole, and `git status` sees the 10 changes.
7. **`owners`:** the guest sees your uid, a session runs as it, and you own what the guest makes. Every pair of names APFS takes as one is folded as one. A FAIL names the alias missed.
8. **`mmap`:** recorded only.
9. **`descriptors`:** all 150,000 files found, 1,000 read, git commits them, and no EMFILE.
10. **`readonly`:** the guest's root can't write the read-only share, even after remounting it read-write, and the folder on the Mac is unchanged.
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

1. **The helper:** `cd native/helper && swift test && cd ../..`. Pass: "Executed 13 tests, with 0 failures".
2. **The company CA:** `test/darwin/admin-trust.sh --keep`, which asks your password.
   - Pass: it prints `{"ssl":true,"smime":false,"denied":false}` and "Node reached https://localhost:<port> …: trusted".
   - Open that URL in Chrome and Safari: no warning. Ctrl+C removes the test roots.
   - Then install a test root through a configuration profile, run `native/helper/.build/surogate-helper trust`, and note whether that root is listed. Pass: it is.
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

Run steps 0 to 5 again first, on the branch's newest head, but leave Surogate Dev in place: skip step 5's clean-up. Then these, in order. Each says what a pass looks like; send back every one that fails, with its words.

### The shell and the file host

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
6. **A company root from a configuration profile.** Install the test root's `.mobileconfig` by a double-click, as on the first day, and run `native/helper/.build/surogate-helper trust`.
   - Pass: the root is listed, as PEM.
   - Then remove the profile (System Settings → General → Device Management), and run `test/darwin/admin-trust.sh --user`. macOS asks for your password in its own window, once for each test root.
   - Pass: it prints `{"ssl":true,"smime":false,"denied":false}` and ends "The user's trust domain is read as it should be".
   - Note any prompt `surogate-helper trust` itself raised.
7. **Removing the dev bundle.** Turn on Settings → Start at login, leave Surogate Dev running, and run `test/darwin/dev-bundle.sh --remove`.
   - Pass: it prints "Took out its login item", then "Removed …".
   - `pgrep -fl "Surogate Dev.app"` prints nothing.
   - System Settings → General → Login Items no longer lists Surogate Dev.
8. **The spelling dictionary.** Make the bundle again with `scripts/mac-build.sh && test/darwin/dev-bundle.sh`, and start it from Terminal with `"$HOME/Applications/Surogate Dev.app/Contents/MacOS/Electron"`.
   - Pass: its output has no `ENOENT` for `dictionaries/en-US-10-1.bdic`. Quit it with ⌘Q.
