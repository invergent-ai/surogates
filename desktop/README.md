# Surogate Desktop

The desktop app that lets an agent's file, terminal and browser tools work on a
folder of your computer, while the agent's reasoning stays on the server.

The device-link protocol is the module docstring of `surogates/devices/link.py`.

    npm install
    npm run electron:install   # Electron 44 ships no postinstall: this fetches its binary and sets its fuses
    npm test                   # builds first; the first build fetches the app's own node into bin/
    npm run typecheck

The shell, in development:

    npm start                  # builds, makes the agent disk, then runs Electron
    npm run test:e2e           # the shell end to end, under xvfb-run, with its sandbox on

Its state lives under `$XDG_DATA_HOME/surogate` (`~/.local/share/surogate`):
Electron's own data, the agent, the device's credential and journal, and the
window's place. No chat's folder may hold it.

The VM sandbox's guest (spec, Section 11) is the `guest` stage of
`images/sandbox/Dockerfile`, which shares its `tools` stage with the cloud
sandbox, and the guest agent in `src/guest/`. Docker builds the image, without
root:

    ../images/guest/build.sh      # into images/guest/out

It leaves `rootfs.img.zst` and `vmlinuz.zst`, the files a release publishes;
`rootfs.img` and `vmlinuz` unpacked beside them, for the VM tests; and
`manifest.json`, the image's key and each file's size and sha256, unpacked and as
downloaded, which the app's tarball carries.

The VM tests boot it under QEMU and KVM (`/dev/kvm`, `qemu-system-x86`,
`virtiofsd`), with the agent disk `vm/agent-disk.sh` makes from `dist/`.
`SUROGATE_VM_IMAGE` names another image folder, and `SUROGATE_VM_KVM` a device
that does not exist, to run them emulated.

    npm run build
    SUROGATE_VM_TESTS=1 npx vitest run test/vm/

The app runs a chat's commands and background processes in that VM, each command
in a cgroup of its own, its manager in a utility process of its own, with the VM's
sockets in a folder of `$XDG_RUNTIME_DIR/surogate` that is its state's own.
Each chat's folder joins the running VM while the chat works and leaves it once the
chat lets it go, served uncached, with its protected files read-only there.
The guest has no network device: a command's connections go through its chat's
proxies in the guest to the app's host proxy, which lets the package hosts through,
refuses this computer's own addresses, and asks the chat's user about the rest.

Nothing runs a command on this computer. srt (`@anthropic-ai/sandbox-runtime`) wraps
only the file helper (`src/hosts/host.ts`, `src/hosts/policy.ts`), which does the file
tools and ripgrep in a sandbox that shows it the system, the app and the chat's folder,
and connects to no host.

The file hosts and their helpers run on the app's own node, `bin/node`: Node 22 for
linux-x64, pinned by hash and stripped (`scripts/node.sh`, which the build runs).

Electron never runs as Node: its RunAsNode fuse is off (`scripts/fuses.mjs`; this
package's own Electron keeps the inspector, which the end-to-end tests drive).

An installed app downloads the image its `manifest.json` names from where it was
installed from, and checks it by those hashes. A development build boots the image
built here (or `SUROGATE_VM_IMAGE`'s) with the agent disk `npm run agent-disk` makes
from `dist/`, or downloads it as an installed app does when `SUROGATE_INSTALL_JSON`
names an install record, as the tests do. A packaged app takes neither
`SUROGATE_VM_IMAGE` nor `SUROGATE_VM_KVM`. The shell's tests that run a command boot
it too:

    SUROGATE_VM_TESTS=1 npm run test:e2e

The agent's browser is one installed here: Chrome or Edge from its `.deb` (Brave, Vivaldi
and a Chromium that is not the Snap are detected, not verified), chosen in Settings →
Browser. The browser host, in a utility process of its own, launches it headed with its own
sandbox, over a pipe, in a profile of the agent's own under `browser-profiles/` in the state
root, and sends its every request through a pinning proxy that reaches nothing on this
computer or its private networks. The browser dies with the host.

A file a page downloads waits in the browser host's own temporary folder, under
`browser-profiles/` too, and is then saved under `Downloads` in the chat's folder through
the chat's file host: under its own name, made one plain visible file name, or the next free
one (`report (2).txt`), never over a file that is there. In a chat that asks every time you
are asked first, by a prompt that says a page downloaded it; in a chat that works freely it
is saved without asking. A page can start a download by itself, so there a file of a page's
choosing can land in `Downloads` unasked, where a tool that searches the folder's tree, as a
test runner does, finds it. The agent is told where the file was saved, or why it was not
(over 50 MiB, not finished, denied), with its next answer that says what the page did: a
navigation's, the mouse's, the keyboard's or an upload's. One answer carries at most twenty
notes of what its downloads came to and twenty of what else its pages did; more than that is
not told. A download of the agent's that begins and never ends is dropped, and told as not
finished, once no byte of its own has come for a minute. That drops one whose site is silent
for a minute part-way as well, though it would have gone on: a report that is made while it
is sent meets this each time it is tried. One of the agent's that keeps coming is stopped by
what the agent's own downloads may have staged together, those on their way and those that
ended and wait to be saved: 400 MiB, eight files as large as one that can be saved. Past
that, every download of the agent's still on its way is stopped and what it had staged
removed, and one that ends then is not handed on; the agent is told of each that its own
downloads were together too large to save, and that it may start again one that was not the
large one. What is staged is looked at once a second, so the agent's own can pass the
400 MiB by what comes in a second. A download of your own counts for nothing in any of this:
it is never stopped for its size or its silence, and the agent is told nothing of it. One of
yours with no end goes on until you stop it in the browser or the browser closes.

While the agent drives, a page gets a file only when the agent uploads one: up to ten files
of the chat's folder, 50 MiB in all, read through the chat's file host and given to the file
input the page last asked for a file for, by name, type and content, never a path. A file
input opens no file dialog then. In a chat that asks every time you are asked first, by a
prompt that names each file and the site that gets them: the one of the frame the input is
in, which can be another than the tab shows. In a chat that works freely nothing is asked.

You can take the browser over from the chat. Once the agent has opened an address in it,
the chat's browser pane says "The browser is open on this computer", with Show browser and
Take over (in Surogate Desktop on that computer: elsewhere it only names the computer).
Show browser brings the chat's page to the front of the browser's window; whether that
window comes above your others is your window manager's call. Take over shows the page too,
and makes the agent wait. It is the agent's one browser here, so its browser tools answer
that you have it in every one of its chats; what it was doing in a page is cut short, a
download of its own still on its way is dropped, which it is told, and its open browser
prompts close. A file input that asked before is given nothing after: the page must ask
again. One upload can still reach its page: a step that was already sent when you took the
browser over gives the files if the page takes it within a quarter of a second, and the
agent is then told that the page has them. While you have the browser:

- a page's own questions wait for your answer;
- a file input you click opens the browser's own file chooser; in a page the agent had open,
  only once that page has answered and then been quiet for five seconds: until then, and in
  one a frame of which is stuck, your click on a file input opens nothing. A page that keeps
  asking for a file by itself is heard, and given nothing by that: it cannot keep what the
  agent's last click gave it, and so opens no window, fills no screen and writes no
  clipboard under your hand;
- a download that comes in a chat's page is yours: you are asked before it is saved, in
  either mode, and the agent is told nothing of it. One you start that comes only after you
  have handed the browser back is not covered by this: the paragraph on the minute after a
  hand back, below, says what the agent is told of it then, and after that minute it is
  taken for the agent's own, saved as the agent's are and told to it by name.

One limit of that last rule. Where the agent had clicked a link whose site had not answered
when you took the browser over, the browser does not say that its navigation stopped. If
you then download that same address yourself by a link marked `download`, your download is
taken for the agent's answer: it is dropped without asking you, and the agent is told its
name, as of a download of its own that your take-over interrupted. It was measured 8 s
after a take-over, and nothing bounds it in time while the site leaves the first request
open. It costs you that one download; a second click works.

Hand back, in the pane, asks in a window of the desktop's own, "Hand the browser back to
<agent>?". Keep control is its default. Nothing answers it, Keep control neither, until half
a second has passed since it showed and since the last key or press it received: what you
were typing into the browser when it opened answers nothing and moves nothing, and a key
held down never does. Every prompt of the desktop's is held back so, with each of its
buttons and Escape. The browser is handed back from the chat it was taken over from, or
from any chat of the agent's once that one is deleted; where that chat is gone, Settings →
Browser says so and offers Hand back itself, through the same confirmation, since no chat
may be left with a browser pane to hand it back in. Nothing the agent's page does by itself hands the
browser back, shows it or opens Settings: each needs a click of yours in that page. It can
take the browser over by itself, with no click of yours, and that does all your own
take-over does, not only stop the agent: the agent's downloads on their way are dropped; no
file input that had asked, in any of its chats, is given a file after; each download that
comes meanwhile is asked about as yours, as one downloaded while you had control, and the
agent is told nothing of it; and five seconds on, a file input in the agent's pages opens
the browser's own file chooser at a click. It stays so until you hand the browser back.
The pane tells the chat of a take-over and of a hand back, and at the hand back that chat's
agent goes on; if the chat could not be told, its agent waits until you write to it, and
the pane says so where it can. The agent's other chats are not told of either. A take-over
lasts until you hand the browser back or quit the app, which closes the browser. A hand
back gives no page anything a click of yours would: no page can open a window, fill the
screen or ask for a file on it.

For a minute after a hand back, a download the browser shows no request for (a `download`
link to its page's own site, a `blob:` or a `data:` address) is still asked about as yours,
since you may have started it; the agent is told where it was saved, or only that it was
not. In a tab you opened yourself, a download that is yours by these rules is saved, asked
the same way, in the chat you took the browser over from, unless that chat was deleted; any
other download there is cancelled.

Settings → Folders and permissions lists each chat that uses the browser here, with Take
back: you are asked again, as at its first use, before the agent next opens, reads or acts
in a page for that chat. The tabs it has open stay open.

The browser's tests drive the real browser, headed, so they run apart from your session:
`test/isolated.sh` gives them a display of xvfb's own, an X11 session (else the browser
finds your Wayland compositor), a dead session bus (else it reaches your keyring), and a
scratch home, XDG folders and temp folder. They are behind `SUROGATE_BROWSER_TESTS=1`, which
it sets, and refuse to launch a browser without all of it:

    npm run test:browser -- test/browser-host.test.ts test/browser-client.test.ts
    npm run build && sh test/isolated.sh npx vitest run -c vitest.e2e.config.ts test/e2e/browser.e2e.ts

Where a test acts as the person at the browser, with a click or a key, it sends X events to
that display (`test/x-user.py`), which needs `python3`, libXtst, `xwininfo` and `xprop`. The
last of `browser.e2e.ts`'s tests through the app runs all of the above together: a page's
download saved and told, a file of the folder given to a page, the browser taken over, a
download its user makes there asked as theirs, and the browser handed back. It saves and
reads through the chat's file host, so it passes only where srt's sandbox starts.

They launch Chrome where it is installed, else Edge; `SUROGATE_TEST_BROWSER` names another:

    SUROGATE_TEST_BROWSER=/opt/microsoft/msedge/msedge npm run test:browser -- test/browser-host.test.ts
