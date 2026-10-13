// Windows's own rules on what a name is: the names Windows's programs cannot open, or open as something else. On
// Windows the file tools refuse each of them, for reads and writes alike, and never fold one to the name it may stand
// for: the walk checks each component before it opens it (native/ntat/ntat.c, name_taken, which a test holds to this),
// and the long name of what it opened after.
//  - A stream: a ':' anywhere. pre-commit::$DATA is the main stream of pre-commit, and Dir::$INDEX_ALLOCATION is Dir
//    (Microsoft Learn, "File Streams"); git refuses every .git: stream for the same reason (git's path.c, is_ntfs_dotgit).
//  - The characters Windows forbids: \ / < > " | ? * and 1 to 31 (Microsoft Learn, "Naming Files, Paths, and Namespaces").
//  - A trailing dot or space: Win32 strips them from a path's last element, so .git. and .git  name .git to every
//    Win32 program, while NT's opens keep them as other names (Project Zero, "The Definitive Guide on Win32 to NT Path
//    Conversion").
//  - An 8.3 short name's form, a '~' and a digit: NTFS makes short names on the system volume by default, and GIT~1 can
//    be .git (git's path.c).
//  - A device's name: CON, PRN, AUX, NUL, CONIN$, CONOUT$, COM0 to 9 and LPT0 to 9, and COM and LPT with a superscript
//    one, two or three, in any case, alone or with any extension, with spaces before it. Windows 11 changed which of
//    these Win32 maps to a device, and sources disagree on how (CPython issue 95486, Go's path_windows.go, Microsoft's
//    naming page): refusing all of them on every build is the one rule that needs no answer.

/** The file tools' words for a name Windows does not take as itself. */
export const nameRefusal = (name: string): string => `This name cannot be used on Windows: ${name}`;

const FORBIDDEN = /[\\/:*?"<>|\u0001-\u001f\u0000]/;
const SHORT = /~[0-9]/;
const DEVICE = /^(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[0-9¹²³])$/i;

/** Whether *name*, one component, is a name Windows opens as itself. */
export function windowsTakes(name: string): boolean {
  if (name === "" || name === "." || name === ".." || name.length > 255) return false;
  if (FORBIDDEN.test(name) || SHORT.test(name) || name.endsWith(".") || name.endsWith(" ")) return false;
  const stem = name.split(".", 1)[0]!.replace(/ +$/, "");
  return !DEVICE.test(stem);
}

/** The first component of *relative*, a path under a folder, that Windows does not take as itself; null where each is taken. */
export function refusedName(relative: string): string | null {
  if (relative === "") return null;
  return relative.split("/").find((name) => !windowsTakes(name)) ?? null;
}
