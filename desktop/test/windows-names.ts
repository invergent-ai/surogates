// Names for the tests of Windows's names: the file tools' rule (names-win32.test.ts) and the walk's own (ntat-win32.test.ts).

/** Names Windows takes as themselves, each beside a near miss below. */
export const TAKEN = [
  "a", "pre-commit", ".git", "hooks", "notes.txt", "CONSOLE", "con-notes", "nul2", "COM10", "LPT", "lpt10.txt", "COMX", "~", "a~b", "x~", "~$doc.docx",
  "naïve", "日本語", "é", ".gitignore", "a b", " leading", "..a", "a..b", "x".repeat(255), "AUXILIARY.md", "conin", "CONOUT", "com0x",
];

/** Names Windows aliases or refuses, for every reason it has. */
export const REFUSED = [
  // Streams.
  "pre-commit::$DATA", "Dir::$INDEX_ALLOCATION", ".git:x", "a:b",
  // Forbidden characters.
  "a\\b", "a/b", "a*b", "a?b", 'a"b', "a<b", "a>b", "a|b", "a\u0001b", "tab\there", "\u001f",
  // A trailing dot or space.
  ".git.", ".git ", "desktop.ini.", "desktop.ini ", "x.lnk.", "a..", "a. .",
  // Short names' form.
  "GIT~1", "git~1", "DOCUME~1.LIB", "notes~9.txt", "PROGRA~2", "~0",
  // Devices, alone, with any extension, with spaces before it, in any case.
  "CON", "con", "Con", "PRN", "AUX", "NUL", "nul.txt", "NUL.tar.gz", "CON .txt", "COM0", "COM1", "com9.log", "LPT0", "LPT9", "COM¹",
  "LPT²", "lpt³.txt", "CONIN$", "conout$", "CONIN$.x", "AUX .",
  // Not a name.
  "", ".", "..", "x".repeat(256),
];
