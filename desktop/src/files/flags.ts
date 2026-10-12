// Every open flag the file host spells by its number, for each OS, and nowhere else: Node's fs.constants names each
// OS's own common flags, and not these. A flag's number on one OS can be another flag on the next: Linux's O_PATH is
// Darwin's O_SYMLINK, which opens a link itself where Linux's O_PATH with O_NOFOLLOW refuses one.

// Linux's: a descriptor that holds a folder without reading it, so one this user may pass through but not list is
// held all the same; and close-on-exec, as the open flags in /proc/self/fdinfo show it.
export const LINUX_FLAGS = { O_PATH: 0o10000000, O_CLOEXEC: 0o2000000 } as const;
