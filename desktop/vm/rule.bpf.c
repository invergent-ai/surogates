// The guest-kernel rule on protected names. Refuses a session's writes to
// the paths files/protect.ts protects, in a folder a chat shares into the guest, by
// walking the dentry up and feeding each component to rule-match.h's stepper. Loaded
// once at boot (vm/init); a session (uid >= FIRST_UID) cannot unload it.
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_core_read.h>
#include <bpf/bpf_tracing.h>
#include "rule-match.h"

char LICENSE[] SEC("license") = "GPL";

#define FIRST_UID 10000u            // sessions' guest uids start here (root.ts)
#define EPERM 1
#define FUSE_SUPER_MAGIC 0x65735546 // virtiofs; the only share fs on Linux
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
// judging each component (dir: as a directory being moved). One bounded pass, no array:
// sg_classify is out-of-line and sg_step branch-free, so the walk stays within the
// verifier's budget at SG_WALK=12.
static __noinline int judge(struct dentry *dentry, int dir)
{
	struct sg_name nm;
	struct sg_state st;
	sg_init(&st, dir);
	for (int i = 0; i < SG_WALK; i++) {
		const unsigned char *name = BPF_CORE_READ(dentry, d_name.name);
		int len = BPF_CORE_READ(dentry, d_name.len);
		long got = bpf_core_read_str(nm.b, SG_NAMELEN, name);
		// Branch-free ASCII lower-case: unknown name bytes must not fork the verifier.
		for (int j = 0; j < SG_NAMELEN; j++) {
			unsigned char uc = nm.b[j];
			unsigned char ge = (unsigned char)('A' - 1 - uc) >> 7; // 1 iff uc >= 'A'
			unsigned char le = (unsigned char)(uc - ('Z' + 1)) >> 7; // 1 iff uc <= 'Z'
			nm.b[j] = uc + ((ge & le) << 5);
		}
		if (sg_step(&st, sg_classify(&nm, got < 0 ? 0 : len)))
			return REFUSED;
		struct dentry *parent = BPF_CORE_READ(dentry, d_parent);
		if (parent == dentry)
			break;
		dentry = parent;
	}
	return sg_end(&st) | (sg_in_dep(&st) << 1);
}

static __noinline int share_sb(struct super_block *sb)
{
	if (!sb)
		return 0;
	return BPF_CORE_READ(sb, s_magic) == FUSE_SUPER_MAGIC;
}

static __noinline int judge_dentry(struct super_block *sb, struct dentry *dentry, int dir)
{
	__u32 uid = bpf_get_current_uid_gid();
	if (uid < FIRST_UID || !dentry || !share_sb(sb))
		return 0;
	return judge(dentry, dir);
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

// A read-only open with O_TRUNC is a write: virtiofsd truncates at the open, before setattr is asked.
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
