//! `surogate-helper spawn <keep> <program> <args>…`: a program started holding the descriptors its caller gave the
//! helper, the first *keep* + 1, and nothing else of the caller's, as src/clean-child.ts asks on Windows. What is read
//! and written to start it is here, the same on every OS and tested on each; windows/spawn.rs starts it.
//!
//! Its caller, libuv, gives every child the stdio it asked for twice: as the standard handles, and as the C runtime's
//! table of descriptors in STARTUPINFOW's lpReserved2 (libuv's src/win/process-stdio.c, the MSVC runtime's layout),
//! where a fourth descriptor and on, as Node's IPC channel at 3, are found by the program's own runtime.

use std::ffi::{OsStr, OsString};

/// The most descriptors a program is given: libuv takes no more stdio than this (process-stdio.c, a count kept in a
/// byte's worth of the table's flags).
pub const MOST_KEPT: usize = 255;

/// A descriptor of the table that is open (the runtime's FOPEN).
const FOPEN: u8 = 0x01;

/// A run's arguments after `spawn`: the last descriptor the program keeps, the program by its whole path, and its own.
pub struct Spawn<'a> {
    pub keep: usize,
    pub program: &'a OsStr,
    pub args: &'a [OsString],
}

/// *args* as a spawn takes them, or None: *keep* in decimal, from 2 (the standard three) to MOST_KEPT; a program on
/// a drive by its whole path (X:\…), as the app names its own and the system's.
pub fn parsed(args: &[OsString]) -> Option<Spawn<'_>> {
    let (keep, rest) = args.split_first()?;
    let (program, args) = rest.split_first()?;
    let keep = keep.to_str().filter(|text| !text.is_empty() && text.len() <= 3 && text.bytes().all(|byte| byte.is_ascii_digit()))?;
    let keep: usize = keep.parse().ok().filter(|keep| (2..=MOST_KEPT).contains(keep))?;
    let path = program.to_str()?;
    let bytes = path.as_bytes();
    let drive = bytes.len() > 3 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':' && bytes[2] == b'\\';
    (drive && !path.contains('"') && !path.contains('\0')).then_some(Spawn { keep, program, args })
}

/// The command line of *program* and *args*, as CommandLineToArgvW and the MSVC runtime read each back as it is
/// (Microsoft, "Parsing C++ command-line arguments"): the program in quotes, which hold no quote, since argv[0] takes
/// no escape; each argument as it is where it holds no blank and no quote, and else in quotes, each backslash that
/// comes before a quote or before the closing one doubled, and each quote after a backslash.
pub fn command_line(program: &[u16], args: &[Vec<u16>]) -> Vec<u16> {
    const QUOTE: u16 = b'"' as u16;
    const BACKSLASH: u16 = b'\\' as u16;
    let mut line = vec![QUOTE];
    line.extend_from_slice(program);
    line.push(QUOTE);
    for arg in args {
        line.push(b' ' as u16);
        let plain = !arg.is_empty() && !arg.iter().any(|&unit| matches!(unit, 0x20 | 0x09 | 0x0a | 0x0b | QUOTE));
        if plain {
            line.extend_from_slice(arg);
            continue;
        }
        line.push(QUOTE);
        let mut backslashes = 0;
        for &unit in arg {
            if unit == BACKSLASH {
                backslashes += 1;
                continue;
            }
            let doubled = if unit == QUOTE { backslashes * 2 + 1 } else { backslashes };
            line.extend(std::iter::repeat_n(BACKSLASH, doubled));
            backslashes = 0;
            line.push(unit);
        }
        line.extend(std::iter::repeat_n(BACKSLASH, backslashes * 2));
        line.push(QUOTE);
    }
    line
}

/// What the program is given of the caller's table, *table* (lpReserved2's bytes): the table cut to its first
/// *keep* + 1 descriptors, and the handle of each of those that is open, as a pointer-sized number. None for a table
/// that is not one: shorter than its count says, or of a negative count.
pub fn inherited(table: &[u8], keep: usize) -> Option<(Vec<u8>, Vec<usize>)> {
    const WIDTH: usize = size_of::<usize>();
    let count = i32::from_le_bytes(table.get(..4)?.try_into().ok()?);
    let count = usize::try_from(count).ok()?;
    let flags = table.get(4..4 + count)?;
    let handles = table.get(4 + count..4 + count + count * WIDTH)?;
    let kept = count.min(keep + 1);
    let mut cut = Vec::with_capacity(4 + kept + kept * WIDTH);
    cut.extend_from_slice(&i32::try_from(kept).ok()?.to_le_bytes());
    cut.extend_from_slice(&flags[..kept]);
    cut.extend_from_slice(&handles[..kept * WIDTH]);
    let open = (0..kept)
        .filter(|&fd| flags[fd] & FOPEN != 0)
        .map(|fd| usize::from_le_bytes(handles[fd * WIDTH..(fd + 1) * WIDTH].try_into().expect("a handle's width")))
        // Neither none, nor INVALID_HANDLE_VALUE, nor the runtime's -2 for a descriptor with no console behind it.
        .filter(|&handle| handle != 0 && handle != usize::MAX && handle != usize::MAX - 1)
        .collect();
    Some((cut, open))
}

#[cfg(test)]
mod tests {
    use super::{command_line, inherited, parsed, MOST_KEPT};
    use std::ffi::OsString;

    fn wide(text: &str) -> Vec<u16> {
        text.encode_utf16().collect()
    }

    fn args(texts: &[&str]) -> Vec<OsString> {
        texts.iter().map(OsString::from).collect()
    }

    #[test]
    fn takes_the_descriptors_kept_and_a_program_on_a_drive_by_its_whole_path() {
        let given = args(&["3", "C:\\Program Files\\Surogate\\bin\\node.exe", "--disable-sigusr1", "C:\\a b\\host.js"]);
        let spawn = parsed(&given).expect("a spawn");
        assert_eq!((spawn.keep, spawn.program.to_str(), spawn.args.len()), (3, Some("C:\\Program Files\\Surogate\\bin\\node.exe"), 2));
        assert_eq!(parsed(&args(&["2", "d:\\x.exe"])).map(|spawn| spawn.args.len()), Some(0));
        assert_eq!(parsed(&args(&[&MOST_KEPT.to_string(), "C:\\x.exe"])).map(|spawn| spawn.keep), Some(MOST_KEPT));
    }

    #[test]
    fn refuses_a_count_it_cannot_keep_and_a_program_not_named_by_its_whole_path_on_a_drive() {
        for keep in ["", "1", "0", "-3", "+3", "03x", "256", "1000", " 3", "3 "] {
            assert!(parsed(&args(&[keep, "C:\\x.exe"])).is_none(), "{keep:?}");
        }
        for program in ["node.exe", "bin\\node.exe", "\\node.exe", "C:node.exe", "C:/node.exe", "\\\\server\\share\\x.exe", "\\\\?\\C:\\x.exe", "C:\\", "C:\\a\"b.exe"] {
            assert!(parsed(&args(&["2", program])).is_none(), "{program:?}");
        }
        assert!(parsed(&args(&["2"])).is_none());
        assert!(parsed(&args(&[])).is_none());
    }

    #[test]
    fn writes_a_command_line_each_argument_reads_back_from_as_it_is() {
        let program = wide("C:\\Program Files\\Surogate\\bin\\node.exe");
        let line = |given: &[&str]| String::from_utf16(&command_line(&program, &given.iter().map(|arg| wide(arg)).collect::<Vec<_>>())).unwrap();
        assert_eq!(line(&[]), "\"C:\\Program Files\\Surogate\\bin\\node.exe\"");
        assert_eq!(line(&["--disable-sigusr1", "C:\\x\\host.js"]), "\"C:\\Program Files\\Surogate\\bin\\node.exe\" --disable-sigusr1 C:\\x\\host.js");
        // Microsoft's table of how the runtime reads a line, each the other way round.
        assert_eq!(line(&["a b", ""]), "\"C:\\Program Files\\Surogate\\bin\\node.exe\" \"a b\" \"\"");
        assert_eq!(line(&["ab\"c"]), "\"C:\\Program Files\\Surogate\\bin\\node.exe\" \"ab\\\"c\"");
        assert_eq!(line(&["a\\\\b", "a\\\\\\\"b"]), "\"C:\\Program Files\\Surogate\\bin\\node.exe\" a\\\\b \"a\\\\\\\\\\\\\\\"b\"");
        // A backslash at the end of a quoted argument is doubled, so it closes nothing.
        assert_eq!(line(&["C:\\a b\\"]), "\"C:\\Program Files\\Surogate\\bin\\node.exe\" \"C:\\a b\\\\\"");
        assert_eq!(line(&["tab\there", "line\nend"]), "\"C:\\Program Files\\Surogate\\bin\\node.exe\" \"tab\there\" \"line\nend\"");
    }

    // A table as libuv writes one: its count, a flag byte each, then a handle each, unaligned.
    fn table(entries: &[(u8, usize)]) -> Vec<u8> {
        let mut bytes = i32::try_from(entries.len()).unwrap().to_le_bytes().to_vec();
        bytes.extend(entries.iter().map(|&(flags, _)| flags));
        for &(_, handle) in entries {
            bytes.extend_from_slice(&handle.to_le_bytes());
        }
        bytes
    }

    #[test]
    fn gives_the_program_the_descriptors_it_keeps_and_the_handle_of_each_open_one() {
        // stdin ignored (NUL), stdout and stderr the caller's, Node's channel at 3, and one more the program does not keep.
        let given = table(&[(0x41, 0x10), (0x09, 0x14), (0x09, 0x18), (0x09, 0x1c), (0x01, 0x20)]);
        let (cut, open) = inherited(&given, 3).expect("a table");
        assert_eq!(cut, table(&[(0x41, 0x10), (0x09, 0x14), (0x09, 0x18), (0x09, 0x1c)]));
        assert_eq!(open, vec![0x10, 0x14, 0x18, 0x1c]);
        // A table shorter than the descriptors kept is given as it is.
        assert_eq!(inherited(&given, 9).expect("a table").0, given);
    }

    #[test]
    fn leaves_out_a_descriptor_that_is_not_open_or_has_no_handle() {
        let given = table(&[(0x41, 0x10), (0x00, 0x14), (0x09, usize::MAX), (0x01, usize::MAX - 1), (0x09, 0)]);
        assert_eq!(inherited(&given, 4).expect("a table").1, vec![0x10]);
    }

    #[test]
    fn refuses_a_table_shorter_than_its_count_says() {
        let given = table(&[(0x41, 0x10), (0x09, 0x14)]);
        assert!(inherited(&given[..given.len() - 1], 2).is_none());
        assert!(inherited(&[], 2).is_none());
        assert!(inherited(&(-1i32).to_le_bytes(), 2).is_none());
        assert_eq!(inherited(&0i32.to_le_bytes(), 2), Some((0i32.to_le_bytes().to_vec(), vec![])));
    }
}
