// The one form of path a line may name: a drive's absolute path, by backslashes, of plain names. What the form refuses
// is what Windows would read as another file than the one named, or as no file: another namespace (\\?\, \\.\), a
// server's share (\\server\share), a drive-relative or a relative path, "." and "..", a stream (name:stream), and a
// name Win32 would change by stripping its trailing dots and spaces (Microsoft Learn, "Naming Files, Paths, and
// Namespaces"). Whether the file is there, whose it is, and what lies on its way, the service looks at on the disk
// itself, as the caller (the service's files.rs); the form is checked first, so nothing else is opened at all.

/// The longest path a line may name, in bytes.
pub const MAX_PATH: usize = 4096;

/// *path*, if it is of the one form; else why not.
pub fn checked(path: &str) -> Result<&str, &'static str> {
    if path.len() > MAX_PATH {
        return Err("a path over 4096 bytes");
    }
    let bytes = path.as_bytes();
    if bytes.len() < 3 || !bytes[0].is_ascii_alphabetic() || bytes[1] != b':' || bytes[2] != b'\\' {
        return Err("a path that is not a drive's absolute path, as C:\\…");
    }
    if path.contains('/') {
        return Err("a path with a forward slash");
    }
    let rest = &path[3..];
    if rest.is_empty() {
        return Ok(path);
    }
    for name in rest.split('\\') {
        if name.is_empty() {
            return Err("a path with an empty name in it");
        }
        if name == "." || name == ".." {
            return Err("a path through . or ..");
        }
        if name.ends_with('.') || name.ends_with(' ') {
            return Err("a path with a name that ends in a dot or a space");
        }
        if name.chars().any(|c| (c as u32) < 32 || matches!(c, ':' | '*' | '?' | '"' | '<' | '>' | '|')) {
            return Err("a path with a character Windows does not take in a name, or a stream");
        }
    }
    Ok(path)
}

/// Whether *path* is *folder* or in it, comparing ASCII letters without their case, as NTFS does, and every other
/// character exactly: NTFS's upper-casing takes no other character to an ASCII letter, so a name that differs
/// elsewhere is another name, and is never taken for the folder's.
pub fn within(path: &str, folder: &str) -> bool {
    let folder = folder.trim_end_matches('\\');
    path.len() >= folder.len()
        && path.is_char_boundary(folder.len())
        && path[..folder.len()].eq_ignore_ascii_case(folder)
        && (path.len() == folder.len() || path.as_bytes()[folder.len()] == b'\\')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn takes_a_drive_s_absolute_path_of_plain_names() {
        for path in [r"C:\", r"C:\Users\ana\AppData\Local\Surogate\vm\image\rootfs.vhd", r"d:\Ana's files\x.y", r"C:\Users\Ana\Documents\shop (2)\ſ"] {
            assert_eq!(checked(path), Ok(path));
        }
    }

    #[test]
    fn refuses_every_other_form() {
        for path in [
            "", "C:", r"C:x", r"\Users\ana", r"\\?\C:\Users", r"\\.\PhysicalDrive0", r"\\server\share\x", r"\\?\UNC\server\share", "C:/Users/ana",
            r"C:\Users\..\Windows", r"C:\Users\.\ana", r"C:\Users\\ana", r"C:\Users\ana\", r"C:\Users\ana.", r"C:\Users\ana ", r"C:\x\pre-commit::$DATA",
            r"C:\x\a:b", "C:\\x\\a\u{1}b", r"C:\x\a*", r"C:\x\a?", r"C:\x\a|b", "C:\\x\\a\"b", r"C:\x\<a>",
        ] {
            assert!(checked(path).is_err(), "{path}");
        }
        assert_eq!(checked(&format!(r"C:\{}", "x".repeat(4094))), Err("a path over 4096 bytes"));
    }

    #[test]
    fn finds_a_path_in_its_folder_by_ascii_case_alone() {
        let data = r"C:\Users\ana\AppData\Local\Surogate";
        assert!(within(r"C:\Users\ana\AppData\Local\Surogate\vm\image\vmlinuz", data));
        assert!(within(r"c:\USERS\ANA\appdata\local\surogate\vm", data));
        assert!(within(data, data));
        assert!(within(r"C:\Users\ana\x", r"C:\Users\ana\"));
        assert!(!within(r"C:\Users\ana\AppData\Local\SurogateX\vm", data));
        assert!(!within(r"C:\Users\ana\AppData\Local", data));
        // ſ upper-cases to S elsewhere, but not in NTFS: another folder.
        assert!(!within(r"C:\Uſers\ana\AppData\Local\Surogate\vm", data));
        assert!(!within(r"C:\Users\anä\AppData\Local\Surogate\vm", r"C:\Users\anÄ\AppData\Local\Surogate"));
    }
}
