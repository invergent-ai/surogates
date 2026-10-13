# Surogate Desktop on a Windows PC: the test day

This runbook tests Surogate Desktop's Windows line on a real PC. Run it on Windows 11 with Virtual Machine Platform on, in a fresh local administrator account made for this, never your own.

Each step says what a pass looks like. When something fails, keep going where you can, and send back what the step says to send.

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
