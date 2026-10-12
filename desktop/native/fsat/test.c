// The numbers fsat gives for Darwin's flags, as its headers define them, written out by a program that is not fsat:
// test/fd-paths-darwin.test.ts holds fsat's table against them, name for name. Built by build.sh beside fsat.

#include <fcntl.h>
#include <stdio.h>
#include <unistd.h>

#define SAY(name) printf("%s\"%s\":%d", first ? "" : ",", #name, (int)(name)), first = 0

int main(void) {
  int first = 1;
  printf("{");
  SAY(O_RDONLY); SAY(O_WRONLY); SAY(O_RDWR); SAY(O_CREAT); SAY(O_EXCL); SAY(O_TRUNC); SAY(O_NONBLOCK); SAY(O_DIRECTORY);
  SAY(O_NOFOLLOW); SAY(O_NOFOLLOW_ANY); SAY(O_CLOEXEC); SAY(O_EXLOCK); SAY(O_SYMLINK); SAY(AT_FDCWD); SAY(AT_SYMLINK_NOFOLLOW);
  SAY(AT_REMOVEDIR); SAY(RENAME_EXCL); SAY(FD_CLOEXEC);
#ifdef O_SEARCH
  SAY(O_SEARCH);
#endif
  printf("}\n");
  return 0;
}
