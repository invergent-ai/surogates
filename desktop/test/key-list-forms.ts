// The spellings of a script's list of release keys that each of its readers is asked about: the
// install script's own (listed, in release/install.sh), the release job, which refuses a script
// whose list reads otherwise (release/publish.sh), and the app (releaseKeys, in
// src/shell/updates.ts). One list, as manifest-forms.ts is for a manifest: a release whose
// helper's list one reader takes and another does not is installed, and takes no later release.
//
// The list has one form, said at the list itself in install.sh, and every other spelling is no
// list: all three read no key of it, whatever bash would make of it.

// *script* with *keys* as its list, in the list's form: each a PEM as OpenSSL writes one.
export const inForm = (script: string, keys: string[]) =>
  script.replace(/RELEASE_KEYS=\(\n[^)]*\)/, `RELEASE_KEYS=(\n${keys.map((key) => `    '${key}'`).join("\n")}\n  )`);
// The list of *script* as it is written, and *script* with *list* in its place.
const LIST = /^ *RELEASE_KEYS=\(\n[^)]*\)\n/m;
const rewritten = (script: string, change: (list: string) => string) => script.replace(LIST, (list) => change(list));

// Each spelling: its name, the script it makes of one whose list holds *keys* in the list's form,
// and how many of two keys every reader reads of it. What the install script's reader reads is
// what its signing hands to OpenSSL, as it is written, and what the app makes keys of: a key has
// one spelling, an Ed25519 key's one line as OpenSSL writes it, and every other is no list.
export type KeyList = [name: string, written: (script: string) => string, read: number];
export const KEY_LISTS: KeyList[] = [
  ["as it is", (script) => script, 2],
  ["with spaces after an entry's quote", (script) => rewritten(script, (list) => list.replace("-----END PUBLIC KEY-----'\n", "-----END PUBLIC KEY-----'  \n")), 0],
  ["with a comment in it", (script) => rewritten(script, (list) => list.replace("(\n", "(\n    # the first key, since 2026\n")), 0],
  ["with a comment that has an apostrophe in it", (script) => rewritten(script, (list) => list.replace("(\n", "(\n    # Surogate's release key since 2026\n")), 0],
  ["with a comment that has a parenthesis in it", (script) => rewritten(script, (list) => list.replace("(\n", "(\n    # the first key (2026)\n")), 0],
  ["with a comment between two keys", (script) => rewritten(script, (list) => list.replace("-----END PUBLIC KEY-----'\n", "-----END PUBLIC KEY-----'\n    # the next\n")), 0],
  ["with its keys in double quotes", (script) => rewritten(script, (list) => list.replaceAll("'", '"')), 0],
  ["on one line", (script) => rewritten(script, (list) => `${list.trim().replace("(\n", "( ").replace(/\n *\)$/, " )")}\n`), 0],
  ["with its closing bracket behind the last key", (script) => rewritten(script, (list) => list.replace(/'\n *\)\n$/, "' )\n")), 0],
  ["with a key's lines indented", (script) => rewritten(script, (list) => list.replace(/\n(?=[A-Za-z0-9+/=]+\n|-----END)/g, "\n    ")), 0],
  // One line of a key alone, where the spelling above has them all: OpenSSL reads a key whose
  // lines are indented, so a reader that let one by would hand it a key in another spelling.
  ["with one line of a key's letters indented", (script) => rewritten(script, (list) => list.replace(/\n(?=[A-Za-z0-9+/=]+\n)/, "\n    ")), 0],
  ["with a key's last line indented", (script) => rewritten(script, (list) => list.replace(/\n(?=-----END)/, "\n    ")), 0],
  ["with a key that has no line between its first and its last", (script) => rewritten(script, (list) => list.replace(/\n[A-Za-z0-9+/=]+\n/, "\n")), 0],
  // A line of the list with more on it than the form has, before or behind: bash would read a
  // word joined to a key, a list closed early, or a comment.
  ["with a word before a key's first quote", (script) => rewritten(script, (list) => list.replace("'-----BEGIN", "x'-----BEGIN")), 0],
  ["with a word before its closing bracket", (script) => rewritten(script, (list) => list.replace(/\n( *)\)\n$/, "\n$1true)\n")), 0],
  ["with a comment behind its opening bracket", (script) => rewritten(script, (list) => list.replace("(\n", "( # the keys\n")), 0],
  ["with an empty line in it", (script) => rewritten(script, (list) => list.replace("(\n", "(\n\n")), 0],
  ["with no key in it", (script) => rewritten(script, (list) => `${list.split("\n")[0]}\n  )\n`), 0],
  ["with a second list added to it", (script) => rewritten(script, (list) => `${list}${list.replace("RELEASE_KEYS=(", "RELEASE_KEYS+=(")}`), 0],
  ["with the list given twice", (script) => rewritten(script, (list) => `${list}${list}`), 0],
  ["with a carriage return at each of its lines' ends", (script) => rewritten(script, (list) => list.replaceAll("\n", "\r\n")), 0],
  // The list in its form, and a key more that bash alone has: added where no line begins with the
  // list's name, which no reader of a list looks at. Both readers read the list's two; the release
  // job, which asks bash too, refuses a script that the two would not read as bash runs it.
  ["with a key added to it from the middle of another line", (script) => rewritten(script, (list) => `${list}  true && RELEASE_KEYS+=('-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEA9SZBZHM7o/wDBWPfbhPMxucA2139J9j+nFHJYNwPA1w=\n-----END PUBLIC KEY-----')\n`), 2],
  // bash drops such a byte from a script it runs and from what a command writes it, and would set
  // the key without it. Read letter for letter, the line is no line of a key.
  ["with a zero byte in a key's line", (script) => rewritten(script, (list) => list.replace(/\n([A-Za-z0-9+/=]{8})(?=[A-Za-z0-9+/=]+\n)/, "\n$1\0")), 0],
  ["with a zero byte behind its closing bracket", (script) => rewritten(script, (list) => list.replace(/\)\n$/, ")\0\n")), 0],
  // A key is Ed25519's, in the one line OpenSSL writes of one. OpenSSL reads more: the same key's
  // letters over two lines, or with bits in its last letter that are no byte's; and keys of other
  // kinds, whose signatures no install takes, as it takes 64 bytes of one and no more.
  ["with a key that is no key", (script) => rewritten(script, (list) => list.replace(/\n[A-Za-z0-9+/=]+\n/, "\nbm90IGEga2V5\n")), 0],
  ["with a key's letters on two lines", (script) => rewritten(script, (list) => list.replace(/\n([A-Za-z0-9+/=]{30})([A-Za-z0-9+/=]+)\n/, "\n$1\n$2\n")), 0],
  ["with a key whose last letter holds bits of no byte", (script) => rewritten(script, (list) => list.replace(/[A-Za-z0-9+/]=\n/, (last) => `${"BFJNRVZdhlptx159"["AEIMQUYcgkosw048".indexOf(last[0]!)]}=\n`)), 0],
  ["with a key of another kind", (script) => rewritten(script, (list) => list.replace(/\n[A-Za-z0-9+/=]+\n/, "\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEMVBDfASKJhXoSCBb3+OSw5Y0F8KE\nRgLG5k0I+l4L3V8F/GVHjRRK6EVyBPqRVisczBpTXU5figSz6i6CZA3E3w==\n")), 0],
  ["with a key a letter short", (script) => rewritten(script, (list) => list.replace(/\n([A-Za-z0-9+/]{4})([A-Za-z0-9+/=]+)\n/, "\n$2\n")), 0],
  // The list in its form, and its first key another to bash: set by its place, on a line that
  // does not begin as one that sets the list. The readers read the list's two, and the release
  // job, which asks bash key for key, refuses the script.
  ["with its first key replaced by its place on a later line", (script) => rewritten(script, (list) => `${list}  RELEASE_KEYS[0]='-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEA9SZBZHM7o/wDBWPfbhPMxucA2139J9j+nFHJYNwPA1w=\n-----END PUBLIC KEY-----'\n`), 2],
];
