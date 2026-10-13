// Native harness for the drift test: judge share-relative paths, one per stdin line,
// printing one verdict per line, 1 (refuse) or 0 (allow), by feeding each path's
// components leaf-first to the shared stepper. With the argument "dir", each path is
// judged as a directory being moved, as the rule judges both paths of such a rename.
// With "rename" or "exchange", each line is a directory's old and new path, tab-separated,
// judged as on_rename judges them. With "fold", each line is one name, printed as sg_fold folds it:
// its folded length, a tab, and its first SG_NAMELEN folded bytes; with "tail", its end as sg_fold_tail
// folds it, its last SG_TAIL bytes or all of a shorter one: its folded length, a tab, and those letters.
// With "windows", each path is judged as with none, and also as the rule judges it with the kernel's line
// naming surogate.names=windows: its last component for an Explorer name (rule.bpf.c, explorer_name), and each
// component for a spelling NTFS may resolve to another name (sg_windows_alias).
#include <stdio.h>
#include <string.h>
#include "rule-match.h"

// A name's end as explorer_name reads it from its dentry: its last SG_TAIL bytes, or the whole of a shorter one.
static int end_of(const char *name, struct sg_tail *tail)
{
	int len = strlen(name);
	memset(tail, 0, sizeof *tail);
	memcpy(tail->b, len > SG_TAIL ? name + len - SG_TAIL : name, len > SG_TAIL ? SG_TAIL : len);
	return len;
}

static int windows;

static int refuses(char *path, int dir, int *in_dep)
{
	char *comps[256]; int nc = 0;
	for (char *p = strtok(path, "/"); p && nc < 256; p = strtok(NULL, "/")) comps[nc++] = p;
	struct sg_state st; sg_init(&st, dir);
	*in_dep = 0;
	if (windows && nc > 0) {
		struct sg_tail tail;
		int len = end_of(comps[nc - 1], &tail);
		if (sg_explorer(&tail, len > SG_TAIL ? SG_TAIL : len)) return 1;
	}
	for (int i = 0; i < nc && i < SG_WALK; i++) {
		char *c = comps[nc - 1 - i];
		// Read as the rule reads a name: at most SG_RAWLEN bytes, its NUL among them.
		struct sg_raw raw = {{0}}; struct sg_name nm; int len = strlen(c);
		memcpy(raw.b, c, len < SG_RAWLEN - 1 ? len : SG_RAWLEN - 1);
		if (windows && sg_windows_alias(&raw, len)) return 1;
		if (sg_step(&st, sg_classify(&nm, sg_fold(&raw, len, &nm)))) return 1;
	}
	*in_dep = sg_in_dep(&st);
	return sg_end(&st);
}

int main(int argc, char **argv)
{
	const char *mode = argc > 1 ? argv[1] : "";
	int dir = strcmp(mode, "dir") == 0, exchange = strcmp(mode, "exchange") == 0;
	int pairs = exchange || strcmp(mode, "rename") == 0, fold = strcmp(mode, "fold") == 0, tail = strcmp(mode, "tail") == 0;
	windows = strcmp(mode, "windows") == 0;
	static char line[8192];
	while (fgets(line, sizeof line, stdin)) {
		line[strcspn(line, "\n")] = 0;
		int old_dep, new_dep;
		if (tail) {
			struct sg_tail end; struct sg_tailname nm;
			int len = end_of(line, &end);
			int folded = sg_fold_tail(&end, len > SG_TAIL ? SG_TAIL : len, &nm);
			printf("%d\t%.*s\n", folded, folded, nm.b + SG_TAILOUT - folded);
			continue;
		}
		if (fold) {
			struct sg_raw raw = {{0}}; struct sg_name nm; int len = strlen(line);
			memcpy(raw.b, line, len < SG_RAWLEN - 1 ? len : SG_RAWLEN - 1);
			int folded = sg_fold(&raw, len, &nm);
			printf("%d\t%.*s\n", folded, folded < SG_NAMELEN ? folded : SG_NAMELEN, nm.b);
			continue;
		}
		if (!pairs) {
			printf("%d\n", refuses(line, dir, &old_dep));
			continue;
		}
		char *tab = strchr(line, '\t');
		if (!tab) return 2;
		*tab = 0;
		int r = refuses(line, 1, &old_dep) || refuses(tab + 1, 1, &new_dep) || sg_moved_out(1, exchange, old_dep, new_dep);
		printf("%d\n", r);
	}
	return 0;
}
