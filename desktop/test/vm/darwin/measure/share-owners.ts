// How Apple's share presents owners to the guest, seen from both sides, for a session to run as a uid other
// than the guest's root's on it (VZVirtioFileSystemDevice: "The framework ignores requests from guest operating
// systems to change the UID or group ID (GID) of files on the host"; what owner the guest is shown, Apple does not
// say). It records:
//   - what /proc/mounts says of the share's mount;
//   - the owner, group and mode of the share's folder, of a file the Mac made, and of files the guest made, as the
//     guest's root, uid 10000, uid 4321 and the Mac user's uid see them in the guest, and as the Mac does; how long
//     a look by root goes on showing root as the owner to another uid;
//   - whether a uid other than root's reads and writes a 0644 file of the Mac's (does the guest's kernel check the
//     modes itself, as virtiofs's default_permissions has it), and files of mode 0600, 0444 and 0000;
//   - what chown and chmod do, by root and by uid 10000, and what the Mac then shows; git's check of a
//     repository's owner;
//   - whether an idmapped bind mount of the share (open_tree, mount_setattr with MOUNT_ATTR_IDMAP, a user namespace
//     mapping 0 to 10000) is taken, as one of a tmpfs is, and whether uid 10000 then writes through it.
// It needs no root set up: the agent mounts a chat's share before it reads its owner, so a setup refused for
// that owner leaves it mounted, and the measurements' agent disk (prepare.sh, never in an app) runs the probe as
// the guest's root. Run it in two accounts of the Mac (its first, uid 501, and another), as the owners may
// depend on the Mac user's uid.
//   npx vitest run -c test/vm/darwin/measure/vitest.config.ts share-owners
// Prints what each side saw. PASS or FAIL: the probe ran to its end. What it found is for the owner's choice of
// how a session runs on this share, and is not judged here.

import { chmodSync, existsSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { expect, it } from "vitest";

import { folderOf, lab, PROBING_DISK, record, ROOTS, scratch, USER } from "./harness.js";

// The guest's uids the probe looks as: a session's first (guest/root.ts, FIRST_UID), another, and the Mac user's.
const SESSION = 10_000;
const OTHER = 4_321;

// The files the Mac makes, and their modes.
const MADE_BY_THE_MAC: Array<[string, number]> = [["host.txt", 0o644], ["private.txt", 0o600], ["readonly.txt", 0o444], ["locked.txt", 0o000]];
// The files the guest makes, which the Mac then looks at.
const MADE_BY_THE_GUEST = ["by-root.txt", "by-10000.txt", "chmod-root.txt", "chown-root.txt", "chmod-10000.txt", "chown-10000.txt", "idmapped-by-10000.txt"];

// An idmapped bind mount of argv[1] at argv[2], idmapped by the user namespace at argv[3]: open_tree, mount_setattr
// with MOUNT_ATTR_IDMAP, then move_mount (linux/mount.h; the generic syscall numbers, arm64's and x86_64's alike).
// Each argument is given its C type: an int in a variadic call leaves a 64-bit register's top half unset.
const IDMAP = `import ctypes, errno, os, sys
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
class MountAttr(ctypes.Structure):
    _fields_ = [("attr_set", ctypes.c_uint64), ("attr_clr", ctypes.c_uint64), ("propagation", ctypes.c_uint64), ("userns_fd", ctypes.c_uint64)]
def failed(call):
    code = ctypes.get_errno()
    print(f"{call} failed: {errno.errorcode.get(code, code)} {os.strerror(code)}")
    sys.exit(1)
source, target, userns = sys.argv[1:4]
tree = libc.syscall(ctypes.c_long(428), ctypes.c_int(-100), ctypes.c_char_p(source.encode()), ctypes.c_uint(1 | os.O_CLOEXEC))
if tree < 0: failed("open_tree")
attr = MountAttr(0x00100000, 0, 0, os.open(userns, os.O_RDONLY | os.O_CLOEXEC))
if libc.syscall(ctypes.c_long(442), ctypes.c_int(tree), ctypes.c_char_p(b""), ctypes.c_uint(0x1000), ctypes.byref(attr), ctypes.c_size_t(ctypes.sizeof(attr))) < 0: failed("mount_setattr")
if libc.syscall(ctypes.c_long(429), ctypes.c_int(tree), ctypes.c_char_p(b""), ctypes.c_int(-100), ctypes.c_char_p(target.encode()), ctypes.c_uint(4)) < 0: failed("move_mount")
print("ok")
`;

// The probe, as the guest's root, in the share's folder (here). Each line is "<what>: <what happened>".
const probe = (hostUid: number) => `here=$(dirname "$0")
S=/run/surogate-measure-share; I=/run/surogate-measure-idmap; T=/run/surogate-measure-tmpfs
as() { who=$1; shift; HOME=/tmp setpriv --reuid="$who" --regid="$who" --clear-groups -- "$@"; }
tried() { if said=$("$@" 2>&1 >/dev/null); then echo ok; else echo "failed: $(printf %s "$said" | tr '\\n' ' ' | cut -c1-160)"; fi; }
look() { who=$1; shift; as "$who" stat -c '%n %u:%g %a' "$@" 2>&1 | tr '\\n' ';'; }
now() { echo $(( $(date +%s%N) / 1000000 )); }
# The share's folder bound where every uid may reach it: the agent's own mounts are under a folder of root's alone.
mkdir -p "$S" "$I" "$T" && mount --bind "$here" "$S" && cd "$S" || { echo "bound: failed"; exit 0; }
echo "the share's mounts: $(grep ' virtiofs ' /proc/mounts | tr '\\n' ';')"
echo "as root: $(look 0 . host.txt)"
echo "as ${SESSION} at once: $(look ${SESSION} . host.txt)"
sleep 3
# How long a look by root goes on showing root as the owner to another uid: the attributes' time in the guest's cache.
stat . host.txt > /dev/null; start=$(now)
while shown=$(as ${SESSION} stat -c %u host.txt 2>&1); [ "$shown" != ${SESSION} ] && [ $(( $(now) - start )) -lt 5000 ]; do sleep 0.05; done
window=$(( $(now) - start ))
echo "root's look shown to uid ${SESSION}: for $window ms, then as $shown"
pause=$(( window + 500 )); [ "$pause" -ge 1500 ] || pause=1500
cool() { sleep "$(( pause / 1000 )).$(printf %03d $(( pause % 1000 )))"; }
cool; echo "as ${SESSION} once cool: $(look ${SESSION} . host.txt)"
echo "as ${OTHER} at once after: $(look ${OTHER} . host.txt)"
cool; echo "as ${OTHER} once cool: $(look ${OTHER} . host.txt)"
cool; echo "as ${hostUid}, the Mac user's uid, once cool: $(look ${hostUid} . host.txt)"
cool; stat host.txt > /dev/null
echo "${SESSION} reads host.txt (0644) after root's look: $(tried as ${SESSION} cat host.txt)"
echo "${SESSION} appends to host.txt (0644) after root's look: $(tried as ${SESSION} sh -c 'echo ${SESSION} >> host.txt')"
cool; echo "${SESSION} appends to host.txt (0644) once cool: $(tried as ${SESSION} sh -c 'echo ${SESSION} >> host.txt')"
echo "${OTHER} appends to host.txt (0644) at once after: $(tried as ${OTHER} sh -c 'echo ${OTHER} >> host.txt')"
cool; echo "${SESSION} reads private.txt (0600): $(tried as ${SESSION} cat private.txt)"
echo "${SESSION} appends to readonly.txt (0444): $(tried as ${SESSION} sh -c 'echo ${SESSION} >> readonly.txt')"
echo "root appends to readonly.txt (0444): $(tried sh -c 'echo root >> readonly.txt')"
echo "root reads locked.txt (0000): $(tried cat locked.txt)"
echo "${SESSION} reads locked.txt (0000): $(tried as ${SESSION} cat locked.txt)"
echo "root makes by-root.txt: $(tried sh -c 'echo root > by-root.txt')"
echo "${SESSION} makes by-10000.txt: $(tried as ${SESSION} sh -c 'echo ${SESSION} > by-10000.txt')"
echo "the made files, as root: $(look 0 by-root.txt by-10000.txt)"
cool; echo "the made files, as ${SESSION} once cool: $(look ${SESSION} by-root.txt by-10000.txt)"
echo root > chmod-root.txt; echo root > chown-root.txt
echo "root chmod 600 chmod-root.txt: $(tried chmod 600 chmod-root.txt); then as root $(look 0 chmod-root.txt)"
echo "root chown 4321:4321 chown-root.txt: $(tried chown ${OTHER}:${OTHER} chown-root.txt); then as root $(look 0 chown-root.txt)"
cool; echo "chown-root.txt once cool: as root $(look 0 chown-root.txt) as ${OTHER} $(look ${OTHER} chown-root.txt)"
as ${SESSION} sh -c 'echo x > chmod-10000.txt; echo x > chown-10000.txt'
cool; stat chmod-10000.txt > /dev/null
echo "${SESSION} chmod 600 chmod-10000.txt after root's look: $(tried as ${SESSION} chmod 600 chmod-10000.txt)"
cool; echo "${SESSION} chmod 640 chmod-10000.txt once cool: $(tried as ${SESSION} chmod 640 chmod-10000.txt); then as ${SESSION} $(look ${SESSION} chmod-10000.txt)"
cool; echo "${SESSION} chown ${SESSION}:${SESSION} chown-10000.txt once cool: $(tried as ${SESSION} chown ${SESSION}:${SESSION} chown-10000.txt)"
cool; echo "${SESSION} git init: $(tried as ${SESSION} git init -q repo)"
cool; stat repo > /dev/null
echo "${SESSION} git status after root's look: $(tried as ${SESSION} git -C repo status --short)"
cool; echo "${SESSION} git status once cool: $(tried as ${SESSION} git -C repo status --short)"
# An idmapped mount needs a user namespace, which the guest's init allows none of: one, for this, then none again.
echo "user namespaces the guest allows: $(cat /proc/sys/user/max_user_namespaces)"
echo 1 > /proc/sys/user/max_user_namespaces
unshare --user sleep 120 & holder=$!
sleep 0.5
if [ "$(readlink /proc/$holder/ns/user)" != "$(readlink /proc/self/ns/user)" ] && echo "0 ${SESSION} 1" > /proc/$holder/uid_map && echo "0 ${SESSION} 1" > /proc/$holder/gid_map; then
  echo "the user namespace: uid_map $(tr -s ' ' < /proc/$holder/uid_map)"
  mount -t tmpfs -o mode=0755 tmpfs "$T" && echo tmpfs > "$T/file"
  echo "an idmapped bind mount of a tmpfs: $(python3 "$here/.measure-idmap.py" "$T" "$I" /proc/$holder/ns/user 2>&1)"
  mountpoint -q "$I" && { echo "through it, as root: $(look 0 "$I/file")"; umount "$I"; }
  umount "$T"
  idmapped=$(python3 "$here/.measure-idmap.py" "$S" "$I" /proc/$holder/ns/user 2>&1)
  echo "an idmapped bind mount of the share: $idmapped"
  if mountpoint -q "$I"; then
    echo "through it, as ${SESSION}: $(look ${SESSION} "$I" "$I/host.txt")"
    echo "${SESSION} makes idmapped-by-10000.txt through it: $(tried as ${SESSION} sh -c "echo ${SESSION} > $I/idmapped-by-10000.txt")"
    echo "${SESSION} appends to host.txt through it: $(tried as ${SESSION} sh -c "echo idmapped >> $I/host.txt")"
    echo "idmapped-by-10000.txt, as ${SESSION} through it: $(look ${SESSION} "$I/idmapped-by-10000.txt") as root in the share: $(look 0 idmapped-by-10000.txt)"
    umount "$I"
  fi
else
  echo "the user namespace: not made"
fi
kill $holder 2>/dev/null
echo 0 > /proc/sys/user/max_user_namespaces
cd / && umount "$S"; rmdir "$S" "$I" "$T"
echo "the probe's end: ok"
`;

// "<what>: <what happened>" lines, as an object in their order.
const lines = (text: string) => Object.fromEntries(text.trim().split("\n").map((line) => {
  const at = line.indexOf(": ");
  return at < 0 ? [line, ""] : [line.slice(0, at), line.slice(at + 2)];
}));

// How the Mac sees *path*: its owner, group and mode, or that it is not there.
const onTheMac = (path: string) => {
  if (!existsSync(path)) return "not there";
  const { uid, gid, mode } = statSync(path);
  return `${uid}:${gid} ${(mode & 0o7777).toString(8).padStart(4, "0")}`;
};

it("records how Apple's share shows owners to each uid of the guest, and what chown, chmod and an idmapped mount do", async () => {
  const folder = scratch("share-owners");
  for (const [name, mode] of MADE_BY_THE_MAC) {
    writeFileSync(join(folder, name), `${name}\n`);
    chmodSync(join(folder, name), mode);
  }
  const before = Object.fromEntries([".", ...MADE_BY_THE_MAC.map(([name]) => name)].map((name) => [name, onTheMac(join(folder, name))]));
  const booted = await lab("share-owners", { agentDisk: PROBING_DISK });
  // Set up, or refused for the owner it reads: the share is mounted either way, where the probe's watcher looks.
  const setup = await booted.guest.ready(ROOTS[0]!, folderOf(folder));
  writeFileSync(join(folder, ".measure-idmap.py"), IDMAP);
  writeFileSync(join(folder, ".measure-probe"), probe(USER.uid));
  let said = "";
  for (const end = Date.now() + 300_000; Date.now() < end && !said; await new Promise((resolve) => setTimeout(resolve, 500))) {
    try {
      said = readFileSync(join(folder, ".measure-probe.out"), "utf8");
    } catch {
      // Not yet.
    }
  }
  await booted.stop();
  const after = Object.fromEntries([".", ...MADE_BY_THE_MAC.map(([name]) => name), ...MADE_BY_THE_GUEST].map((name) => [name, onTheMac(join(folder, name))]));
  const seen = lines(said);
  const failed = record("share-owners", {
    "the Mac user": `uid ${USER.uid} gid ${USER.gid}`,
    "the root's setup": setup === null ? "set up" : JSON.stringify(setup),
    "the Mac's files, on the Mac, before": before,
    "what the guest saw and did": seen,
    "every file, on the Mac, after": after,
    "host.txt, on the Mac, after": existsSync(join(folder, "host.txt")) ? readFileSync(join(folder, "host.txt"), "utf8").trim().split("\n") : "not there",
  }, {
    "the probe ran to its end": seen["the probe's end"] === "ok",
  });
  expect(failed).toEqual([]);
});
