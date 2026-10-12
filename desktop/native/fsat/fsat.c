// fsat: the file host's calls relative to a folder it holds open, on macOS (src/files/fd-paths-darwin.ts).
//
// Linux reaches a name in a held folder by the descriptor's entry in /proc/self/fd, which macOS has
// no counterpart of. Darwin has the *at calls instead, which Node does not expose: each here takes
// the held folder's descriptor and one name in it, never a path through it, so nothing put on the
// folder's path since is followed. The only path any call takes is an absolute one, with AT_FDCWD.
// A held folder's entries need no call of fsat's: Node lists one by its /dev/fd entry, whose open
// on macOS is a dup of the descriptor (fd(4)).
//
// Loaded by the app's own node alone, in the file host and in its helper: Electron's processes never
// load it. N-API 8, and nothing else. Each call is synchronous, as Node's *Sync calls are, and throws
// an Error with the call's name and the negative errno, as libuv numbers them, which its loader
// (src/files/fsat.ts) makes into Node's own form. Apple's documentation of each call is its man page,
// named at the call.

#define NAPI_VERSION 8
#include <node_api.h>

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include <sys/param.h>
#include <sys/stat.h>
#include <unistd.h>

// One JavaScript call's arguments: at most five.
#define MOST_ARGS 5

static napi_value fail(napi_env env, int code, const char *syscall) {
  napi_value message, error, number, name;
  napi_create_string_utf8(env, syscall, NAPI_AUTO_LENGTH, &message);
  napi_create_error(env, NULL, message, &error);
  napi_create_int32(env, -code, &number);
  napi_set_named_property(env, error, "errno", number);
  napi_create_string_utf8(env, syscall, NAPI_AUTO_LENGTH, &name);
  napi_set_named_property(env, error, "syscall", name);
  napi_throw(env, error);
  return NULL;
}

static int args(napi_env env, napi_callback_info info, size_t want, napi_value *out) {
  size_t got = MOST_ARGS;
  if (napi_get_cb_info(env, info, &got, out, NULL, NULL) != napi_ok) return 0;
  if (got < want) {
    napi_throw_type_error(env, NULL, "fsat: too few arguments");
    return 0;
  }
  return 1;
}

static int whole(napi_env env, napi_value value, int32_t *out) {
  if (napi_get_value_int32(env, value, out) != napi_ok) {
    napi_throw_type_error(env, NULL, "fsat: a descriptor, flags and a mode are numbers");
    return 0;
  }
  return 1;
}

// A name in the folder held as *dir*: one name, or "." for the folder itself; never "", "..", or one
// holding "/" or NUL. With AT_FDCWD, an absolute path instead. Anything else is refused before a call,
// with EINVAL.
static int named(napi_env env, napi_value value, int dir, char *out, size_t size, const char *syscall) {
  size_t length = 0;
  if (napi_get_value_string_utf8(env, value, out, size, &length) != napi_ok) {
    napi_throw_type_error(env, NULL, "fsat: a name is a string");
    return 0;
  }
  if (length + 1 >= size) {
    fail(env, ENAMETOOLONG, syscall);
    return 0;
  }
  int ok = length == strlen(out) && length > 0;
  if (dir == AT_FDCWD) ok = ok && out[0] == '/';
  else ok = ok && strchr(out, '/') == NULL && strcmp(out, "..") != 0;
  if (!ok) {
    fail(env, EINVAL, syscall);
    return 0;
  }
  return 1;
}

static int is_null(napi_env env, napi_value value) {
  napi_valuetype type;
  return napi_typeof(env, value, &type) == napi_ok && (type == napi_null || type == napi_undefined);
}

static napi_value number(napi_env env, int64_t value) {
  napi_value out;
  napi_create_int64(env, value, &out);
  return out;
}

static napi_value big(napi_env env, uint64_t value) {
  napi_value out;
  napi_create_bigint_uint64(env, value, &out);
  return out;
}

static void put(napi_env env, napi_value object, const char *key, napi_value value) {
  napi_set_named_property(env, object, key, value);
}

static void put_time(napi_env env, napi_value object, const char *sec, const char *nsec, struct timespec at) {
  put(env, object, sec, number(env, (int64_t)at.tv_sec));
  put(env, object, nsec, number(env, (int64_t)at.tv_nsec));
}

// A stat as libuv gives Node one: each integer as an unsigned 64-bit number (Node's own conversion of
// Darwin's signed dev_t), each time as its seconds and nanoseconds.
static napi_value stats(napi_env env, const struct stat *st) {
  napi_value out;
  napi_create_object(env, &out);
  put(env, out, "dev", big(env, (uint64_t)st->st_dev));
  put(env, out, "ino", big(env, (uint64_t)st->st_ino));
  put(env, out, "mode", big(env, (uint64_t)st->st_mode));
  put(env, out, "nlink", big(env, (uint64_t)st->st_nlink));
  put(env, out, "uid", big(env, (uint64_t)st->st_uid));
  put(env, out, "gid", big(env, (uint64_t)st->st_gid));
  put(env, out, "rdev", big(env, (uint64_t)st->st_rdev));
  put(env, out, "size", big(env, (uint64_t)st->st_size));
  put(env, out, "blksize", big(env, (uint64_t)st->st_blksize));
  put(env, out, "blocks", big(env, (uint64_t)st->st_blocks));
  put_time(env, out, "atimeSec", "atimeNsec", st->st_atimespec);
  put_time(env, out, "mtimeSec", "mtimeNsec", st->st_mtimespec);
  put_time(env, out, "ctimeSec", "ctimeNsec", st->st_ctimespec);
  put_time(env, out, "birthtimeSec", "birthtimeNsec", st->st_birthtimespec);
  return out;
}

// openat(2): the name in *dir*, opened, close-on-exec always, as libuv opens every file.
static napi_value js_openat(napi_env env, napi_callback_info info) {
  napi_value argv[MOST_ARGS];
  int32_t dir, flags, mode;
  char name[PATH_MAX + 1];
  if (!args(env, info, 4, argv) || !whole(env, argv[0], &dir) || !named(env, argv[1], dir, name, sizeof name, "openat")
      || !whole(env, argv[2], &flags) || !whole(env, argv[3], &mode)) return NULL;
  int fd = openat(dir, name, flags | O_CLOEXEC, (mode_t)mode);
  if (fd < 0) return fail(env, errno, "openat");
  return number(env, fd);
}

// fstatat(2), or with no name, fstat(2) of *dir* itself.
static napi_value js_fstatat(napi_env env, napi_callback_info info) {
  napi_value argv[MOST_ARGS];
  int32_t dir, flags;
  char name[PATH_MAX + 1];
  struct stat st;
  if (!args(env, info, 3, argv) || !whole(env, argv[0], &dir) || !whole(env, argv[2], &flags)) return NULL;
  if (is_null(env, argv[1])) {
    if (fstat(dir, &st) != 0) return fail(env, errno, "fstat");
    return stats(env, &st);
  }
  if (!named(env, argv[1], dir, name, sizeof name, "fstatat")) return NULL;
  if (fstatat(dir, name, &st, flags) != 0) return fail(env, errno, "fstatat");
  return stats(env, &st);
}

// faccessat(2).
static napi_value js_faccessat(napi_env env, napi_callback_info info) {
  napi_value argv[MOST_ARGS];
  int32_t dir, mode, flags;
  char name[PATH_MAX + 1];
  if (!args(env, info, 4, argv) || !whole(env, argv[0], &dir) || !named(env, argv[1], dir, name, sizeof name, "faccessat")
      || !whole(env, argv[2], &mode) || !whole(env, argv[3], &flags)) return NULL;
  if (faccessat(dir, name, mode, flags) != 0) return fail(env, errno, "faccessat");
  return NULL;
}

// fchmodat(2), or with no name, fchmod(2) of *dir* itself.
static napi_value js_fchmodat(napi_env env, napi_callback_info info) {
  napi_value argv[MOST_ARGS];
  int32_t dir, mode, flags;
  char name[PATH_MAX + 1];
  if (!args(env, info, 4, argv) || !whole(env, argv[0], &dir) || !whole(env, argv[2], &mode) || !whole(env, argv[3], &flags)) return NULL;
  if (is_null(env, argv[1])) {
    if (fchmod(dir, (mode_t)mode) != 0) return fail(env, errno, "fchmod");
    return NULL;
  }
  if (!named(env, argv[1], dir, name, sizeof name, "fchmodat")) return NULL;
  if (fchmodat(dir, name, (mode_t)mode, flags) != 0) return fail(env, errno, "fchmodat");
  return NULL;
}

// mkdirat(2).
static napi_value js_mkdirat(napi_env env, napi_callback_info info) {
  napi_value argv[MOST_ARGS];
  int32_t dir, mode;
  char name[PATH_MAX + 1];
  if (!args(env, info, 3, argv) || !whole(env, argv[0], &dir) || !named(env, argv[1], dir, name, sizeof name, "mkdirat")
      || !whole(env, argv[2], &mode)) return NULL;
  if (mkdirat(dir, name, (mode_t)mode) != 0) return fail(env, errno, "mkdirat");
  return NULL;
}

// unlinkat(2): a file, or with AT_REMOVEDIR an empty folder.
static napi_value js_unlinkat(napi_env env, napi_callback_info info) {
  napi_value argv[MOST_ARGS];
  int32_t dir, flags;
  char name[PATH_MAX + 1];
  if (!args(env, info, 3, argv) || !whole(env, argv[0], &dir) || !named(env, argv[1], dir, name, sizeof name, "unlinkat")
      || !whole(env, argv[2], &flags)) return NULL;
  if (unlinkat(dir, name, flags) != 0) return fail(env, errno, "unlinkat");
  return NULL;
}

// renameatx_np(2): renameat with Darwin's flags, RENAME_EXCL among them ("the rename fails with
// EEXIST if the destination already exists").
static napi_value js_renameatx(napi_env env, napi_callback_info info) {
  napi_value argv[MOST_ARGS];
  int32_t from_dir, to_dir, flags;
  char from[PATH_MAX + 1], to[PATH_MAX + 1];
  if (!args(env, info, 5, argv) || !whole(env, argv[0], &from_dir) || !named(env, argv[1], from_dir, from, sizeof from, "renameatx_np")
      || !whole(env, argv[2], &to_dir) || !named(env, argv[3], to_dir, to, sizeof to, "renameatx_np") || !whole(env, argv[4], &flags)) return NULL;
  if (renameatx_np(from_dir, from, to_dir, to, (unsigned int)flags) != 0) return fail(env, errno, "renameatx_np");
  return NULL;
}

// linkat(2): with no AT_SYMLINK_FOLLOW, a link is linked as itself, as Linux's link(2) does.
static napi_value js_linkat(napi_env env, napi_callback_info info) {
  napi_value argv[MOST_ARGS];
  int32_t from_dir, to_dir, flags;
  char from[PATH_MAX + 1], to[PATH_MAX + 1];
  if (!args(env, info, 5, argv) || !whole(env, argv[0], &from_dir) || !named(env, argv[1], from_dir, from, sizeof from, "linkat")
      || !whole(env, argv[2], &to_dir) || !named(env, argv[3], to_dir, to, sizeof to, "linkat") || !whole(env, argv[4], &flags)) return NULL;
  if (linkat(from_dir, from, to_dir, to, flags) != 0) return fail(env, errno, "linkat");
  return NULL;
}

// fcntl(2) F_GETPATH: the path the file open as *fd* has now.
static napi_value js_getpath(napi_env env, napi_callback_info info) {
  napi_value argv[MOST_ARGS];
  int32_t fd;
  char path[MAXPATHLEN];
  if (!args(env, info, 1, argv) || !whole(env, argv[0], &fd)) return NULL;
  if (fcntl(fd, F_GETPATH, path) == -1) return fail(env, errno, "fcntl");
  napi_value out;
  napi_create_string_utf8(env, path, NAPI_AUTO_LENGTH, &out);
  return out;
}

// fcntl(2) F_GETFD: whether *fd* is close-on-exec; null for a descriptor that is not open.
static napi_value js_cloexec(napi_env env, napi_callback_info info) {
  napi_value argv[MOST_ARGS];
  int32_t fd;
  if (!args(env, info, 1, argv) || !whole(env, argv[0], &fd)) return NULL;
  int flags = fcntl(fd, F_GETFD);
  napi_value out;
  if (flags == -1) {
    if (errno != EBADF) return fail(env, errno, "fcntl");
    napi_get_null(env, &out);
    return out;
  }
  napi_get_boolean(env, (flags & FD_CLOEXEC) != 0, &out);
  return out;
}

static void constant(napi_env env, napi_value object, const char *name, int64_t value) {
  put(env, object, name, number(env, value));
}

NAPI_MODULE_INIT() {
  napi_value constants;
  napi_create_object(env, &constants);
  // Each as Darwin's headers define it: never a number written here, and never Linux's.
  constant(env, constants, "O_RDONLY", O_RDONLY);
  constant(env, constants, "O_WRONLY", O_WRONLY);
  constant(env, constants, "O_RDWR", O_RDWR);
  constant(env, constants, "O_CREAT", O_CREAT);
  constant(env, constants, "O_EXCL", O_EXCL);
  constant(env, constants, "O_TRUNC", O_TRUNC);
  constant(env, constants, "O_NONBLOCK", O_NONBLOCK);
  constant(env, constants, "O_DIRECTORY", O_DIRECTORY);
  constant(env, constants, "O_NOFOLLOW", O_NOFOLLOW);
  // open(2): "O_NOFOLLOW_ANY do not follow symlinks in the entire path".
  constant(env, constants, "O_NOFOLLOW_ANY", O_NOFOLLOW_ANY);
  constant(env, constants, "O_CLOEXEC", O_CLOEXEC);
  // open(2): "O_EXLOCK atomically obtain an exclusive lock".
  constant(env, constants, "O_EXLOCK", O_EXLOCK);
  constant(env, constants, "O_SYMLINK", O_SYMLINK);
  constant(env, constants, "AT_FDCWD", AT_FDCWD);
  constant(env, constants, "AT_SYMLINK_NOFOLLOW", AT_SYMLINK_NOFOLLOW);
  constant(env, constants, "AT_REMOVEDIR", AT_REMOVEDIR);
  constant(env, constants, "RENAME_EXCL", RENAME_EXCL);
  constant(env, constants, "FD_CLOEXEC", FD_CLOEXEC);
#ifdef O_SEARCH
  // open(2): "O_SEARCH open directory for search only", where the SDK has it: not used yet, only measured.
  constant(env, constants, "O_SEARCH", O_SEARCH);
#endif
  put(env, exports, "constants", constants);

  const struct { const char *name; napi_callback call; } calls[] = {
    { "openat", js_openat }, { "fstatat", js_fstatat }, { "faccessat", js_faccessat }, { "fchmodat", js_fchmodat },
    { "mkdirat", js_mkdirat }, { "unlinkat", js_unlinkat }, { "renameatx", js_renameatx }, { "linkat", js_linkat },
    { "getpath", js_getpath }, { "cloexec", js_cloexec },
  };
  for (size_t i = 0; i < sizeof calls / sizeof calls[0]; i += 1) {
    napi_value fn;
    napi_create_function(env, calls[i].name, NAPI_AUTO_LENGTH, calls[i].call, NULL, &fn);
    put(env, exports, calls[i].name, fn);
  }
  return exports;
}
