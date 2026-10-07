// Native harness for the drift test: judge share-relative paths, one per stdin line,
// printing one verdict per line, 1 (refuse) or 0 (allow), by feeding each path's
// components leaf-first to the shared stepper. With the argument "dir", each path is
// judged as a directory being moved, as the rule judges both paths of such a rename.
#include <stdio.h>
#include <string.h>
#include "rule-match.h"

static int refuses(char *path, int dir)
{
	char *comps[256]; int nc = 0;
	for (char *p = strtok(path, "/"); p && nc < 256; p = strtok(NULL, "/")) comps[nc++] = p;
	struct sg_state st; sg_init(&st, dir);
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
	return 0;
}

int main(int argc, char **argv)
{
	int dir = argc > 1 && strcmp(argv[1], "dir") == 0;
	static char line[4096];
	while (fgets(line, sizeof line, stdin)) {
		line[strcspn(line, "\n")] = 0;
		printf("%d\n", refuses(line, dir));
	}
	return 0;
}
