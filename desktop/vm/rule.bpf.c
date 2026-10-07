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
#define ATTR_MODE (1 << 0)
#define ATTR_UID (1 << 1)
#define ATTR_GID (1 << 2)
#define ATTR_SIZE (1 << 3)
#define S_IFMT 00170000
#define S_IFDIR 0040000

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
			return 1;
		struct dentry *parent = BPF_CORE_READ(dentry, d_parent);
		if (parent == dentry)
			break;
		dentry = parent;
	}
	return sg_end(&st);
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

// A negative dentry (a rename's new name, mostly) has no inode, and reads as no directory.
static __always_inline int is_dir(struct dentry *dentry)
{
	return (BPF_CORE_READ(dentry, d_inode, i_mode) & S_IFMT) == S_IFDIR;
}

// judge_dentry returns 0 (allow) or 1 (refuse). The verifier does not range-track a
// noinline subprogram's return; barrier_var stops clang from folding this to a bare
// negation, so the verifier sees two literal return paths, both in [-EPERM, 0].
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
	return DENY(judge_dentry(BPF_CORE_READ(dir, i_sb), dentry, 0));
}

SEC("lsm/path_mkdir")
int BPF_PROG(on_mkdir, const struct path *dir, struct dentry *dentry, umode_t mode)
{
	return DENY(judge_dentry(BPF_CORE_READ(dir, dentry, d_sb), dentry, 0));
}

SEC("lsm/path_symlink")
int BPF_PROG(on_symlink, const struct path *dir, struct dentry *dentry, const char *old)
{
	return DENY(judge_dentry(BPF_CORE_READ(dir, dentry, d_sb), dentry, 0));
}

// A FIFO or socket at a protected name would hang the host's git or editor on it.
SEC("lsm/path_mknod")
int BPF_PROG(on_mknod, const struct path *dir, struct dentry *dentry, umode_t mode, unsigned int dev)
{
	return DENY(judge_dentry(BPF_CORE_READ(dir, dentry, d_sb), dentry, 0));
}

// The old name too: a link to a protected file under an ordinary name is that file,
// and a write through the link would land in it. || for the reason on_rename gives.
SEC("lsm/path_link")
int BPF_PROG(on_link, struct dentry *old, const struct path *dir, struct dentry *dentry)
{
	return DENY(judge_dentry(BPF_CORE_READ(dir, dentry, d_sb), dentry, 0) || judge_dentry(BPF_CORE_READ(old, d_sb), old, 0));
}

SEC("lsm/path_unlink")
int BPF_PROG(on_unlink, const struct path *dir, struct dentry *dentry)
{
	return DENY(judge_dentry(BPF_CORE_READ(dir, dentry, d_sb), dentry, 0));
}

SEC("lsm/path_rmdir")
int BPF_PROG(on_rmdir, const struct path *dir, struct dentry *dentry)
{
	return DENY(judge_dentry(BPF_CORE_READ(dir, dentry, d_sb), dentry, 0));
}

// ||, not |: the verifier then walks the new path only where the old one was allowed.
// With |, it walks it once for each way the first walk returned, past its 1 M budget.
// A directory moved (either one, as an exchange moves both) is judged as one on both paths.
SEC("lsm/path_rename")
int BPF_PROG(on_rename, const struct path *odir, struct dentry *od, const struct path *ndir, struct dentry *nd, unsigned int flags)
{
	int dir = is_dir(od) | is_dir(nd);
	return DENY(judge_dentry(BPF_CORE_READ(od, d_sb), od, dir) || judge_dentry(BPF_CORE_READ(nd, d_sb), nd, dir));
}

SEC("lsm/path_truncate")
int BPF_PROG(on_truncate, const struct path *path)
{
	return DENY(judge_dentry(BPF_CORE_READ(path, dentry, d_sb), BPF_CORE_READ(path, dentry), 0));
}

SEC("lsm/file_open")
int BPF_PROG(on_open, struct file *file)
{
	if (!(BPF_CORE_READ(file, f_mode) & FMODE_WRITE))
		return 0;
	struct dentry *dentry = BPF_CORE_READ(file, f_path.dentry);
	return DENY(judge_dentry(BPF_CORE_READ(dentry, d_sb), dentry, 0));
}

// The pinned kernel's hook takes no idmap (6.9 added one): declared with it, the program
// read attr as the dentry and the hook's return slot, 0, as attr, and allowed every chmod.
SEC("lsm/inode_setattr")
int BPF_PROG(on_setattr, struct dentry *dentry, struct iattr *attr)
{
	unsigned int valid = BPF_CORE_READ(attr, ia_valid);
	if (!(valid & (ATTR_MODE | ATTR_UID | ATTR_GID | ATTR_SIZE)))
		return 0;
	return DENY(judge_dentry(BPF_CORE_READ(dentry, d_sb), dentry, 0));
}
