# Surogate Desktop

The desktop app that lets an agent's file, terminal and browser tools work on a
folder of your computer, while the agent's reasoning stays on the server.

The device-link protocol is the module docstring of `surogates/devices/link.py`.

    npm install
    npm test
    npm run typecheck

The VM sandbox's guest (spec, Section 11) is the `guest` stage of
`images/sandbox/Dockerfile`, which shares its `tools` stage with the cloud
sandbox, and the guest agent in `src/guest/`. Docker builds the image, without
root:

    ../images/guest/build.sh      # rootfs.img(.zst) and vmlinuz into images/guest/out

The VM tests boot it under QEMU and KVM (`/dev/kvm`, `qemu-system-x86`,
`virtiofsd`), with the agent disk `vm/agent-disk.sh` makes from `dist/`.
`SUROGATE_VM_IMAGE` names another image folder.

    npm run build
    SUROGATE_VM_TESTS=1 npx vitest run test/vm
