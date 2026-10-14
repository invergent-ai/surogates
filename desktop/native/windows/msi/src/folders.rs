//! The MSI's folder of `%ProgramData%`, `Surogate`, which holds the install record (`install.json`) that every user's app
//! reads, and its `apply`, which only administrators and SYSTEM reach, for the copies an update is checked from: each
//! SYSTEM's own, with an access list of its own and none inherited (Surogate.wxs, PermissionEx). `%ProgramData%` lets
//! its users make folders, and gives whoever makes one the folder as its owner, who can give themselves any right to it
//! again whatever its access rules say. So one that was there before the install, made by a user before it, after an
//! uninstall or in the gap a major upgrade opens, is removed whole by the take-back (take_back.rs), a link in its place
//! as a link and never followed, and the install makes its own: one Windows Installer made, which it removes again at
//! the uninstall, as it removes no folder that was there before it.

/// The privileges the take-back enables in its token, each named as Windows names it: to set SYSTEM as an owner, and to
/// open by a backup-semantics handle what its maker's access rules give SYSTEM no right to.
pub const PRIVILEGES: [&str; 3] = ["SeTakeOwnershipPrivilege", "SeRestorePrivilege", "SeBackupPrivilege"];

const SE_PRIVILEGE_ENABLED: u32 = 0x2;

/// Whether the privilege of LUID *low*, *high* is enabled, by the *token*'s own list as `GetTokenInformation` writes it
/// (`TOKEN_PRIVILEGES`: its count, then each privilege's LUID parts and attributes), or None where the token lacks it.
pub fn enabled(token: &[u32], low: u32, high: i32) -> Option<bool> {
    let count = token.first().copied().unwrap_or(0) as usize;
    let (entries, _) = token.get(1..).unwrap_or(&[]).as_chunks::<3>();
    entries.iter().take(count).find(|[l, h, _]| *l == low && *h as i32 == high).map(|[_, _, attributes]| attributes & SE_PRIVILEGE_ENABLED != 0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_whether_each_privilege_is_enabled_from_the_token_s_own_list() {
        // TOKEN_PRIVILEGES as GetTokenInformation writes it: a count, then each LUID's low and high parts and attributes.
        let token = [3, 9, 0, 0x3, 17, 0, 0x0, 18, 0, 0x2];
        assert_eq!(enabled(&token, 9, 0), Some(true));
        assert_eq!(enabled(&token, 17, 0), Some(false));
        assert_eq!(enabled(&token, 18, 0), Some(true));
        assert_eq!(enabled(&token, 20, 0), None);
        assert_eq!(enabled(&token, 9, 1), None);
        assert_eq!(enabled(&[5, 9, 0, 2], 9, 0), Some(true), "a count past the list reads what is there");
        assert_eq!(enabled(&[], 9, 0), None);
        assert_eq!(PRIVILEGES, ["SeTakeOwnershipPrivilege", "SeRestorePrivilege", "SeBackupPrivilege"]);
    }
}
