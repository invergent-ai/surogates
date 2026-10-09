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
That script needs `mke2fs` and `debugfs` (e2fsprogs) and `fakeroot`
(`apt install fakeroot`): the disk's files are root's, and it is made without
root and in no user namespace, which a stock Ubuntu 24.04 refuses. `npm start`,
`npm run test:e2e` and `scripts/package.sh` run it too. At one
`SOURCE_DATE_EPOCH`, and with the same e2fsprogs, the same build gives the same
disk, and the same tarball.
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

One private destination is carried: a server the agent started in a chat's sandbox, at
`http://localhost:<port>/` (or `127.0.0.1`, or `[::1]`), once that chat's user has allowed
the port in the desktop's own prompt. The proxy dials nothing on this computer for it: it
knocks at a socket of the VM manager's, in the VM's runtime folder, which carries the
connection into that chat's sandbox and no other. A port is one chat's at a time: the
prompt names the chat by its folder, and says which chat has the port when it is another's.
Settings → Folders and permissions lists each port under its chat, with Take back. It goes
then, when the chat's browser is taken back, or when the chat is deleted, and what the
browser had open to it ends at once. A page's WebSocket to such a port is carried too, when it
is a page of an allowed port that opens it, so a development server's page reloads by
itself; https and `wss://` are not. A chat's sandbox takes 160 connections from the browser
at once: past them the one that has carried nothing for longest ends, which a page hears as
a connection lost. A tab that goes to a port not allowed is shown a short page of the
proxy's own, which says that the chat's agent opens it and that the ports allowed are
listed in Settings: nobody is asked from the browser.

That proxy listens on this computer's loopback, where any program here can connect to it. A
public site it carries for whoever asks, since that program reaches the site by itself.
Whatever else it answers, it answers only to the browser it launched: the browser signs in
to it with a secret made at each launch, given to the browser over its pipe and kept in
memory alone, on no command line, in no environment and in no file. Without that sign-in the
answer is 407 and nothing more: so it is for a chat's allowed port, which no other program
here reaches through the proxy, and for the answer a launch proves its proxy with, so a
browser that did not take the sign-in is not used. Should a running browser
stop signing in, it shows its own proxy sign-in prompt for such a request and nothing is
carried. Measured on Chrome; not yet on Edge, nor with a second user of this computer at the
proxy's port.

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
400 MiB by what comes in a second. Each of the agent's is counted by the file the browser
names for it as it begins; one the browser has not named two seconds on cannot be told from
another's, so it is stopped, and the agent told that the browser did not say which file it
was. Nothing else in the folder is counted. A download of your own counts for nothing in
any of this: it is never stopped for its size or its silence, and the agent is told nothing of it. One of
yours with no end goes on until you stop it in the browser or the browser closes. The one
download that may be either's, in the minute after a hand back, is said below.

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
a second has passed since it showed, and no key or press answers it that comes within half
a second of the one before: what you were typing into the browser when it opened answers
nothing and changes no choice, and a key held down since before it opened never does. Tab
and Shift+Tab move nothing in that first half second, nor within half a second of any other
key, so what you type leaves the keyboard on the button that changes nothing; after a pause
they move it, one after the other as fast as you like. A modifier by itself holds nothing back, and a
key you press on a button and hold answers when you let it go. A button pressed with no key
and no press, as a screen reader presses one, answers once half a second has passed with no
key and no press at all; no button is marked unavailable meanwhile. Only the confirmation's
own page, a file of the app's, can send that. Every prompt of the desktop's is held back so,
with each of its buttons and Escape. The browser is handed back from the chat it was taken over from, or
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
The pane tells the chat of a take-over and of a hand back. A hand back you confirmed, of
the browser taken over from that chat, gives that chat's agent a turn, in which it reads
that the browser is handed back, and the pane says the agent goes on. Where the chat can
take no turn then (one is under way, you stopped the chat or it failed, your limit is
spent), the browser is the agent's again all the same, the agent does not go on by itself,
and the pane says to write to it; so it does where the chat could not be told. A hand back
made for a chat that is deleted wakes no agent either: write to the agent to go on. Nor
does the app's end while you had the browser: the chat is told, when it is next opened,
that nobody holds it, which is no hand back. A take-over is told to the chat it was made
from alone. A hand back is also told to the agent's other chats on this computer that still
said you had the browser, and wakes none of them. A take-over lasts until you hand the
browser back or quit the app, which closes the browser. A hand back gives no page anything
a click of yours would: no page can open a window, fill the screen or ask for a file on it.

For a minute after a hand back, a download the browser shows no request for (a `download`
link to its page's own site, a `blob:` or a `data:` address) is still asked about as yours,
since you may have started it; the agent is told where it was saved, or only that it was
not. Until the agent has acted again after the hand back, such a download cannot be its
own: it is yours, counted nowhere and never stopped. Once the agent has acted again, nothing
tells one you start from one the agent's own script or click starts, so it is counted and
timed as the agent's are, from its first byte: stopped with the agent's past 400 MiB, or
after a minute with no byte, and the agent told only that it was not saved. A download you
start in the agent's browser in that minute, while the agent works in it, can so be stopped;
take the browser over first, and it is yours whatever its size. In a tab you opened yourself, a download that is yours by these rules is saved, asked
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

## The acceptance VMs

`test/acceptance.test.ts` installs the release this package builds on Ubuntu 24.04 and 26.04,
each from its cloud image booted under QEMU, with the install script and from a server on this
computer, as a person installs it; starts the app, updates it, cuts the power, rolls it back and
removes it. It is behind `SUROGATE_ACCEPTANCE_TESTS=1`, needs `/dev/kvm`, QEMU, `qemu-img`,
`ssh` and `ssh-keygen`, a build, and the Ubuntu archive for the VMs' own apt, and takes about
40 minutes:

    npm run build
    SUROGATE_ACCEPTANCE_TESTS=1 SUROGATE_ACCEPTANCE_IMAGES=~/.cache/surogate-scratch/cloud-images npx vitest run test/acceptance.test.ts

The two cloud images are expected in the folder that `SUROGATE_ACCEPTANCE_IMAGES` names, as
`noble.img` and `resolute.img`: on the computer the desktop is built on, that folder is
`~/.cache/surogate-scratch/cloud-images`, on its disk and not in its temporary folder, which a
restart empties. The test downloads neither, and says so where one is not there. It only reads
them: each VM's disk is a file of its own over the image. These two lines, in that folder, bring
them back:

    curl -fLo noble.img https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img
    curl -fLo resolute.img https://cloud-images.ubuntu.com/resolute/current/resolute-server-cloudimg-amd64.img

A release is a tarball (`scripts/package.sh`), which is read where no key is
(`release/publish.sh describe`), its manifest written and signed with the release key
(`release/publish.sh sign`, which opens no tarball), and both on the release bucket under `desktop/` with the install script
(`release/publish.sh send`). In `.github/workflows/release.yml` these are three jobs, as a job
is the boundary and a step is none: `desktop-build` makes the tarball; `desktop-describe`, which
holds no secret and runs no npm, reads it and says the app's state schema; and
`desktop-publish`, which alone holds the release key, runs no npm and opens no tarball, writes
and signs the manifest of the tag, of the build's hash and size and of that schema, and sends
the release. One thing rests on the describe job's runner alone, as the job that signs cannot
see it: that the tarball's root helper is the tag's install script. No workflow runs a test of
any of this: the tests are run by hand before a tag is pushed. `release/install.sh` installs a
release into `/opt/surogate`, and is each version's root helper for updates (`--apply`):

    curl -fsSL https://surogate.ai/desktop/install.sh | bash
    curl -fsSL https://surogate.ai/desktop/install.sh | bash -s -- --uninstall

## When Surogate Desktop says to remove it and install it again

An update, the install script and its `--version` can each stop with a line that ends "remove
Surogate Desktop with --uninstall, and install it again". The app shows the same line after
"Surogate could not install its update:". It is about two files that only root writes, and the
folder of the release they belong to:

    /opt/surogate/bin/surogate-apply-update    the helper that an update runs as root, which lists the release keys this computer trusts
    /opt/surogate/bin/release.json             its mark: the manifest of the release that the helper is of
    /opt/surogate/versions/<version>/          that release's folder, with a copy of the helper and of the manifest in it

The line means that one of them is not as an install or an update leaves it. No install, update
or rollback leaves them so, wherever it is stopped. The states below need root's own hand on
those files, a damaged disk, an install script from before the mark was written, or a removal
that was stopped before its end. Nothing short of a removal mends them, because what is damaged
is what says which release keys this computer trusts. Do what the line says:

    curl -fsSL https://surogate.ai/desktop/install.sh | bash -s -- --uninstall
    curl -fsSL https://surogate.ai/desktop/install.sh | bash

The removal takes the app away for every user of the computer. It asks before it deletes your
sign-in, your device token and your browser profiles, and chat folders stay.

The script has four such lines. These are the states each is known to be said of; the list is
of what was measured, and the script may refuse others in the same words. The first:

    /opt/surogate/bin/surogate-apply-update is not as Surogate Desktop's install leaves it: remove Surogate Desktop with --uninstall, and install it again

- the helper is gone, and a version is installed, as a removal leaves it that was stopped before
  its last step;
- the helper is a link, or a folder stands in its place;
- the helper is not root's own program that every user may read: it is another user's, its
  group or others may write it, no one may run it, or others may not read it. The app reads its
  release keys from it as its user, so one closed to others is none to the app, and the script
  takes it for none either. At any other mode it is taken, read-only too;
- the folder of the release that the mark names has lost its own copy of the helper,
  `bin/surogate-apply-update`, and the install is otherwise whole;
- a link is where that copy was;
- the same loss after a rollback, where that folder is not the running version's;
- an update was stopped half way, between the mark's writing and the helper's, and the update's
  folder has lost its program, `surogate`;
- an update was stopped so, and the `release.json` in its folder is not root's own file at mode
  0644;
- an update was stopped so, and its own copy of the helper is another user's, or one that its
  group or others may write, or has a set-id bit;
- an update was stopped so, and its folder has lost its own copy of the helper;
- the mark names an older release than the helper is of.

The second:

    /opt/surogate/bin/surogate-apply-update lists no release key: remove Surogate Desktop with --uninstall, and install it again

- the helper lists no release key, or its list is not written in the one form that a list has
  (the form is said at the list, in `release/install.sh`). A release job writes and signs no
  manifest for such a script.

The third:

    /opt/surogate/bin/release.json does not say which release /opt/surogate/bin/surogate-apply-update is of: remove Surogate Desktop with --uninstall, and install it again

- the mark is gone;
- the mark is not root's own file at mode 0644, or names no release.

The fourth, where nothing is installed yet and something that is no file, a link or a folder,
stands where the mark goes:

    /opt/surogate/bin/release.json is not as Surogate Desktop's install leaves it: remove Surogate Desktop with --uninstall, and install it again

One state needs a removal and is told nothing of one: a folder where the link
`/opt/surogate/current` should be. An update, and the install script, then end "stopped, as this
step failed: mv -T ...", with the new version's helper and mark already in place. Remove
Surogate Desktop and install it again there too.

An update that was only stopped half way is none of these: the next update, or the install
script, finishes it. And a line that ends "run Surogate Desktop's install script again" means
what it says, where the base's newest release is the installed one or a newer: the install
script then unpacks again a version's folder that has lost its program or its `release.json`.
Where the installed version is newer than the base's newest, the script keeps it as it is
("kept the installed ..., newer than the server's ..."), and mends nothing of it.

A different line is said of a release that no release key of this computer's has signed. The
install script says it, of the base's newest release:

    <base>/desktop/latest.json is not signed by a release key this computer trusts: nothing was installed, and Surogate Desktop stays at its version. If Surogate's release key has changed since this computer's last update, run Surogate Desktop's install script with --version of the release that brought the new key

and the app says it in the same words, in its sidebar: of the newest release, once that has stood
for a day of failed checks, and of an update that its own helper refuses for it:

    Surogate's newest release is not signed by a release key this computer trusts: nothing was installed, and Surogate Desktop stays at its version. If Surogate's release key has changed since this computer's last update, run Surogate Desktop's install script with --version of the release that brought the new key
    Surogate could not install its update: the release's manifest is not signed by a release key this computer trusts: nothing was installed, and Surogate Desktop stays at its version. If Surogate's release key has changed since this computer's last update, run Surogate Desktop's install script with --version of the release that brought the new key

Nothing was installed, and Surogate goes on at the version it has. The line cannot tell why the
release is not signed: a release from after a change of the release key, on a computer that took
no update while the key changed, reads the same as one that someone else put on the server.

A key is changed over two releases: one that the old key signs and that lists the new key beside
it, and then the first that the new key alone signs. A computer that never installed the first
of the two knows the old key alone, and takes nothing that the new one signs. What mends it is
that first release, which this computer's own key signed, and then an update as ever:

    curl -fsSL https://surogate.ai/desktop/install.sh | bash -s -- --version <that release>
    curl -fsSL https://surogate.ai/desktop/install.sh | bash

Which release that is, the line cannot say: it is the one whose notes say that the release key
changes. That way is safe whatever the cause, since this computer's own keys check what it
installs.

Removing Surogate Desktop and installing it again also ends the line, and is not safe whatever
the cause: a removal forgets the keys this computer trusts, and the install after it trusts the
keys that the server's install script lists. Do it only once Surogate's release notes say that the
release key has changed, read somewhere other than the server the line names.

A rollback (`--version`) to a release that is not signed is told so plainly, with no way on. And
a release that a key signed which Surogate has since retired is told as that: "... is signed by a
release key that Surogate has retired, which this computer no longer trusts".

