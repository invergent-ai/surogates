// A folder shared into the running guest by HCS's own Plan 9 server: HcsModifyComputeSystem's ModifySettingRequest
// (2.0) at "VirtualMachine/Devices/Plan9/Shares", whose Settings are a Plan9Share (HcsModifyComputeSystem's
// remarks list the path and its type). The guest mounts it from the server's vsock port by its AccessName, with
// what follows the name in its aname (guest/root.ts, p9Mount).

use serde::Serialize;

/// The Plan 9 server's port, which every share is served on and told apart by its name (guest/protocol.ts,
/// PLAN9_PORT): hcsshim's guest dials it (internal/guest/storage/plan9/plan9.go).
pub const PLAN9_PORT: u32 = 564;

/// Plan9Share's Flags. The schema reference leaves them out; WSL's src/windows/common/hcs_schema.h (Plan9ShareFlags)
/// and hcsshim's internal/hcs/schema2/plan9_share_flags.go agree on each value used here.
pub struct ShareFlags;
impl ShareFlags {
    /// The guest may not write through the share.
    pub const READ_ONLY: u32 = 0x1;
    /// The server opens the share's files as the identity HcsModifyComputeSystem is given: the caller's own token
    /// (WSL's AddPlan9Share sets it whenever it passes the user's token). Unsure: what the server does with the
    /// identity besides, which no source says; whose rights a share uses is measured on a PC before any folder is
    /// shared (test/vm/win32/measure/identity.ts).
    pub const USE_SHARE_ROOT_IDENTITY: u32 = 0x10;
    /// The server takes options after the name in the guest's aname: "<name>;uid=<n>;gid=<n>", WSL drvfs's form
    /// (WSL, drvfs.cpp). Unsure: which options HCS's own server takes (test/vm/win32/measure/owners.ts).
    pub const ALLOW_OPTIONS: u32 = 0x20;
}

/// The flags of every share: the caller's identity and the guest's uid at the mount; read-only for a place's folder.
pub fn flags(read_only: bool) -> u32 {
    ShareFlags::USE_SHARE_ROOT_IDENTITY | ShareFlags::ALLOW_OPTIONS | if read_only { ShareFlags::READ_ONLY } else { 0 }
}

// ModifySettingRequest (2.0): ResourcePath (2.1), RequestType (2.0), Settings (2.0).
#[derive(Serialize)]
#[serde(rename_all = "PascalCase")]
struct Request<T> {
    resource_path: &'static str,
    request_type: &'static str,
    settings: T,
}

const SHARES: &str = "VirtualMachine/Devices/Plan9/Shares";

// Plan9Share (2.0): Name (2.0), AccessName (2.1, "The name by which the guest operation system can access this
// share, via the aname parameter in the Plan9 protocol"), Path (2.0), Port (2.0), and Flags (above).
#[derive(Serialize)]
#[serde(rename_all = "PascalCase")]
struct Added<'a> {
    name: &'a str,
    access_name: &'a str,
    path: &'a str,
    port: u32,
    flags: u32,
}

// What a removal names: the share's AccessName and port, as WSL's RemovePlan9Share does (hcs.cpp).
#[derive(Serialize)]
#[serde(rename_all = "PascalCase")]
struct Removed<'a> {
    access_name: &'a str,
    port: u32,
}

/// The request that adds the share *name* of the folder *path*.
pub fn add_share(name: &str, path: &str, read_only: bool) -> String {
    let settings = Added { name, access_name: name, path, port: PLAN9_PORT, flags: flags(read_only) };
    serde_json::to_string(&Request { resource_path: SHARES, request_type: "Add", settings }).expect("a share's request is JSON")
}

/// The request that removes the share *name*.
pub fn remove_share(name: &str) -> String {
    serde_json::to_string(&Request { resource_path: SHARES, request_type: "Remove", settings: Removed { access_name: name, port: PLAN9_PORT } })
        .expect("a share's request is JSON")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};

    #[test]
    fn adds_a_share_by_its_name_on_the_plan9_port_with_the_caller_s_identity_and_options() {
        let made: Value = serde_json::from_str(&add_share("r3", r"C:\Users\ana\Documents\shop", false)).unwrap();
        assert_eq!(
            made,
            json!({
                "ResourcePath": "VirtualMachine/Devices/Plan9/Shares",
                "RequestType": "Add",
                "Settings": { "Name": "r3", "AccessName": "r3", "Path": r"C:\Users\ana\Documents\shop", "Port": 564, "Flags": 0x30 },
            })
        );
        let read_only: Value = serde_json::from_str(&add_share("r4", r"C:\x", true)).unwrap();
        assert_eq!(read_only["Settings"]["Flags"], json!(0x31));
    }

    #[test]
    fn removes_a_share_by_its_name_and_port() {
        let made: Value = serde_json::from_str(&remove_share("r3")).unwrap();
        assert_eq!(made, json!({ "ResourcePath": "VirtualMachine/Devices/Plan9/Shares", "RequestType": "Remove", "Settings": { "AccessName": "r3", "Port": 564 } }));
    }
}
