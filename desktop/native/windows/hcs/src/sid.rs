// A user's security identifier in its string form, and the descriptors that name it.

/// Whether *text* is a SID's string form, as ConvertSidToStringSidW writes one: "S-1-", the identifier authority, and
/// one to fifteen subauthorities, each a decimal number (Microsoft Learn, "SID Components"). Nothing else, so a SID
/// put into a descriptor's string adds no other entry to it.
pub fn is_sid(text: &str) -> bool {
    let Some(rest) = text.strip_prefix("S-1-") else { return false };
    let parts: Vec<&str> = rest.split('-').collect();
    (2..=16).contains(&parts.len())
        && parts.iter().all(|part| !part.is_empty() && part.len() <= 10 && part.bytes().all(|byte| byte.is_ascii_digit()) && part.parse::<u32>().is_ok())
}

/// The descriptor a VM's Hyper-V sockets are bound and connected under: LocalSystem and the user *sid* alone, with
/// full access, as WSL's service gives its VM (WSL, src/windows/service/exe/WslCoreVm.cpp, GenerateConfigJson:
/// "D:P(A;;FA;;;SY)(A;;FA;;;<user SID>)"). None if *sid* is not one.
pub fn socket_descriptor(sid: &str) -> Option<String> {
    is_sid(sid).then(|| format!("D:P(A;;FA;;;SY)(A;;FA;;;{sid})"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn takes_a_user_s_sid_and_nothing_that_would_add_to_a_descriptor() {
        assert!(is_sid("S-1-5-21-3623811015-3361044348-30300820-1013"));
        // An Entra ID user's.
        assert!(is_sid("S-1-12-1-1234567890-1234567890-1234567890-1234567890"));
        for not in [
            "", "S-1-5", "S-1-5-", "s-1-5-21-1-2-3-1000", "S-1-5-21-1-2-3-1000)(A;;FA;;;WD", "S-1-5-21-1-2-3-1000 ", "S-1-5-21-1-2-3-99999999999",
            "S-2-5-21-1", "S-1-5-21-1-2-3-4-5-6-7-8-9-10-11-12-13-14-15-16",
        ] {
            assert!(!is_sid(not), "{not}");
        }
    }

    #[test]
    fn names_the_user_and_the_system_alone() {
        assert_eq!(socket_descriptor("S-1-5-21-1-2-3-1001").as_deref(), Some("D:P(A;;FA;;;SY)(A;;FA;;;S-1-5-21-1-2-3-1001)"));
        assert_eq!(socket_descriptor("S-1-5-21-1-2-3-1001)(A;;FA;;;WD)"), None);
    }
}
