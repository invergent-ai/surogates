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
// The bytes of a component read for its fold (sg_fold), its NUL among them. The most a protected name
// takes, written with the characters sg_fold takes to its letters, is 18 (config.worktree, with ı for
// its i and the Kelvin sign for its k): a name too long to be read whole folds to none.
#define SG_RAWLEN 32

#ifdef __bpf__
#define SG_INLINE static __attribute__((always_inline)) inline
#define SG_NOINLINE __attribute__((noinline))
#define SG_ARG_NONNULL __attribute__((btf_decl_tag("arg:nonnull")))
// What it hides the compiler cannot see through, so arithmetic on a name's bytes is never turned into
// a branch, which would fork the verifier at every byte.
#define SG_OPAQUE(x) asm volatile("" : "+r"(x))
#else
#define SG_INLINE static inline
#define SG_NOINLINE static
#define SG_ARG_NONNULL
#define SG_OPAQUE(x) ((void)0)
#endif

struct sg_name { char b[SG_NAMELEN]; };
// A component as read, NUL-terminated, with two NULs past SG_RAWLEN for sg_fold's look ahead.
struct sg_raw { unsigned char b[SG_RAWLEN + 2]; };

// 1 when x is 0, else 0.
SG_INLINE unsigned sg_zero(unsigned x)
{
	unsigned t = x | (0u - x);
	SG_OPAQUE(t);
	t >>= 31;
	SG_OPAQUE(t);
	return t ^ 1u;
}

// 1 when x < y, for x and y below 2^31.
SG_INLINE unsigned sg_below(unsigned x, unsigned y)
{
	unsigned t = x - y;
	SG_OPAQUE(t);
	return t >> 31;
}

// The component *raw*, *len* bytes, into *nm* as a volume that compares names without case may compare
// it, as files/protect.ts's folded does: ASCII lower-cased; ſ (U+017F) as s; ı (U+0131) and İ (U+0130) as
// i; ß (U+00DF) and ẞ (U+1E9E) as ss; the Kelvin sign (U+212A) as k; the ligatures U+FB00 to U+FB06 as the
// letters they join. Returns the folded name's length, or *len* for a name too long to be read whole.
// Branch-free (SG_OPAQUE), and writing *nm* at fixed places alone: each byte of a name is a value here,
// never a branch or an address, so the verifier walks one path, once, for every name. Each character
// writes its letters at *o*, where the folded name has got to: each place k of *nm* takes the letter, of
// whichever character's, that lands at k.
SG_NOINLINE int sg_fold(const struct sg_raw *raw SG_ARG_NONNULL, int len, struct sg_name *nm SG_ARG_NONNULL)
{
	unsigned o = 0, skip = 0;
	for (int k = 0; k < SG_NAMELEN; k++)
		nm->b[k] = 0;
	for (int j = 0; j < SG_RAWLEN; j++) {
		unsigned b0 = raw->b[j], b1 = raw->b[j + 1], b2 = raw->b[j + 2];
		unsigned two = b0 << 8 | b1, three = two << 8 | b2;
		unsigned long_s = sg_zero(two ^ 0xc5bf);
		unsigned dotless = sg_zero(two ^ 0xc4b1) | sg_zero(two ^ 0xc4b0);
		unsigned sharp = sg_zero(two ^ 0xc39f);
		unsigned kelvin = sg_zero(three ^ 0xe284aa);
		unsigned capital_sharp = sg_zero(three ^ 0xe1ba9e);
		// U+FB00 + l, for l from 0 to 6: ff, fi, fl, ffi, ffl, st, st.
		unsigned l = (b2 - 0x80u) & 0xffu;
		unsigned lig = sg_zero((three >> 8) ^ 0xefac) & sg_below(l, 7);
		unsigned l3 = sg_zero(l ^ 3), l4 = sg_zero(l ^ 4), st = sg_zero(l ^ 5) | sg_zero(l ^ 6);
		unsigned lig0 = 'f' + st * ('s' - 'f');
		unsigned lig1 = (sg_zero(l) | l3 | l4) * 'f' + sg_zero(l ^ 1) * 'i' + sg_zero(l ^ 2) * 'l' + st * 't';
		unsigned lig2 = l3 * 'i' + l4 * 'l';
		unsigned paired = long_s | dotless | sharp, tripled = kelvin | capital_sharp | lig;
		unsigned plain = 1 - paired - tripled;
		unsigned upper = sg_below('A' - 1, b0) & sg_below(b0, 'Z' + 1);
		unsigned at0 = plain * (b0 + (upper << 5)) + (long_s | sharp | capital_sharp) * 's' + dotless * 'i' + kelvin * 'k' + lig * lig0;
		unsigned at1 = (sharp | capital_sharp) * 's' + lig * lig1;
		unsigned at2 = lig * lig2;
		unsigned count = plain + long_s + dotless + kelvin + 2 * (sharp | capital_sharp) + lig * (2 + l3 + l4);
		// A byte of the name, and none of a character taken already.
		unsigned active = sg_below(j, len) & sg_zero(skip);
		for (int k = 0; k < SG_NAMELEN; k++)
			nm->b[k] = (char)(nm->b[k] + active * (sg_zero(o ^ k) * at0 + sg_zero((o + 1) ^ k) * at1 + sg_zero((o + 2) ^ k) * at2));
		o += active * count;
		skip = active * (plain + 2 * paired + 3 * tripled - 1) + (1 - active) * (skip - 1 + sg_zero(skip));
	}
	unsigned whole = sg_below(len, SG_RAWLEN);
	return (int)(whole * o + (1 - whole) * (unsigned)len);
}

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
// name lies below, with no dependency folder above it yet. in_dep: a dependency folder is
// on the path, the path itself included.
struct sg_state { int i; int child; int any_hooks; int hk1; int hk2; int leaf_config; int dir; int moved_key; int pending; int in_dep; };

SG_INLINE void sg_init(struct sg_state *s, int dir)
{
	s->i = 0; s->child = CC_OTHER; s->any_hooks = 0; s->hk1 = 0; s->hk2 = 0; s->leaf_config = 0;
	s->dir = dir; s->moved_key = 0; s->pending = 0; s->in_dep = 0;
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
	s->in_dep |= (bits & SB_DEP) != 0;
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

// Whether the walked path lies at or below a dependency folder, within SG_WALK.
SG_INLINE int sg_in_dep(const struct sg_state *s)
{
	return s->in_dep;
}

// A directory moved out of a dependency folder (old_dep, not new_dep) carries what was unpacked
// there, unjudged, to where a shell's, editor's or agent's name counts; an exchange (RENAME_EXCHANGE)
// moves both ways. Refused. A dependency folder past SG_WALK is not seen, but nothing below that
// bound can hold a name sg_end let through.
SG_INLINE int sg_moved_out(int dir, int exchange, int old_dep, int new_dep)
{
	return dir & ((old_dep & (new_dep ^ 1)) | (exchange & (old_dep ^ 1) & new_dep));
}

#endif
