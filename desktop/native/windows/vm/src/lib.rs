// surogate-vm.exe, the user's side of one VM, as the app's VM manager runs it (src/vm/win32.ts): it speaks the VM
// helper's line protocol on its stdin and stdout (src/vm/vm-helper.ts), asks the Surogate VM service for the VM, binds
// the guest's three Hyper-V sockets as the user, and serves each as a named pipe only the user may open. Its lines and
// arguments are plain data (here); everything else is Windows's (run.rs, check.rs).

use serde::{Deserialize, Serialize};

pub use pipe::lines::{Lines, Read};

#[cfg(windows)]
pub mod check;
#[cfg(windows)]
pub mod service;

/// What the VM manager says, one JSON object a line.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(tag = "type", rename_all = "camelCase", deny_unknown_fields)]
pub enum Told {
    #[serde(rename_all = "camelCase")]
    Boot { kernel: String, initrd: String, rootfs: String, agent_disk: String, sessions: String, console: String, cpus: u32, command_line: String },
    #[serde(rename_all = "camelCase")]
    Share { id: u32, name: String, path: String, read_only: bool },
    Unshare { id: u32, name: String },
    Kill {},
}

impl Told {
    pub fn parse(line: &str) -> Result<Told, Said> {
        let id = serde_json::from_str::<serde_json::Value>(line).ok().and_then(|value| value.get("id").and_then(serde_json::Value::as_u64)).and_then(|id| u32::try_from(id).ok());
        serde_json::from_str(line).map_err(|_| Said::Refused { id, why: "a line that is not a JSON object of a type, its fields each of theirs".into() })
    }
}

/// What surogate-vm.exe says, one JSON object a line: vm-helper.ts's Said.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "type", rename_all = "camelCase")]
pub enum Said {
    Listening,
    Started,
    Failed {
        why: String,
    },
    Shared {
        id: u32,
        tag: String,
    },
    Unshared {
        id: u32,
    },
    Refused {
        #[serde(skip_serializing_if = "Option::is_none")]
        id: Option<u32>,
        why: String,
    },
    Stopped {
        reason: &'static str,
        #[serde(skip_serializing_if = "Option::is_none")]
        why: Option<String>,
    },
}

impl Said {
    pub fn line(&self) -> String {
        let mut line = serde_json::to_string(self).expect("a line is JSON");
        line.push('\n');
        line
    }
}

/// The three pipes the guest's ports are served on, as the VM manager names them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Pipes {
    pub control: String,
    pub net: String,
    pub inbound: String,
}

/// What `run` is told: the runtime folder, the data folder, and the three pipes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Run {
    pub run: String,
    pub data: String,
    pub pipes: Pipes,
}

/// What surogate-vm.exe is asked to do, from its arguments.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Command {
    Run(Run),
    Check,
}

/// Whether *name* is a pipe of the guest's port *port*: \\.\pipe\surogate-, then lower-case letters, digits and dashes,
/// ending -<port>. The pipes are made here, for the user alone, so the name may say nothing else.
pub fn pipe_name(name: &str, port: &str) -> bool {
    name.strip_prefix(r"\\.\pipe\surogate-")
        .and_then(|rest| rest.strip_suffix(port))
        .and_then(|rest| rest.strip_suffix('-'))
        .is_some_and(|middle| !middle.is_empty() && middle.len() <= 64 && middle.bytes().all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-'))
}

const USAGE: &str = "usage: surogate-vm run <runtime folder> --data <data folder> --control <pipe> --net <pipe> --inbound <pipe> | surogate-vm check";

/// The command *arguments* (without the program's own name) ask for.
pub fn command(arguments: &[String]) -> Result<Command, String> {
    match arguments {
        [check] if check == "check" => Ok(Command::Check),
        [run, folder, data_flag, data, control_flag, control, net_flag, net, inbound_flag, inbound]
            if run == "run" && data_flag == "--data" && control_flag == "--control" && net_flag == "--net" && inbound_flag == "--inbound" =>
        {
            for path in [folder, data] {
                pipe::path::checked(path).map_err(|why| format!("{why}: {path}"))?;
            }
            for (name, port) in [(control, "control"), (net, "net"), (inbound, "inbound")] {
                if !pipe_name(name, port) {
                    return Err(format!("not a pipe of the guest's {port} port: {name}"));
                }
            }
            Ok(Command::Run(Run { run: folder.clone(), data: data.clone(), pipes: Pipes { control: control.clone(), net: net.clone(), inbound: inbound.clone() } }))
        }
        _ => Err(USAGE.into()),
    }
}

/// The descriptor of the guest's pipes: the user *sid* alone, all access; none other, the service's SYSTEM included.
pub fn pipe_descriptor(sid: &str) -> Option<String> {
    hcs::is_sid(sid).then(|| format!("D:P(A;;GA;;;{sid})"))
}

/// The guest's vsock port each pipe carries (guest/protocol.ts, VSOCK_PORTS).
pub const PORTS: [u32; 3] = [5000, 5001, 5002];

/// A GUID's fields from its string form, 8-4-4-4-12 hex digits: Data1, Data2, Data3 and Data4, as GUID holds them.
pub fn guid(text: &str) -> Option<(u32, u16, u16, [u8; 8])> {
    if !hcs::properties::is_guid(text) {
        return None;
    }
    let hex: String = text.chars().filter(|c| *c != '-').collect();
    let byte = |at: usize| u8::from_str_radix(&hex[at..at + 2], 16).ok();
    let mut data4 = [0u8; 8];
    for (n, slot) in data4.iter_mut().enumerate() {
        *slot = byte(16 + 2 * n)?;
    }
    Some((u32::from_str_radix(&hex[0..8], 16).ok()?, u16::from_str_radix(&hex[8..12], 16).ok()?, u16::from_str_radix(&hex[12..16], 16).ok()?, data4))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn arguments(text: &str) -> Vec<String> {
        text.split(' ').map(str::to_owned).collect()
    }

    #[test]
    fn takes_run_with_its_folders_and_three_pipes_and_check() {
        let run = command(&arguments(r"run C:\Users\ana\AppData\Local\Surogate\run\vm-0b6c1d3e --data C:\Users\ana\AppData\Local\Surogate --control \\.\pipe\surogate-0b6c1d3e9a5f2c4b-control --net \\.\pipe\surogate-0b6c1d3e9a5f2c4b-net --inbound \\.\pipe\surogate-0b6c1d3e9a5f2c4b-inbound")).unwrap();
        assert_eq!(
            run,
            Command::Run(Run {
                run: r"C:\Users\ana\AppData\Local\Surogate\run\vm-0b6c1d3e".into(),
                data: r"C:\Users\ana\AppData\Local\Surogate".into(),
                pipes: Pipes {
                    control: r"\\.\pipe\surogate-0b6c1d3e9a5f2c4b-control".into(),
                    net: r"\\.\pipe\surogate-0b6c1d3e9a5f2c4b-net".into(),
                    inbound: r"\\.\pipe\surogate-0b6c1d3e9a5f2c4b-inbound".into(),
                },
            })
        );
        assert_eq!(command(&arguments("check")), Ok(Command::Check));
    }

    #[test]
    fn refuses_a_pipe_that_is_not_the_guest_s_and_a_folder_not_of_the_one_form() {
        assert!(pipe_name(r"\\.\pipe\surogate-0b6c1d3e9a5f2c4b-net", "net"));
        for name in [r"\\.\pipe\surogate-vm", r"\\.\pipe\surogate--net", r"\\.\pipe\other-1-net", r"\\.\pipe\surogate-1-control", r"\\.\pipe\surogate-A-net", r"\\server\pipe\surogate-1-net", r"\\.\pipe\surogate-1\x-net"] {
            assert!(!pipe_name(name, "net"), "{name}");
        }
        assert!(command(&arguments(r"run C:\x --data C:\y --control \\.\pipe\surogate-vm --net \\.\pipe\surogate-1-net --inbound \\.\pipe\surogate-1-inbound")).is_err());
        assert!(command(&arguments(r"run C:\x\..\y --data C:\y --control \\.\pipe\surogate-1-control --net \\.\pipe\surogate-1-net --inbound \\.\pipe\surogate-1-inbound")).is_err());
        assert_eq!(command(&arguments("run")), Err(USAGE.into()));
        assert_eq!(command(&[]), Err(USAGE.into()));
    }

    #[test]
    fn reads_the_vm_manager_s_lines_in_their_one_form() {
        assert_eq!(Told::parse(r#"{"type":"kill"}"#), Ok(Told::Kill {}));
        assert_eq!(Told::parse(r#"{"type":"unshare","id":3,"name":"r1"}"#), Ok(Told::Unshare { id: 3, name: "r1".into() }));
        assert!(matches!(Told::parse(r#"{"type":"boot","kernel":"a","initrd":"b","rootfs":"c","agentDisk":"d","sessions":"e","console":"f","cpus":2,"commandLine":"g"}"#), Ok(Told::Boot { cpus: 2, .. })));
        assert_eq!(Told::parse(r#"{"type":"share","id":4,"name":"r1","path":"C:\\x"}"#), Err(Said::Refused { id: Some(4), why: "a line that is not a JSON object of a type, its fields each of theirs".into() }));
        assert!(Told::parse(r#"{"type":"kill","now":true}"#).is_err());
        assert!(Told::parse("null").is_err());
    }

    #[test]
    fn says_its_lines_as_the_vm_manager_reads_them() {
        assert_eq!(Said::Listening.line(), "{\"type\":\"listening\"}\n");
        assert_eq!(Said::Shared { id: 2, tag: "r1".into() }.line(), "{\"type\":\"shared\",\"id\":2,\"tag\":\"r1\"}\n");
        assert_eq!(Said::Stopped { reason: "guest", why: None }.line(), "{\"type\":\"stopped\",\"reason\":\"guest\"}\n");
        assert_eq!(Said::Refused { id: None, why: "no".into() }.line(), "{\"type\":\"refused\",\"why\":\"no\"}\n");
    }

    #[test]
    fn reads_a_guid_s_fields_from_its_string() {
        assert_eq!(guid("6b3c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f"), Some((0x6b3c_1d3e, 0x6f0a, 0x4c1e, [0x9a, 0x52, 0x6a, 0x1d, 0x2c, 0x3b, 0x4e, 0x5f])));
        assert_eq!(guid(&hcs::service_id(5000)), Some((5000, 0xfacb, 0x11e6, [0xbd, 0x58, 0x64, 0x00, 0x6a, 0x79, 0x86, 0xd3])));
        assert_eq!(guid("6b3c1d3e-6f0a-4c1e-9a52"), None);
    }

    #[test]
    fn gives_the_guest_s_pipes_to_the_user_alone() {
        assert_eq!(pipe_descriptor("S-1-5-21-1-2-3-1001").as_deref(), Some("D:P(A;;GA;;;S-1-5-21-1-2-3-1001)"));
        assert_eq!(pipe_descriptor("WD"), None);
    }
}
