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
computer or its private networks. The browser dies with the host. Its tests drive the real
browser, headed, so they run apart from your session: `test/isolated.sh` gives them a display
of xvfb's own, an X11 session (else the browser finds your Wayland compositor), a dead session
bus (else it reaches your keyring), and a scratch home, XDG folders and temp folder. They
are behind `SUROGATE_BROWSER_TESTS=1`, which it sets, and refuse to launch a browser without
all of it:

    npm run test:browser -- test/browser-host.test.ts test/browser-client.test.ts
    npm run build && sh test/isolated.sh npx vitest run -c vitest.e2e.config.ts test/e2e/browser.e2e.ts

They launch Chrome where it is installed, else Edge; `SUROGATE_TEST_BROWSER` names another:

    SUROGATE_TEST_BROWSER=/opt/microsoft/msedge/msedge npm run test:browser -- test/browser-host.test.ts

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
- the helper is not root's own program: it is another user's, its group or others may write it,
  or no one may run it. At any other mode it is taken, read-only or closed to others too;
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

A different line, with two ways on, is said where the release key changed while this computer
took no update:

    <base>/desktop/latest.json is not signed by Surogate's release key, as this computer has it. The key may have changed since this computer's last update: run Surogate Desktop's install script with --version of the first release that lists the new key, or remove Surogate Desktop with --uninstall and install it again

A key is changed over two releases: one that the old key signs and that lists the new key beside
it, and then the first that the new key alone signs. A computer that never installed the first
of the two knows the old key alone, and takes nothing that the new one signs. Either install
that first release, which its own key signed, and then update as ever:

    curl -fsSL https://surogate.ai/desktop/install.sh | bash -s -- --version <that release>
    curl -fsSL https://surogate.ai/desktop/install.sh | bash

or remove Surogate Desktop and install it again, as above. Which release that is, the line
cannot say: it is the one whose notes say that the release key changes.
