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
// The bytes of a component sg_fold reads, a byte at a time: PATH_MAX, so every name a path names is read whole. A name
// is judged by what is left of it once a volume drops what it holds, which may be most of it.
#define SG_RAWLEN 4096

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

#ifdef __bpf__
// A name, by its address in the kernel, read a byte at a time; and bpf_for's loop over its bytes, which the verifier
// walks until the loop's state stops changing, not once for each byte.
typedef unsigned long sg_text;
SG_INLINE unsigned sg_byte(sg_text name, int at)
{
	unsigned char c = 0;
	bpf_probe_read_kernel(&c, 1, (const void *)(name + at));
	return c;
}
#define SG_EACH(j, end) bpf_for(j, 0, end)
#else
typedef const unsigned char *sg_text;
SG_INLINE unsigned sg_byte(sg_text name, int at)
{
	return name[at];
}
#define SG_EACH(j, end) for ((j) = 0; (j) < (end); (j)++)
#endif

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

// One character of a name, from its first byte *b0* and the two after it, *b1* and *b2*, as sg_fold takes it to
// letters: ASCII lower-cased; ſ (U+017F) as s; ı (U+0131) and İ (U+0130) as i; ß (U+00DF) and ẞ (U+1E9E) as ss; the
// Kelvin sign (U+212A) as k; the ligatures U+FB00 to U+FB06 as the letters they join; and, as none, the code points
// HFS+'s comparison of names ignores, U+200C to U+200F, U+202A to U+202E, U+206A to U+206F and U+FEFF (Apple's
// Technical Note TN1150, "HFS Plus Volume Format", its case-insensitive string comparison). Each value as sg_fold's:
// arithmetic, never a branch. *at0* to *at2*, its letters; *count*, how many; *span*, its bytes; *trails*, 1 for a dot
// or a space, which FAT's and exFAT's volumes drop from a name's end.
struct sg_char { unsigned at0, at1, at2, count, span, trails; };
SG_INLINE struct sg_char sg_char(unsigned b0, unsigned b1, unsigned b2)
{
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
	// What HFS+ ignores, as UTF-8 writes it: e2 80 8c to e2 80 8f, e2 80 aa to e2 80 ae, e2 81 aa to e2 81 af, and ef bb bf.
	unsigned ignored = sg_below((three - 0xe2808cu) & 0xffffffu, 4) | sg_below((three - 0xe280aau) & 0xffffffu, 5)
		| sg_below((three - 0xe281aau) & 0xffffffu, 6) | sg_zero(three ^ 0xefbbbf);
	unsigned paired = long_s | dotless | sharp, tripled = kelvin | capital_sharp | lig | ignored;
	unsigned plain = 1 - paired - tripled;
	unsigned upper = sg_below('A' - 1, b0) & sg_below(b0, 'Z' + 1);
	struct sg_char c;
	c.at0 = plain * (b0 + (upper << 5)) + (long_s | sharp | capital_sharp) * 's' + dotless * 'i' + kelvin * 'k' + lig * lig0;
	c.at1 = (sharp | capital_sharp) * 's' + lig * lig1;
	c.at2 = lig * lig2;
	c.count = plain + long_s + dotless + kelvin + 2 * (sharp | capital_sharp) + lig * (2 + l3 + l4);
	c.span = plain + 2 * paired + 3 * tripled;
	c.trails = plain & (sg_zero(b0 ^ '.') | sg_zero(b0 ^ ' '));
	return c;
}

// The component *name*, *len* bytes, into *nm* as the volume a folder lies on may take it, as files/protect.ts's
// folded does: each character as sg_char takes it, then without the dots and spaces the name ends with. Returns the
// folded name's length, its letters past SG_NAMELEN left out of *nm*. Read whole, so a name that what a volume drops
// makes long is judged as the name left.
// Branch-free (SG_OPAQUE) but for where the name ends, and writing *nm* at fixed places alone: each byte of a name is
// a value here, never a branch or an address, so the verifier walks one path for every name, and the loop until that
// path's state stops changing. Each character writes its letters at *o*, where the folded name has got to: each place
// k of *nm* takes the letter, of whichever character's, that lands at k. *kept* is where it had got to after the last
// character that is no dot, space or code point HFS+ ignores.
SG_NOINLINE int sg_fold(sg_text name, int len, struct sg_name *nm SG_ARG_NONNULL)
{
	unsigned o = 0, kept = 0, skip = 0, b1 = 0, b2 = 0;
	int j;
	for (int k = 0; k < SG_NAMELEN; k++)
		nm->b[k] = 0;
	if (len > 0)
		b1 = sg_byte(name, 0);
	if (len > 1)
		b2 = sg_byte(name, 1);
	SG_EACH(j, SG_RAWLEN) {
		if (j >= len)
			break;
		unsigned b0 = b1;
		b1 = b2;
		b2 = j + 2 < len ? sg_byte(name, j + 2) : 0;
		struct sg_char c = sg_char(b0, b1, b2);
		// None of a character taken already.
		unsigned active = sg_zero(skip);
		for (int k = 0; k < SG_NAMELEN; k++)
			nm->b[k] = (char)(nm->b[k] + active * (sg_zero(o ^ k) * c.at0 + sg_zero((o + 1) ^ k) * c.at1 + sg_zero((o + 2) ^ k) * c.at2));
		o += active * c.count;
		unsigned keeps = active * (1 - c.trails) * (1 - sg_zero(c.count));
		kept = keeps * o + (1 - keeps) * kept;
		skip = active * (c.span - 1) + (1 - active) * (skip - 1);
	}
	return (int)kept;
}

// The names Windows's Explorer acts on when it shows a folder (files/explorer-names.ts), which the rule refuses
// besides the protected ones where the kernel's line names surogate.names=windows: desktop.ini, autorun.inf, and
// a name ending .lnk, .url, .scf, .library-ms or .searchconnector-ms. Each judged as a protected name is, by what is
// left of the name once a volume drops what it holds (sg_fold), read whole: by that name's last SG_ENDLEN letters,
// more than the longest of them, .searchconnector-ms, takes.
#define SG_ENDLEN 24
struct sg_end { char b[SG_ENDLEN]; };

// The component *name*, *len* bytes, folded as sg_fold folds it to its *kept* letters (sg_fold's answer), its last
// SG_ENDLEN letters into *end* right-aligned: its last letter at end->b[SG_ENDLEN - 1]. sg_fold's loop again, so
// each write is at a fixed place as sg_fold's are: each letter lands where it lies from the folded name's end, and
// one past *kept*, which a volume drops, or more than SG_ENDLEN before it, lands at none of them.
SG_NOINLINE int sg_fold_end(sg_text name, int len, int kept, struct sg_end *end SG_ARG_NONNULL)
{
	unsigned o = SG_ENDLEN - (unsigned)kept, skip = 0, b1 = 0, b2 = 0;
	int j;
	for (int k = 0; k < SG_ENDLEN; k++)
		end->b[k] = 0;
	if (len > 0)
		b1 = sg_byte(name, 0);
	if (len > 1)
		b2 = sg_byte(name, 1);
	SG_EACH(j, SG_RAWLEN) {
		if (j >= len)
			break;
		unsigned b0 = b1;
		b1 = b2;
		b2 = j + 2 < len ? sg_byte(name, j + 2) : 0;
		struct sg_char c = sg_char(b0, b1, b2);
		unsigned active = sg_zero(skip);
		for (int k = 0; k < SG_ENDLEN; k++)
			end->b[k] = (char)(end->b[k] + active * (sg_zero(o ^ k) * c.at0 + sg_zero((o + 1) ^ k) * c.at1 + sg_zero((o + 2) ^ k) * c.at2));
		o += active * c.count;
		skip = active * (c.span - 1) + (1 - active) * (skip - 1);
	}
	return kept;
}

// 1 when the folded end *end*, of a name of *total* letters, ends with *lit*, its *n* bytes; branch-free.
SG_INLINE unsigned sg_ends(const struct sg_end *end, unsigned total, const char *lit, int n)
{
	unsigned same = 1 - sg_below(total, (unsigned)n);
	for (int k = 0; k < n; k++)
		same &= sg_zero((unsigned)(unsigned char)end->b[SG_ENDLEN - n + k] ^ (unsigned)(unsigned char)lit[k]);
	return same;
}
#define SG_ENDS(end, total, lit) sg_ends((end), (total), lit, sizeof(lit) - 1)

// Whether the component *name*, its *len* bytes, is one Windows's Explorer acts on, as folded and once a volume
// drops what it holds. Out-of-line on BPF, as sg_classify is.
SG_NOINLINE int sg_explorer(sg_text name, int len)
{
	struct sg_name nm = {};
	struct sg_end end = {};
	unsigned total = (unsigned)sg_fold_end(name, len, sg_fold(name, len, &nm), &end);
	unsigned named = sg_zero(total ^ 11) & (SG_ENDS(&end, total, "desktop.ini") | SG_ENDS(&end, total, "autorun.inf"));
	return (int)(named | SG_ENDS(&end, total, ".lnk") | SG_ENDS(&end, total, ".url") | SG_ENDS(&end, total, ".scf")
		| SG_ENDS(&end, total, ".library-ms") | SG_ENDS(&end, total, ".searchconnector-ms"));
}

// Whether the component *name*, its *len* bytes, is a spelling NTFS may resolve to another name, which the rule
// refuses under surogate.names=windows so HCS's Plan 9 server is not the only wall holding a session's write out
// of a protected or an Explorer name (the file tools refuse the same, files/names-win32.ts): a stream or a path
// separator (':' or '\\'), an 8.3 short name's tail ('~' then a digit), or a trailing dot or space. Read whole, a
// byte at a time, as sg_fold reads a name: what a volume drops can make any name long. Out-of-line on BPF, as
// sg_classify is.
SG_NOINLINE int sg_windows_alias(sg_text name, int len)
{
	unsigned alias = 0, next = 0, c;
	int j;
	if (len > 0)
		next = sg_byte(name, 0);
	SG_EACH(j, SG_RAWLEN) {
		if (j >= len)
			break;
		c = next;
		next = j + 1 < len ? sg_byte(name, j + 1) : 0;
		alias |= sg_zero(c ^ ':') | sg_zero(c ^ '\\') | (sg_zero(c ^ '~') & sg_below((next - '0') & 0xffu, 10));
	}
	if (len > 0) {
		c = sg_byte(name, len - 1);
		alias |= sg_zero(c ^ '.') | sg_zero(c ^ ' ');
	}
	return (int)alias;
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
