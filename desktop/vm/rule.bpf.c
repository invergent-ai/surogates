// The guest-kernel rule on protected names. Refuses a session's writes to
// the paths files/protect.ts protects, in a folder a chat shares into the guest, by
// walking the dentry up and feeding each component to rule-match.h's stepper; and,
// where the kernel's line names that set, of the names Windows's Explorer acts on. Loaded
// once at boot (vm/init); a session (uid >= FIRST_UID, or the scope's one uid) cannot unload it.
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_core_read.h>
#include <bpf/bpf_tracing.h>
#include "rule-match.h"

char LICENSE[] SEC("license") = "GPL";

#define FIRST_UID 10000u            // sessions' guest uids start here (root.ts)
#define RDONLY_PROG (1U << 7)       // BPF_F_RDONLY_PROG: the programs read the map and never write it
#define EPERM 1
#define FUSE_SUPER_MAGIC 0x65735546 // virtiofs: Linux's and macOS's shares
#define V9FS_MAGIC 0x01021997       // 9p: Hyper-V's Plan 9 shares (guest/root.ts)
#define AF_VSOCK 40
#define FMODE_WRITE 0x2
#define O_TRUNC 01000
#define ATTR_MODE (1 << 0)
#define ATTR_UID (1 << 1)
#define ATTR_GID (1 << 2)
#define ATTR_SIZE (1 << 3)
#define S_IFMT 00170000
#define S_IFDIR 0040000
#define RENAME_EXCHANGE (1 << 1)
// judge's answer: bit 0, refused; bit 1, the path lies at or below a dependency folder (sg_in_dep).
#define REFUSED 1
#define IN_DEP 2

// Walk from the target up to the mount root (leaf-first, the order sg_step wants),
// judging each component (dir: as a directory being moved) as folded (sg_fold), and, where the kernel's line
// names *windows*, as a spelling NTFS may resolve to another name (sg_windows_alias). One bounded pass: sg_fold,
// sg_classify and sg_windows_alias are out-of-line, each verified once, and sg_fold and sg_step branch-free, so
// the walk stays within the verifier's budget at SG_WALK=12.
static __noinline int judge(struct dentry *dentry, int dir, int windows)
{
	struct sg_raw raw = {};
	struct sg_name nm;
	struct sg_state st;
	sg_init(&st, dir);
	for (int i = 0; i < SG_WALK; i++) {
		const unsigned char *name = BPF_CORE_READ(dentry, d_name.name);
		int len = BPF_CORE_READ(dentry, d_name.len);
		// NUL-terminated: what a longer name before it left past the NUL completes no character.
		long got = bpf_core_read_str(raw.b, SG_RAWLEN, name);
		if (windows && sg_windows_alias(&raw, got < 0 ? 0 : len))
			return REFUSED;
		if (sg_step(&st, sg_classify(&nm, sg_fold(&raw, got < 0 ? 0 : len, &nm))))
			return REFUSED;
		struct dentry *parent = BPF_CORE_READ(dentry, d_parent);
		if (parent == dentry)
			break;
		dentry = parent;
	}
	return sg_end(&st) | (sg_in_dep(&st) << 1);
}

// The one uid besides those from FIRST_UID up that sessions run as: where the VM's shares map no owner
// to a session's uid (macOS's), every session runs as the uid the share shows, the host user's (root.ts).
// vm/init writes it from the kernel's command line (surogate.uid=), or leaves it 0 where that names none,
// as Linux's never does, and freezes the map before it says the rule is attached: nothing writes it after.
// A value of 0, or one above the last uid, judges no other uid: the guest's own root is never in scope.
struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, __u32);
	__uint(map_flags, RDONLY_PROG);
} scope SEC(".maps");

// The names refused besides the protected ones, a set the kernel's command line names (surogate.names=), which
// vm/init writes and freezes as it does the scope: NAMES_POSIX, Linux's and macOS's, none more; NAMES_WINDOWS,
// those Windows's Explorer acts on (rule-match.h, sg_explorer). A load whose line names neither fails the boot.
#define NAMES_POSIX 0
#define NAMES_WINDOWS 1
struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, __u32);
	__uint(map_flags, RDONLY_PROG);
} names SEC(".maps");

static __always_inline int windows_names(void)
{
	__u32 key = 0;
	__u32 *set = bpf_map_lookup_elem(&names, &key);
	return set && *set == NAMES_WINDOWS;
}

// Whether *dentry*'s own name is one Windows's Explorer acts on, by its end: its last SG_TAIL bytes, or the
// whole of a shorter one. One whose end cannot be read is taken for one: the set refuses what it cannot judge.
static __noinline int explorer_name(struct dentry *dentry)
{
	struct sg_tail tail = {};
	const unsigned char *name = BPF_CORE_READ(dentry, d_name.name);
	unsigned int len = BPF_CORE_READ(dentry, d_name.len);
	long got = len > SG_TAIL ? bpf_probe_read_kernel(tail.b, SG_TAIL, name + (len - SG_TAIL)) : bpf_probe_read_kernel_str(tail.b, SG_TAIL + 1, name);
	if (got < 0)
		return 1;
	return sg_explorer(&tail, len > SG_TAIL ? SG_TAIL : (int)len);
}

static __always_inline int in_scope(__u32 uid)
{
	__u32 key = 0;
	__u32 *one = bpf_map_lookup_elem(&scope, &key);
	return uid >= FIRST_UID || (one && *one != 0 && *one != (__u32)-1 && uid == *one);
}

// A share's filesystem: a guest mounts its shares as one kind, virtio-fs or 9p, and nothing else as either.
static __noinline int share_sb(struct super_block *sb)
{
	if (!sb)
		return 0;
	unsigned long magic = BPF_CORE_READ(sb, s_magic);
	return magic == FUSE_SUPER_MAGIC || magic == V9FS_MAGIC;
}

static __noinline int judge_dentry(struct super_block *sb, struct dentry *dentry, int dir)
{
	__u32 uid = bpf_get_current_uid_gid();
	if (!in_scope(uid) || !dentry || !share_sb(sb))
		return 0;
	int windows = windows_names();
	if (windows && explorer_name(dentry))
		return REFUSED;
	return judge(dentry, dir, windows);
}

static __always_inline int refused(struct super_block *sb, struct dentry *dentry, int dir)
{
	return judge_dentry(sb, dentry, dir) & REFUSED;
}

// A negative dentry (a rename's new name, mostly) has no inode, and reads as no directory.
static __always_inline int is_dir(struct dentry *dentry)
{
	return (BPF_CORE_READ(dentry, d_inode, i_mode) & S_IFMT) == S_IFDIR;
}

// deny takes 0 (allow) or 1 (refuse). The verifier does not range-track a noinline
// subprogram's return; barrier_var stops clang from folding this to a bare negation,
// so the verifier sees two literal return paths, both in [-EPERM, 0].
static __always_inline int deny(int r)
{
	barrier_var(r);
	if (r != 0)
		return -EPERM;
	return 0;
}
#define DENY(x) deny(x)

SEC("lsm/inode_create")
int BPF_PROG(on_create, struct inode *dir, struct dentry *dentry, umode_t mode)
{
	return DENY(refused(BPF_CORE_READ(dir, i_sb), dentry, 0));
}

SEC("lsm/path_mkdir")
int BPF_PROG(on_mkdir, const struct path *dir, struct dentry *dentry, umode_t mode)
{
	return DENY(refused(BPF_CORE_READ(dir, dentry, d_sb), dentry, 0));
}

SEC("lsm/path_symlink")
int BPF_PROG(on_symlink, const struct path *dir, struct dentry *dentry, const char *old)
{
	return DENY(refused(BPF_CORE_READ(dir, dentry, d_sb), dentry, 0));
}

// A FIFO or socket at a protected name would hang the host's git or editor on it.
SEC("lsm/path_mknod")
int BPF_PROG(on_mknod, const struct path *dir, struct dentry *dentry, umode_t mode, unsigned int dev)
{
	return DENY(refused(BPF_CORE_READ(dir, dentry, d_sb), dentry, 0));
}

// The old name too: a link to a protected file under an ordinary name is that file,
// and a write through the link would land in it. ||: the second walk only where the first
// allowed, for the reason on_rename gives.
SEC("lsm/path_link")
int BPF_PROG(on_link, struct dentry *old, const struct path *dir, struct dentry *dentry)
{
	return DENY(refused(BPF_CORE_READ(dir, dentry, d_sb), dentry, 0) || refused(BPF_CORE_READ(old, d_sb), old, 0));
}

SEC("lsm/path_unlink")
int BPF_PROG(on_unlink, const struct path *dir, struct dentry *dentry)
{
	return DENY(refused(BPF_CORE_READ(dir, dentry, d_sb), dentry, 0));
}

SEC("lsm/path_rmdir")
int BPF_PROG(on_rmdir, const struct path *dir, struct dentry *dentry)
{
	return DENY(refused(BPF_CORE_READ(dir, dentry, d_sb), dentry, 0));
}

// The new path is walked only where the old one was allowed: walked for each way the first
// walk returned, it would go past the verifier's 1 M budget. A directory moved (either one, as
// an exchange moves both) is judged as one on both paths, and may not leave a dependency folder.
SEC("lsm/path_rename")
int BPF_PROG(on_rename, const struct path *odir, struct dentry *od, const struct path *ndir, struct dentry *nd, unsigned int flags)
{
	int dir = is_dir(od) | is_dir(nd);
	int old = judge_dentry(BPF_CORE_READ(od, d_sb), od, dir);
	if (old & REFUSED)
		return DENY(1);
	int new = judge_dentry(BPF_CORE_READ(nd, d_sb), nd, dir);
	return DENY((new & REFUSED) |
		sg_moved_out(dir, (flags & RENAME_EXCHANGE) != 0, (old & IN_DEP) != 0, (new & IN_DEP) != 0));
}

SEC("lsm/path_truncate")
int BPF_PROG(on_truncate, const struct path *path)
{
	return DENY(refused(BPF_CORE_READ(path, dentry, d_sb), BPF_CORE_READ(path, dentry), 0));
}

// A read-only open with O_TRUNC is a write: virtiofsd and a 9P server truncate at the open, before setattr is asked.
SEC("lsm/file_open")
int BPF_PROG(on_open, struct file *file)
{
	if (!(BPF_CORE_READ(file, f_mode) & FMODE_WRITE) && !(BPF_CORE_READ(file, f_flags) & O_TRUNC))
		return 0;
	struct dentry *dentry = BPF_CORE_READ(file, f_path.dentry);
	return DENY(refused(BPF_CORE_READ(dentry, d_sb), dentry, 0));
}

// The hook takes the mount's idmap first, as every kernel since 6.9 passes it: declared
// without it, the program would read attr as the dentry and allow every chmod. The image's
// build holds each program to its hook's arguments in the pinned kernel's BTF.
SEC("lsm/inode_setattr")
int BPF_PROG(on_setattr, struct mnt_idmap *idmap, struct dentry *dentry, struct iattr *attr)
{
	unsigned int valid = BPF_CORE_READ(attr, ia_valid);
	if (!(valid & (ATTR_MODE | ATTR_UID | ATTR_GID | ATTR_SIZE)))
		return 0;
	return DENY(refused(BPF_CORE_READ(dentry, d_sb), dentry, 0));
}

// An extended attribute set or removed on a protected path: a POSIX ACL there would widen who may write
// it, and reach the host's file where the folder's daemon keeps attributes. Every name is refused there:
// git and the editors keep none on what they read.
SEC("lsm/inode_setxattr")
int BPF_PROG(on_setxattr, struct mnt_idmap *idmap, struct dentry *dentry, const char *name, const void *value, size_t size, int flags)
{
	return DENY(refused(BPF_CORE_READ(dentry, d_sb), dentry, 0));
}

SEC("lsm/inode_removexattr")
int BPF_PROG(on_removexattr, struct mnt_idmap *idmap, struct dentry *dentry, const char *name)
{
	return DENY(refused(BPF_CORE_READ(dentry, d_sb), dentry, 0));
}

// A POSIX ACL set or removed, by setxattr and removexattr too: the kernel asks these hooks for it, not the two above.
SEC("lsm/inode_set_acl")
int BPF_PROG(on_set_acl, struct mnt_idmap *idmap, struct dentry *dentry, const char *acl_name, struct posix_acl *kacl)
{
	return DENY(refused(BPF_CORE_READ(dentry, d_sb), dentry, 0));
}

SEC("lsm/inode_remove_acl")
int BPF_PROG(on_remove_acl, struct mnt_idmap *idmap, struct dentry *dentry, const char *acl_name)
{
	return DENY(refused(BPF_CORE_READ(dentry, d_sb), dentry, 0));
}

// A session's socket of vsock's family. Where the guest's ports are on vsock (Hyper-V's sockets), its vsock reaches
// the host's ports, the agent's and the Plan 9 server's, which the agent alone dials, with the mounts it makes
// (guest/port.ts, root.ts): a session's connection to one would be the agent's, or another root's share's. Where
// they are not, the guest never loads vsock (vm/init), and the socket fails either way.
SEC("lsm/socket_create")
int BPF_PROG(on_socket, int family, int type, int protocol, int kern)
{
	if (kern || family != AF_VSOCK)
		return 0;
	return DENY(in_scope(bpf_get_current_uid_gid()));
}
