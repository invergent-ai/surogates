//! The MSI's folders of `%ProgramData%`: `Surogate`, which holds the install record (`install.json`) that every user's
//! app reads, and its `apply`, which only administrators and SYSTEM reach, for the copies an update is checked from.
//! `%ProgramData%` lets its users make folders, and gives whoever makes one the folder as its owner, who can give
//! themselves any right to it again whatever its access rules say. So each folder is SYSTEM's own, with an access list
//! of its own and none inherited, and one that was there before the install, made by a user before it, after an
//! uninstall or in the gap a major upgrade opens, is taken back: SYSTEM made its owner, its access list replaced, and
//! what it held removed, but the record, which the install writes again whole. A link in either's place is removed, and
//! not followed. The install's own `PermissionEx` on each folder (Surogate.wxs) is the same descriptor.

/// `%ProgramData%\Surogate`: SYSTEM's own; SYSTEM and administrators every right, users read; nothing inherited.
pub const DATA_SDDL: &str = "O:SYG:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)";

/// `%ProgramData%\Surogate\apply`: SYSTEM's own; SYSTEM and administrators alone; nothing inherited.
pub const APPLY_SDDL: &str = "O:SYG:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)";

/// The record found there: SYSTEM's own, with no access rule of its own, but the rules `Surogate` gives a file, each
/// inherited, written out: set on the record alone, they are read from no folder above it.
pub const RECORD_SDDL: &str = "O:SYG:SYD:AI(A;ID;FA;;;SY)(A;ID;FA;;;BA)(A;ID;0x1200a9;;;BU)";

/// The record's name in `Surogate`, and the folder for updates.
pub const RECORD: &str = "install.json";
pub const APPLY: &str = "apply";

/// What the install does with an entry it finds in a folder of its own that was there before it.
#[derive(Debug, PartialEq, Eq)]
pub enum Found {
    /// `apply`, a folder of `Surogate`: taken back, and emptied.
    Apply,
    /// The record, a file of `Surogate`: taken back, and written again by the install.
    Record,
    /// Anything else, or anything of `apply`: removed, a link as a link.
    Remove,
}

/// What the install does with the entry *name* of `Surogate` (*in_apply* false) or of `apply`, a *folder* or not, and a
/// *link* (a reparse point) or not. Windows compares names without ASCII case.
pub fn found(name: &str, in_apply: bool, folder: bool, link: bool) -> Found {
    if in_apply || link {
        Found::Remove
    } else if folder && name.eq_ignore_ascii_case(APPLY) {
        Found::Apply
    } else if !folder && name.eq_ignore_ascii_case(RECORD) {
        Found::Record
    } else {
        Found::Remove
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keeps_of_surogate_the_apply_folder_and_the_record_alone_and_nothing_of_apply() {
        assert_eq!(found("apply", false, true, false), Found::Apply);
        assert_eq!(found("APPLY", false, true, false), Found::Apply);
        assert_eq!(found("install.json", false, false, false), Found::Record);
        assert_eq!(found("Install.JSON", false, false, false), Found::Record);
        for (name, folder) in [("planted.txt", false), ("wintrust.dll", false), ("apply", false), ("install.json", true), ("other", true)] {
            assert_eq!(found(name, false, folder, false), Found::Remove, "{name}");
        }
        for (name, folder) in [("0.1.0", true), ("surogate-admin.exe", false), ("install.json", false)] {
            assert_eq!(found(name, true, folder, false), Found::Remove, "{name}");
        }
    }

    #[test]
    fn removes_a_link_in_place_of_either_and_never_follows_it() {
        assert_eq!(found("apply", false, true, true), Found::Remove);
        assert_eq!(found("install.json", false, false, true), Found::Remove);
    }

    #[test]
    fn makes_each_folder_system_s_own_with_its_access_list_and_none_inherited() {
        for sddl in [DATA_SDDL, APPLY_SDDL, RECORD_SDDL] {
            assert!(sddl.starts_with("O:SYG:SYD:"), "{sddl}");
        }
        assert!(DATA_SDDL.starts_with("O:SYG:SYD:PAI(") && APPLY_SDDL.starts_with("O:SYG:SYD:PAI("));
        assert!(!APPLY_SDDL.contains(";;;BU)"));
    }

    #[test]
    fn gives_the_record_exactly_the_rules_its_folder_gives_a_file_each_inherited_and_none_its_own() {
        let (_, aces) = DATA_SDDL.split_once("D:PAI").unwrap();
        assert_eq!(RECORD_SDDL, format!("O:SYG:SYD:AI{}", aces.replace(";OICI;", ";ID;")));
        assert!(!RECORD_SDDL.contains("D:P") && !RECORD_SDDL.contains(";OICI;"));
    }
}
