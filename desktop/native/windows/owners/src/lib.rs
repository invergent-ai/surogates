//! Whether a file or folder of the install is an administrator's word: owned by SYSTEM or the Administrators, and no one
//! else let change it by an allow entry of its access list. The app asks the same of the install record, through ntat
//! (src/files/admins-own-win32.ts); the MSI's take-back asks it of a data folder it would keep, and surogate-admin.exe
//! of the folder it checks an update in and of the record it reads the installed version from.

/// SYSTEM, which the MSI's server and its custom actions run as.
pub const SYSTEM: &str = "S-1-5-18";
/// The Administrators group.
pub const ADMINISTRATORS: &str = "S-1-5-32-544";
/// TrustedInstaller, Windows's own servicing.
pub const TRUSTED_INSTALLER: &str = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464";
/// OWNER RIGHTS: an entry for whoever owns it, which the owner check makes SYSTEM or the Administrators.
pub const OWNER_RIGHTS: &str = "S-1-3-4";

/// Who owns a file or folder, and the trustee of each allow entry of its access list that lets it be changed, each a
/// SID's string; "?" for a trustee not read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Owners {
    pub owner: String,
    pub writers: Vec<String>,
}

impl Owners {
    /// Why *what* is not an administrator's word, in words that name whom it found; None where it is one.
    pub fn refusal(&self, what: &str) -> Option<String> {
        if self.owner != SYSTEM && self.owner != ADMINISTRATORS {
            return Some(format!("{what} is not the installer's: only an administrator may own it, and {} does", self.owner));
        }
        let mut others: Vec<&str> = Vec::new();
        for writer in self.writers.iter().map(String::as_str) {
            if ![SYSTEM, ADMINISTRATORS, TRUSTED_INSTALLER, OWNER_RIGHTS].contains(&writer) && !others.contains(&writer) {
                others.push(writer);
            }
        }
        if others.is_empty() {
            None
        } else {
            Some(format!("{what} is not the installer's: only administrators may change it, and {} may too", others.join(", ")))
        }
    }
}

#[cfg(windows)]
mod windows;
#[cfg(windows)]
pub use windows::of;

#[cfg(test)]
mod tests {
    use super::*;

    fn owners(owner: &str, writers: &[&str]) -> Owners {
        Owners { owner: owner.to_string(), writers: writers.iter().map(|writer| writer.to_string()).collect() }
    }

    #[test]
    fn takes_what_system_or_the_administrators_own_and_only_they_and_windows_s_own_change() {
        assert_eq!(owners(SYSTEM, &[SYSTEM, ADMINISTRATORS]).refusal("x"), None);
        assert_eq!(owners(ADMINISTRATORS, &[SYSTEM, ADMINISTRATORS, TRUSTED_INSTALLER, OWNER_RIGHTS]).refusal("x"), None);
        assert_eq!(owners(SYSTEM, &[]).refusal("x"), None);
    }

    #[test]
    fn refuses_another_owner_and_names_it() {
        let user = "S-1-5-21-1-2-3-1001";
        assert_eq!(owners(user, &[SYSTEM]).refusal("C:\\x"), Some(format!("C:\\x is not the installer's: only an administrator may own it, and {user} does")));
        assert!(owners(TRUSTED_INSTALLER, &[]).refusal("x").is_some(), "TrustedInstaller may change it, and owns none of the install's");
    }

    #[test]
    fn refuses_another_writer_and_names_each_once() {
        let users = "S-1-5-32-545";
        assert_eq!(
            owners(SYSTEM, &[SYSTEM, users, users, "?"]).refusal("C:\\x"),
            Some(format!("C:\\x is not the installer's: only administrators may change it, and {users}, ? may too"))
        );
        assert!(owners(SYSTEM, &["S-1-1-0"]).refusal("x").is_some(), "Everyone, as no access list at all gives it");
    }
}
