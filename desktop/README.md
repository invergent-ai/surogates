# Surogate Desktop

The desktop app that lets an agent's file, terminal and browser tools work on a
folder of your computer, while the agent's reasoning stays on the server.

The device-link protocol is the module docstring of `surogates/devices/link.py`.

    npm install
    npm run electron:install   # Electron 44 ships no postinstall: this fetches its binary
    npm test                   # builds first; the first build fetches the app's own node into bin/
    npm run typecheck

The shell, in development:

    npm start                  # builds, makes the agent disk, then runs Electron with ELECTRON_RUN_AS_NODE cleared
    npm run test:e2e           # the shell end to end, under xvfb-run, with its sandbox on

Its state lives under `$XDG_DATA_HOME/surogate` (`~/.local/share/surogate`):
Electron's own data, the agent, the device's credential and journal, and the
window's place. No chat's folder may hold it.

The VM sandbox's guest (spec, Section 11) is the `guest` stage of
`images/sandbox/Dockerfile`, which shares its `tools` stage with the cloud
sandbox, and the guest agent in `src/guest/`. Docker builds the image, without
root:

    ../images/guest/build.sh      # rootfs.img(.zst) and vmlinuz into images/guest/out

The VM tests boot it under QEMU and KVM (`/dev/kvm`, `qemu-system-x86`,
`virtiofsd`), with the agent disk `vm/agent-disk.sh` makes from `dist/`.
`SUROGATE_VM_IMAGE` names another image folder.

    npm run build
    SUROGATE_VM_TESTS=1 npx vitest run test/vm/guest.test.ts

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

Until the image is delivered, the app boots the image built here (or
`SUROGATE_VM_IMAGE`'s) with the agent disk `npm run agent-disk` makes from `dist/`.
The shell's tests that run a command boot it too:

    SUROGATE_VM_TESTS=1 npm run test:e2e
