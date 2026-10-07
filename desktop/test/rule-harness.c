// Native harness for the drift test: judge share-relative paths, one per stdin line,
// printing one verdict per line, 1 (refuse) or 0 (allow), by feeding each path's
// components leaf-first to the shared stepper. With the argument "dir", each path is
// judged as a directory being moved, as the rule judges both paths of such a rename.
// With "rename" or "exchange", each line is a directory's old and new path, tab-separated,
// judged as on_rename judges them.
#include <stdio.h>
#include <string.h>
#include "rule-match.h"

static int refuses(char *path, int dir, int *in_dep)
{
	char *comps[256]; int nc = 0;
	for (char *p = strtok(path, "/"); p && nc < 256; p = strtok(NULL, "/")) comps[nc++] = p;
	struct sg_state st; sg_init(&st, dir);
	*in_dep = 0;
	for (int i = 0; i < nc && i < SG_WALK; i++) {
		char *c = comps[nc - 1 - i];
		struct sg_name nm; int len = strlen(c);
		for (int j = 0; j < SG_NAMELEN; j++) {
			char ch = j < len ? c[j] : 0;
			if (ch >= 'A' && ch <= 'Z') ch += 32;
			nm.b[j] = ch;
		}
		if (sg_step(&st, sg_classify(&nm, len))) return 1;
	}
	*in_dep = sg_in_dep(&st);
	return sg_end(&st);
}

int main(int argc, char **argv)
{
	const char *mode = argc > 1 ? argv[1] : "";
	int dir = strcmp(mode, "dir") == 0, exchange = strcmp(mode, "exchange") == 0;
	int pairs = exchange || strcmp(mode, "rename") == 0;
	static char line[8192];
	while (fgets(line, sizeof line, stdin)) {
		line[strcspn(line, "\n")] = 0;
		int old_dep, new_dep;
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
