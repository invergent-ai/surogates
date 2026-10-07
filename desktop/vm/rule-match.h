#ifndef SUROGATE_RULE_MATCH_H
#define SUROGATE_RULE_MATCH_H
// The path decision of the guest-kernel rule, as pure C shared by rule.bpf.c and
// the host drift test (compiled natively with cc). It mirrors files/protect.ts's
// protectedInFolder, minus git's transient-state operational content (rebase-merge/
// rebase-apply/sequencer, and a linked worktree's content other than the config
// names), which the rule allows so the guest's own git can rebase, cherry-pick and
// drive worktrees; the host hook guard strips command-added exec lines from the
// rebase/sequencer todos (the one residual exec vector). Fed one component at a
// time, from the target (leaf) up to the mount root, then asked sg_end.

#define SG_WALK 12    // path components judged from the target up to the mount root
#define SG_NAMELEN 16 // the longest protected name is config.worktree (15)

#ifdef __bpf__
#define SG_INLINE static __attribute__((always_inline)) inline
#define SG_NOINLINE __attribute__((noinline))
#define SG_ARG_NONNULL __attribute__((btf_decl_tag("arg:nonnull")))
#else
#define SG_INLINE static inline
#define SG_NOINLINE static
#define SG_ARG_NONNULL
#endif

struct sg_name { char b[SG_NAMELEN]; };

#define SB_PROTECTED (1 << 0) // a shell's, editor's or agent's name: not below a dependency folder
#define SB_DOTGIT    (1 << 1)
#define SB_HOOKS     (1 << 2)
#define SB_CONFIG    (1 << 3)
#define SB_CFGLEAF   (1 << 4) // config.worktree or commondir
#define SB_MODULES   (1 << 5)
#define SB_WORKTREES (1 << 6)
#define SB_CLAUDE    (1 << 7)
#define SB_CMDAGENT  (1 << 8) // commands or agents
#define SB_GITNAME   (1 << 9) // .gitconfig or .gitmodules, git's: in a dependency folder too
#define SB_DEP       (1 << 10) // node_modules, site-packages or dist-packages

SG_INLINE int sg_streq(const char *a, int alen, const char *lit, int litlen)
{
	if (alen != litlen)
		return 0;
	for (int i = 0; i < SG_NAMELEN && i < litlen; i++)
		if (a[i] != lit[i])
			return 0;
	return 1;
}

#define SG_IS(s, n, lit) sg_streq((s), (n), lit, sizeof(lit) - 1)

// Classify one already-lower-cased component into name-match bits. Out-of-line on
// BPF (verified once, not per walk step) and taking a pointer to a fixed 16-byte
// buffer (a const char* is rejected as unsized; a by-value struct as an invalid arg).
SG_NOINLINE int sg_classify(const struct sg_name *nm SG_ARG_NONNULL, int len)
{
	const char *s = nm->b;
	int b = 0;
	if (len <= SG_NAMELEN &&
	    (SG_IS(s, len, ".bashrc") || SG_IS(s, len, ".bash_profile") || SG_IS(s, len, ".zshrc") ||
	     SG_IS(s, len, ".zprofile") || SG_IS(s, len, ".profile") || SG_IS(s, len, ".ripgreprc") ||
	     SG_IS(s, len, ".mcp.json") || SG_IS(s, len, ".vscode") || SG_IS(s, len, ".idea")))
		b |= SB_PROTECTED;
	if (SG_IS(s, len, ".gitconfig") || SG_IS(s, len, ".gitmodules")) b |= SB_GITNAME;
	if (SG_IS(s, len, "node_modules") || SG_IS(s, len, "site-packages") || SG_IS(s, len, "dist-packages")) b |= SB_DEP;
	if (SG_IS(s, len, ".git")) b |= SB_DOTGIT;
	if (SG_IS(s, len, "hooks")) b |= SB_HOOKS;
	if (SG_IS(s, len, "config")) b |= SB_CONFIG;
	if (SG_IS(s, len, "config.worktree") || SG_IS(s, len, "commondir")) b |= SB_CFGLEAF;
	if (SG_IS(s, len, "modules")) b |= SB_MODULES;
	if (SG_IS(s, len, "worktrees")) b |= SB_WORKTREES;
	if (SG_IS(s, len, ".claude")) b |= SB_CLAUDE;
	if (SG_IS(s, len, "commands") || SG_IS(s, len, "agents")) b |= SB_CMDAGENT;
	return b;
}

enum sg_child { CC_OTHER, CC_HOOKS, CC_CONFIG, CC_CFGLEAF, CC_MODULES, CC_WORKTREES, CC_CMDAGENT };

// any_hooks: a hooks component below this one; hk1 and hk2 lag it by one and two
// components, so at a .git, hk2 leaves out modules/<name's first part> as protect.ts does.
// dir: 1 when the path is a directory being moved (rename), else 0; moved_key: the path
// is one a moved directory may not take or leave. pending: a shell's, editor's or agent's
// name lies below, with no dependency folder above it yet.
struct sg_state { int i; int child; int any_hooks; int hk1; int hk2; int leaf_config; int dir; int moved_key; int pending; };

SG_INLINE void sg_init(struct sg_state *s, int dir)
{
	s->i = 0; s->child = CC_OTHER; s->any_hooks = 0; s->hk1 = 0; s->hk2 = 0; s->leaf_config = 0;
	s->dir = dir; s->moved_key = 0; s->pending = 0;
}

// Feed one component's match bits (leaf-first; i == 0 is the target). Returns 1 the
// moment the path is refused; for a moved directory (sg_init's dir), more is refused.
// Branch-free (bitwise | and & on 0/1 terms) so that, walked on BPF, the unknown bits
// do not fork the verifier into a state explosion.
SG_INLINE int sg_step(struct sg_state *s, int bits)
{
	int i = s->i, child = s->child;
	int d = (bits & SB_DOTGIT) != 0;
	int refuse =
		((i == 0) & d) |                                            // a .git leaf, at any depth
		((bits & SB_GITNAME) != 0) |                                // git's names, at any depth
		(d & ((child == CC_HOOKS) |                                 // .git/hooks/**
			(child == CC_CONFIG) |                              // .git/config** (the pair)
			((i == 1) & (child == CC_CFGLEAF)) |                // .git/config.worktree|commondir
			((child == CC_MODULES) & (i >= 3) & (s->hk2 | s->leaf_config)) | // submodule
			((child == CC_WORKTREES) & (i >= 3) & s->leaf_config))); // linked worktree config
	// A moved directory carries its whole tree, which neither of its paths names: it may
	// not take or leave a .claude leaf, nor a place at or under .git/modules or .git/worktrees.
	s->moved_key |= ((i == 0) & ((bits & SB_CLAUDE) != 0)) |
		(d & ((child == CC_MODULES) | (child == CC_WORKTREES)));
	refuse |= s->dir & s->moved_key;
	// A shell's, editor's or agent's name counts unless a dependency folder lies above it.
	s->pending = (s->pending & ((bits & SB_DEP) == 0)) | ((bits & SB_PROTECTED) != 0) |
		(((bits & SB_CLAUDE) != 0) & (child == CC_CMDAGENT));
	if (i == 0)
		s->leaf_config = (bits & (SB_CONFIG | SB_CFGLEAF)) != 0;
	s->hk2 = s->hk1;
	s->hk1 = s->any_hooks;
	s->any_hooks |= (bits & SB_HOOKS) != 0;
	s->child = (bits & SB_HOOKS) ? CC_HOOKS : (bits & SB_CONFIG) ? CC_CONFIG
		: (bits & SB_CFGLEAF) ? CC_CFGLEAF : (bits & SB_MODULES) ? CC_MODULES
		: (bits & SB_WORKTREES) ? CC_WORKTREES : (bits & SB_CMDAGENT) ? CC_CMDAGENT : CC_OTHER;
	s->i++;
	return refuse;
}

// Once the walk has ended, at the mount root or at SG_WALK: whether a name sg_step left
// pending is refused. One whose dependency folder lies past SG_WALK is refused too.
SG_INLINE int sg_end(const struct sg_state *s)
{
	return s->pending;
}

#endif
