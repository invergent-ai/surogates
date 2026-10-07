// Native harness for the drift test: judge a share-relative path, printing 1
// (refuse) or 0 (allow), feeding components leaf-first to the shared stepper.
#include <stdio.h>
#include <string.h>
#include "rule-match.h"

static int refuses(const char *path)
{
	char buf[4096];
	int plen = strlen(path);
	if (plen >= (int)sizeof buf) plen = sizeof buf - 1;
	memcpy(buf, path, plen); buf[plen] = 0;
	char *comps[256]; int nc = 0;
	for (char *p = strtok(buf, "/"); p && nc < 256; p = strtok(NULL, "/")) comps[nc++] = p;
	struct sg_state st; sg_init(&st);
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
	for (int i = 1; i < argc; i++) printf("%d %s\n", refuses(argv[i]), argv[i]);
	return 0;
}
