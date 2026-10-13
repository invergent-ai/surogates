//! What the helper answers, as the app reads it: a process a line, a program's signature, and a program's name as the
//! App Paths key names one. Windows's own calls fill them (windows/); these are the same on every OS, and tested on
//! each.

use crate::json::string;

/// A process of this user's: its pid, its program's path, and its arguments, the program's own name first, as
/// Linux's /proc and macOS's helper give them.
pub struct Process {
    pub pid: u32,
    pub path: String,
    pub args: Vec<String>,
}

impl Process {
    /// Its JSON line, without the line's end: {"pid":…,"path":…,"args":[…]}.
    pub fn json(&self) -> String {
        let args: Vec<String> = self.args.iter().map(|arg| string(arg)).collect();
        format!("{{\"pid\":{},\"path\":{},\"args\":[{}]}}", self.pid, string(&self.path), args.join(","))
    }
}

/// What WinVerifyTrust said of a program: whether its Authenticode signature verifies, with whom it names as its
/// signer; its HRESULT; and whether the program is on a network volume.
pub struct Verdict {
    pub trusted: bool,
    pub signer: Option<String>,
    pub status: i32,
    pub remote: bool,
}

impl Verdict {
    /// Its JSON object, without the line's end: {"trusted":…,"signer":…,"status":"0x…","remote":…}.
    pub fn json(&self) -> String {
        let signer = self.signer.as_deref().map_or_else(|| "null".to_string(), string);
        format!("{{\"trusted\":{},\"signer\":{},\"status\":\"0x{:08x}\",\"remote\":{}}}", self.trusted, signer, self.status, self.remote)
    }
}

/// Whether *name* is a program's file name as the App Paths key names one: a name ending in .exe, of letters,
/// digits, dots, hyphens and underscores, never a path.
pub fn program_name(name: &str) -> bool {
    let stem = name.len().checked_sub(4).and_then(|end| name.get(..end));
    name.len() <= 64
        && name.to_ascii_lowercase().ends_with(".exe")
        && stem.is_some_and(|stem| !stem.is_empty() && !stem.starts_with('.') && stem.bytes().all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b)))
}

/// A program's path as an App Paths key's value gives it: some installers write it quoted.
pub fn unquoted(value: &str) -> &str {
    let value = value.trim();
    value.strip_prefix('"').and_then(|rest| rest.strip_suffix('"')).unwrap_or(value)
}

/// What a root's own properties let it be trusted for: every purpose where it has no extended-key-usage property, else
/// the purposes the property names, none where it names none (Microsoft, CertGetEnhancedKeyUsage: "If
/// GetLastError returns CRYPT_E_NOT_FOUND, the certificate is good for all uses. If it returns zero, the certificate
/// has no valid uses").
pub enum Usages {
    Unrestricted,
    Only(Vec<String>),
}

/// Server authentication's object identifier (RFC 5280, 4.2.1.12, id-kp-serverAuth).
pub const SERVER_AUTH: &str = "1.3.6.1.5.5.7.3.1";

/// Whether a root of *usages* may be trusted for TLS: unrestricted, or server authentication among its purposes.
pub fn for_server_auth(usages: &Usages) -> bool {
    match usages {
        Usages::Unrestricted => true,
        Usages::Only(purposes) => purposes.iter().any(|purpose| purpose == SERVER_AUTH),
    }
}

/// A program's version, as its version resource's fixed part gives its product version in two words (Microsoft,
/// VS_FIXEDFILEINFO, dwProductVersionMS and dwProductVersionLS): "major.minor.build.patch".
pub fn version(most: u32, least: u32) -> String {
    format!("{}.{}.{}.{}", most >> 16, most & 0xffff, least >> 16, least & 0xffff)
}

#[cfg(test)]
mod tests {
    use super::{for_server_auth, program_name, unquoted, version, Process, Usages, Verdict, SERVER_AUTH};

    #[test]
    fn trusts_a_root_for_tls_only_where_its_purposes_allow_server_authentication() {
        assert!(for_server_auth(&Usages::Unrestricted));
        assert!(for_server_auth(&Usages::Only(vec!["1.3.6.1.5.5.7.3.2".into(), SERVER_AUTH.into()])));
        // Client authentication alone, or every purpose turned off.
        assert!(!for_server_auth(&Usages::Only(vec!["1.3.6.1.5.5.7.3.2".into()])));
        assert!(!for_server_auth(&Usages::Only(vec![])));
        assert!(!for_server_auth(&Usages::Only(vec!["1.3.6.1.5.5.7.3.10".into()])));
    }

    #[test]
    fn says_a_version_from_its_two_words() {
        assert_eq!(version(154 << 16, (8037 << 16) | 58), "154.0.8037.58");
        assert_eq!(version(0, 0), "0.0.0.0");
        assert_eq!(version(u32::MAX, u32::MAX), "65535.65535.65535.65535");
    }

    #[test]
    fn says_a_process_as_a_json_line() {
        let process = Process { pid: 4120, path: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe".into(), args: vec!["chrome.exe".into(), "--user-data-dir=C:\\Users\\Ana\\p \"x\"".into()] };
        assert_eq!(process.json(), r#"{"pid":4120,"path":"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe","args":["chrome.exe","--user-data-dir=C:\\Users\\Ana\\p \"x\""]}"#);
        assert_eq!(Process { pid: 1, path: String::new(), args: vec![] }.json(), r#"{"pid":1,"path":"","args":[]}"#);
    }

    #[test]
    fn says_a_verdict_as_a_json_object() {
        let signed = Verdict { trusted: true, signer: Some("Google LLC".into()), status: 0, remote: false };
        assert_eq!(signed.json(), r#"{"trusted":true,"signer":"Google LLC","status":"0x00000000","remote":false}"#);
        // TRUST_E_NOSIGNATURE, on a network volume.
        let unsigned = Verdict { trusted: false, signer: None, status: 0x800B0100_u32 as i32, remote: true };
        assert_eq!(unsigned.json(), r#"{"trusted":false,"signer":null,"status":"0x800b0100","remote":true}"#);
    }

    #[test]
    fn takes_a_program_s_file_name_and_never_a_path() {
        for name in ["chrome.exe", "msedge.exe", "brave.exe", "Vivaldi.EXE", "my_browser-2.exe"] {
            assert!(program_name(name), "{name}");
        }
        for name in ["", ".exe", "..exe", "chrome", "chrome.exe.lnk", "..\\chrome.exe", "a\\chrome.exe", "a/chrome.exe", "C:chrome.exe", "chr ome.exe", &format!("{}.exe", "a".repeat(61))] {
            assert!(!program_name(name), "{name}");
        }
    }

    #[test]
    fn takes_a_path_out_of_its_quotes() {
        assert_eq!(unquoted("\"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe\""), "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe");
        assert_eq!(unquoted(" C:\\a\\b.exe "), "C:\\a\\b.exe");
        assert_eq!(unquoted("\"C:\\a"), "\"C:\\a");
    }
}
