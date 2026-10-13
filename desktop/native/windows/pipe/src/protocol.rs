// The service's lines. A caller asks for one VM on its connection, then its start, its shares and its end; the
// service answers each by its id, and says unasked what the guest's console printed and how the VM stopped:
//
//   → {"type":"boot","id":1,"data":"C:\\Users\\ana\\AppData\\Local\\Surogate","kernel":…,"initrd":…,"rootfs":…,
//      "agentDisk":…,"sessions":…,"cpus":4,"commandLine":"…","helperPid":4242}
//   ← {"type":"created","id":1,"runtimeId":"<guid>"}      the VM made, not started: its sockets may now be bound
//   → {"type":"start","id":2}                              ← {"type":"started","id":2}
//   → {"type":"share","id":3,"name":"r1","path":"C:\\…","readOnly":false}   ← {"type":"shared","id":3}
//   → {"type":"unshare","id":4,"name":"r1"}                ← {"type":"unshared","id":4}
//   → {"type":"kill","id":5}                               ← {"type":"stopped","reason":"killed"}
//   ← {"type":"refused","id":3,"why":"…","code":-2143878910}   any request, with HCS's HRESULT where it had one
//   ← {"type":"console","text":"…"}                         ← {"type":"stopped","reason":"guest"|"error","why":"…"}
//
// A request is taken only in its one form: its type one of these, every field of it there and of its type, none else,
// each within its bound, and every path of path.rs's form. Anything else is refused before the service acts on it.

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::path::{checked, MAX_PATH};

/// The most processors a request may give the guest (hcs::document, MAX_CPUS).
pub const MAX_CPUS: u32 = 64;
/// The longest kernel command line a request may give.
pub const MAX_COMMAND_LINE: usize = 4096;
/// The most shares one VM holds at once (guest/protocol.ts, MAX_SHARES).
pub const MAX_SHARES: usize = 8;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "camelCase", deny_unknown_fields)]
pub enum Request {
    /// The VM made, not started: the caller's data root, the files it boots, its processors and kernel line, and the
    /// caller's own surogate-vm.exe, whose end is the VM's.
    #[serde(rename_all = "camelCase")]
    Boot { id: u32, data: String, kernel: String, initrd: String, rootfs: String, agent_disk: String, sessions: String, cpus: u32, command_line: String, helper_pid: u32 },
    Start { id: u32 },
    /// The folder *path* shared into the guest as *name*, r<n>, read-only for a place's folder.
    #[serde(rename_all = "camelCase")]
    Share { id: u32, name: String, path: String, read_only: bool },
    Unshare { id: u32, name: String },
    Kill { id: u32 },
}

/// How a VM stopped: its guest powered it off, the caller's kill, or an error of the VM's.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum Reason {
    Guest,
    Killed,
    Error,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "camelCase", deny_unknown_fields)]
pub enum Answer {
    #[serde(rename_all = "camelCase")]
    Created { id: u32, runtime_id: String },
    Started { id: u32 },
    Shared { id: u32 },
    Unshared { id: u32 },
    Refused {
        id: Option<u32>,
        why: String,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        code: Option<i32>,
    },
    Console { text: String },
    Stopped {
        reason: Reason,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        why: Option<String>,
    },
}

impl Answer {
    pub fn refused(id: Option<u32>, why: impl Into<String>) -> Answer {
        Answer::Refused { id, why: why.into(), code: None }
    }

    /// The answer as its line, with its newline.
    pub fn line(&self) -> String {
        let mut line = serde_json::to_string(self).expect("an answer is JSON");
        line.push('\n');
        line
    }
}

impl Request {
    /// The request's id.
    pub fn id(&self) -> u32 {
        match self {
            Request::Boot { id, .. } | Request::Start { id } | Request::Share { id, .. } | Request::Unshare { id, .. } | Request::Kill { id } => *id,
        }
    }

    /// The request as its line, with its newline.
    pub fn line(&self) -> String {
        let mut line = serde_json::to_string(self).expect("a request is JSON");
        line.push('\n');
        line
    }

    /// *line* as a request in its one form, or the refusal to answer it with: by its id where it had one.
    pub fn parse(line: &str) -> Result<Request, Answer> {
        let id = serde_json::from_str::<Value>(line).ok().and_then(|value| value.get("id").and_then(Value::as_u64)).and_then(|id| u32::try_from(id).ok());
        let refuse = |why: &str| Answer::refused(id, why);
        let request: Request = serde_json::from_str(line).map_err(|_| refuse("a line that is not a request of the service's, each of its fields of its type and none else"))?;
        match &request {
            Request::Boot { data, kernel, initrd, rootfs, agent_disk, sessions, cpus, command_line, helper_pid, .. } => {
                for path in [data, kernel, initrd, rootfs, agent_disk, sessions] {
                    checked(path).map_err(refuse)?;
                }
                if *cpus == 0 || *cpus > MAX_CPUS {
                    return Err(refuse("a guest takes 1 to 64 processors"));
                }
                if command_line.is_empty() || command_line.len() > MAX_COMMAND_LINE || !command_line.bytes().all(|byte| (0x20..0x7f).contains(&byte)) {
                    return Err(refuse("a kernel command line that is not printable ASCII of at most 4096 bytes"));
                }
                if *helper_pid == 0 {
                    return Err(refuse("no helper's process"));
                }
            }
            Request::Share { name, path, .. } => {
                share_name(name).map_err(refuse)?;
                checked(path).map_err(refuse)?;
            }
            Request::Unshare { name, .. } => {
                share_name(name).map_err(refuse)?;
            }
            Request::Start { .. } | Request::Kill { .. } => {}
        }
        Ok(request)
    }
}

/// Whether *name* is a share's: r<n>, n from 1, at most nine digits, as Linux's tags are (guest/root.ts, TAG).
pub fn share_name(name: &str) -> Result<u32, &'static str> {
    let digits = name.strip_prefix('r').ok_or("a share's name that is not r<n>")?;
    if digits.is_empty() || digits.len() > 9 || digits.starts_with('0') || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err("a share's name that is not r<n>");
    }
    digits.parse().map_err(|_| "a share's name that is not r<n>")
}

const _: () = assert!(MAX_PATH < crate::lines::MAX_LINE / 6);

#[cfg(test)]
mod tests {
    use super::*;

    const BOOT: &str = r#"{"type":"boot","id":1,"data":"C:\\Users\\ana\\AppData\\Local\\Surogate","kernel":"C:\\Users\\ana\\AppData\\Local\\Surogate\\vm\\image\\vmlinuz","initrd":"C:\\Users\\ana\\AppData\\Local\\Surogate\\vm\\image\\initrd","rootfs":"C:\\Users\\ana\\AppData\\Local\\Surogate\\vm\\image\\rootfs.vhd","agentDisk":"C:\\Program Files\\Surogate\\resources\\vm\\agent.vhd","sessions":"C:\\Users\\ana\\AppData\\Local\\Surogate\\vm\\sessions.vhdx","cpus":4,"commandLine":"console=ttyS0 quiet","helperPid":4242}"#;

    #[test]
    fn takes_each_request_in_its_one_form() {
        let boot = Request::parse(BOOT).unwrap();
        assert!(matches!(&boot, Request::Boot { id: 1, cpus: 4, helper_pid: 4242, agent_disk, .. } if agent_disk == r"C:\Program Files\Surogate\resources\vm\agent.vhd"));
        assert_eq!(Request::parse(&boot.line()).unwrap(), boot);
        assert_eq!(Request::parse(r#"{"type":"start","id":2}"#), Ok(Request::Start { id: 2 }));
        assert_eq!(
            Request::parse(r#"{"type":"share","id":3,"name":"r1","path":"C:\\Users\\ana\\Documents\\shop","readOnly":true}"#),
            Ok(Request::Share { id: 3, name: "r1".into(), path: r"C:\Users\ana\Documents\shop".into(), read_only: true })
        );
        assert_eq!(Request::parse(r#"{"type":"unshare","id":4,"name":"r1"}"#), Ok(Request::Unshare { id: 4, name: "r1".into() }));
        assert_eq!(Request::parse(r#"{"type":"kill","id":5}"#), Ok(Request::Kill { id: 5 }));
    }

    #[test]
    fn refuses_an_unknown_type_an_unknown_or_missing_field_and_a_field_of_another_type() {
        for line in [
            r#"{"type":"exec","id":7,"command":"cmd"}"#,
            r#"{"type":"start","id":7,"options":"{}"}"#,
            r#"{"type":"share","id":7,"name":"r1","path":"C:\\x"}"#,
            r#"{"type":"share","id":7,"name":"r1","path":"C:\\x","readOnly":"yes"}"#,
            r#"{"type":"kill","id":7,"x":1}"#,
            r#"{"id":7}"#,
            "null",
            "[]",
        ] {
            let refused = Request::parse(line).unwrap_err();
            assert!(matches!(&refused, Answer::Refused { id, .. } if *id == if line.contains("\"id\":7") { Some(7) } else { None }), "{line}");
        }
        assert_eq!(Request::parse(&BOOT.replace(r#""cpus":4"#, r#""cpus":4,"networkAdapters":{}"#)).unwrap_err(), Answer::refused(Some(1), "a line that is not a request of the service's, each of its fields of its type and none else"));
    }

    #[test]
    fn refuses_a_boot_whose_path_count_line_or_helper_is_not_of_its_form() {
        for (from, to) in [
            (r#""kernel":"C:\\Users"#, r#""kernel":"\\\\server\\share"#),
            (r#""rootfs":"C:\\Users\\ana\\AppData"#, r#""rootfs":"C:\\Users\\ana\\..\\bob\\AppData"#),
            (r#""sessions":"C:\\Users"#, r#""sessions":"\\\\?\\C:\\Users"#),
            (r#""data":"C:\\Users\\ana\\AppData\\Local\\Surogate""#, r#""data":"C:\\Users\\ana\\AppData\\Local\\Surogate::$DATA""#),
            (r#""cpus":4"#, r#""cpus":0"#),
            (r#""cpus":4"#, r#""cpus":65"#),
            (r#""commandLine":"console=ttyS0 quiet""#, r#""commandLine":"""#),
            (r#""commandLine":"console=ttyS0 quiet""#, r#""commandLine":"console=ttyS0\nquiet""#),
            (r#""helperPid":4242"#, r#""helperPid":0"#),
        ] {
            assert!(BOOT.contains(from), "{from}");
            assert!(matches!(Request::parse(&BOOT.replace(from, to)), Err(Answer::Refused { id: Some(1), .. })), "{to}");
        }
        let long = format!(r#""commandLine":"{}""#, "x".repeat(4097));
        assert!(Request::parse(&BOOT.replace(r#""commandLine":"console=ttyS0 quiet""#, &long)).is_err());
    }

    #[test]
    fn takes_a_share_s_name_as_r_and_a_number_alone() {
        assert_eq!(share_name("r1"), Ok(1));
        assert_eq!(share_name("r123456789"), Ok(123_456_789));
        for name in ["", "r", "r0", "r01", "s1", "r1x", "r1234567890", "R1", "r-1"] {
            assert!(share_name(name).is_err(), "{name}");
        }
    }

    #[test]
    fn writes_each_answer_as_one_line() {
        assert_eq!(Answer::Created { id: 1, runtime_id: "6b3c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f".into() }.line(), "{\"type\":\"created\",\"id\":1,\"runtimeId\":\"6b3c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f\"}\n");
        assert_eq!(Answer::refused(None, "no").line(), "{\"type\":\"refused\",\"id\":null,\"why\":\"no\"}\n");
        assert_eq!(Answer::Stopped { reason: Reason::Guest, why: None }.line(), "{\"type\":\"stopped\",\"reason\":\"guest\"}\n");
        assert_eq!(Answer::Console { text: "a\nb".into() }.line(), "{\"type\":\"console\",\"text\":\"a\\nb\"}\n");
    }
}
