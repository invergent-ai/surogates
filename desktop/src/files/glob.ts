// Which files a search includes, when the helper lists the tree itself and gives a program only files (Windows: the
// walk, fd-paths-win32.ts). On Linux and macOS ripgrep walks the folder and applies its own `-g` globs; on Windows rg
// is given explicit files, which it searches whatever its globs say, so the helper must choose the files rg would have
// chosen. These are ripgrep's `-g` globs, which are gitignore's (the globset/ignore crates): a glob with no slash
// matches a name at any depth, one with a slash is anchored to the search root, `*` matches within a name, `**` any run
// of names, `?` one character, `[...]` a class, a leading `!` negates, and a negated directory takes its whole subtree.
// The oracle is ripgrep's own output on Linux: a cross-check test holds this to it over a corpus (glob-win32.test.ts).
// What it does not reproduce is ripgrep's whitelisting of a hidden directory by an anchored glob that names only its
// contents (`-g '.git/**'` surfaces nothing in ripgrep, since the glob does not name `.git` itself); the file tools
// search code, where that does not arise, and the divergence lets nothing outside the folder in.

// One path component matched against one glob segment (no slash in either): * is any run but a slash, ? one character
// but a slash, [set] a class (a leading ! or ^ negates it), everything else literal.
function segment(name: string, pattern: string): boolean {
  // The regex is built with character classes kept whole, so a ] or a - inside one is not escaped as a literal.
  let regex = "^";
  for (let i = 0; i < pattern.length; i += 1) {
    const ch = pattern[i]!;
    if (ch === "*") regex += "[^/]*";
    else if (ch === "?") regex += "[^/]";
    else if (ch === "[") {
      let end = i + 1;
      if (pattern[end] === "!" || pattern[end] === "^") end += 1;
      if (pattern[end] === "]") end += 1;
      while (end < pattern.length && pattern[end] !== "]") end += 1;
      if (end >= pattern.length) {
        regex += "\\[";
      } else {
        const body = pattern.slice(i + 1, end).replace(/^[!^]/, "^").replace(/\\/g, "\\\\");
        regex += `[${body}]`;
        i = end;
      }
    } else regex += ch.replace(/[.+^${}()|\\]/g, "\\$&");
  }
  return new RegExp(`${regex}$`).test(name);
}

// A path's segments against a glob's, with `**` matching any run of segments, including none.
function segments(path: readonly string[], glob: readonly string[]): boolean {
  if (glob.length === 0) return path.length === 0;
  if (glob[0] === "**") {
    for (let i = 0; i <= path.length; i += 1) if (segments(path.slice(i), glob.slice(1))) return true;
    return false;
  }
  if (path.length === 0) return false;
  return segment(path[0]!, glob[0]!) && segments(path.slice(1), glob.slice(1));
}

/** Whether *path* (a POSIX relative path) matches *glob* (no leading `!`), by ripgrep's `-g` rules. */
export function matchGlob(path: string, glob: string): boolean {
  const anchored = glob.startsWith("/") || glob.replace(/\/+$/, "").includes("/");
  const body = glob.replace(/^\/+/, "").replace(/\/+$/, "");
  if (anchored) return segments(path.split("/"), body.split("/"));
  // No slash: ripgrep matches the name at any depth, which is the last component here.
  return segment(path.split("/").at(-1)!, body);
}

/** The relative paths of a path's ancestor directories: a/b/c → ["a", "a/b"]. */
function ancestors(path: string): string[] {
  const parts = path.split("/");
  return parts.slice(0, -1).map((_, i) => parts.slice(0, i + 1).join("/"));
}

/**
 * Whether a file at *path* (a POSIX relative path under the search root) is one the search includes, for a single
 * ripgrep `-g` *glob* or none: a positive glob includes only what it matches; a negated glob (a leading `!`) includes
 * all but what it or an ancestor directory matches, as gitignore excludes a directory's whole subtree. Hidden files are
 * the walk's to drop (fd-paths-win32.ts): this is only the glob.
 */
export function included(path: string, glob: string | null): boolean {
  if (glob === null) return true;
  if (glob.startsWith("!")) {
    const body = glob.slice(1);
    return !(matchGlob(path, body) || ancestors(path).some((dir) => matchGlob(dir, body)));
  }
  return matchGlob(path, glob);
}

/**
 * Whether the search descends into a hidden directory at *path* for *glob*: only a positive glob that whitelists the
 * directory itself does, as ripgrep's does. A non-hidden directory is always descended, which the caller decides.
 */
export function descendsHidden(path: string, glob: string | null): boolean {
  if (glob === null || glob.startsWith("!")) return false;
  // The directory is whitelisted when the glob matches it, or (anchored) names it as a prefix of what it matches.
  const anchored = glob.startsWith("/") || glob.replace(/\/+$/, "").includes("/");
  if (!anchored) return segment(path.split("/").at(-1)!, glob.replace(/\/+$/, ""));
  const body = glob.replace(/^\/+/, "").replace(/\/+$/, "").split("/");
  const dir = path.split("/");
  // Each of the glob's leading non-`**` segments must match the directory's, as far as the directory goes.
  for (let i = 0; i < dir.length; i += 1) {
    if (i >= body.length) return false;
    if (body[i] === "**") return true;
    if (!segment(dir[i]!, body[i]!)) return false;
  }
  return true;
}
