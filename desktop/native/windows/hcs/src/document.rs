// The compute system document of a guest's VM (HcsCreateComputeSystem's configuration: a ComputeSystem). Its device
// list is the whole of what crosses into the guest: three SCSI disks, Hyper-V sockets under the user's descriptor,
// a Plan 9 device that shares are added to while it runs, and one COM port for the console. Nothing else: no
// network adapter, no virtio-serial port, no GPU, no persistent memory and no mapped pipes.
//
// Each field: the schema reference's type, and the version it is new in (SchemaReference, "Schema Version Map").

use std::collections::BTreeMap;

use serde::Serialize;

use crate::sid::socket_descriptor;

/// The schema version every document is written in: 2.6, the first of Windows 11 (10.0.22000.194), and the first
/// with an attachment's AlwaysAllowSparseFiles, which the image's sparse file needs.
pub const SCHEMA: (u32, u32) = (2, 6);
/// The guest's memory, as on Linux and macOS: 2 GiB, which the guest gives back as it frees it (EnableColdDiscardHint).
pub const MEMORY_MB: u64 = 2048;
/// The most processors a request may give the guest.
pub const MAX_CPUS: u32 = 64;

/// What a guest's VM is made of, as the service has checked it: every path a file's, the SID the token's.
#[derive(Debug, Clone)]
pub struct Machine {
    /// The image's kernel (a bzImage) and its initrd, which LinuxKernelDirect boots.
    pub kernel: String,
    pub initrd: String,
    pub command_line: String,
    /// LUN 0: the image, read-only; LUN 1: the agent disk, read-only; LUN 2: the sessions disk.
    pub rootfs: String,
    pub agent_disk: String,
    pub sessions: String,
    pub cpus: u32,
    /// The user whose processes bind and connect the VM's Hyper-V sockets.
    pub user_sid: String,
    /// The named pipe COM1 is connected to: the guest's console, ttyS0.
    pub console: String,
}

// ComputeSystem (2.0).
#[derive(Serialize)]
#[serde(rename_all = "PascalCase")]
struct ComputeSystem {
    // Owner (2.0): "A string identifying the owning client for this system."
    owner: &'static str,
    schema_version: Version,
    // ShouldTerminateOnLastHandleClosed (2.0): "If true, this system will be forcibly terminated when the last
    // HCS_SYSTEM handle corresponding to it is closed." The service holds the one handle, and closes it when the
    // user's surogate-vm.exe goes; and every handle closes with the service's own end.
    should_terminate_on_last_handle_closed: bool,
    virtual_machine: VirtualMachine,
}

// Version (2.0): Major, Minor.
#[derive(Serialize)]
#[serde(rename_all = "PascalCase")]
struct Version {
    major: u32,
    minor: u32,
}

// VirtualMachine (2.0).
#[derive(Serialize)]
#[serde(rename_all = "PascalCase")]
struct VirtualMachine {
    // StopOnReset (2.0): "the virtual machine will treat a reset as a stop": the guest's panic=-1 reboots, and its
    // VM then ends, as QEMU's -no-reboot ends Linux's.
    stop_on_reset: bool,
    chipset: Chipset,
    compute_topology: Topology,
    devices: Devices,
}

// Chipset (2.0).
#[derive(Serialize)]
#[serde(rename_all = "PascalCase")]
struct Chipset {
    // UseUtc (2.1): the RTC in UTC, as Linux reads it.
    use_utc: bool,
    // LinuxKernelDirect (2.2): the kernel booted directly, without firmware.
    linux_kernel_direct: LinuxKernelDirect,
}

// LinuxKernelDirect (2.2): KernelFilePath, InitRdPath, KernelCmdLine.
#[derive(Serialize)]
#[serde(rename_all = "PascalCase")]
struct LinuxKernelDirect {
    kernel_file_path: String,
    init_rd_path: String,
    kernel_cmd_line: String,
}

// Topology (2.0): Memory and Processor.
#[derive(Serialize)]
#[serde(rename_all = "PascalCase")]
struct Topology {
    memory: Memory,
    processor: Processor,
}

// VirtualMachine_Memory (2.1), with WSL's three settings (WslCoreVm.cpp, GenerateConfigJson).
#[derive(Serialize)]
#[serde(rename_all = "PascalCase")]
struct Memory {
    // SizeInMB (2.1).
    #[serde(rename = "SizeInMB")]
    size_in_mb: u64,
    // AllowOvercommit (2.1): "the VM's memory is backed by the Windows pagefile rather than physically backed".
    allow_overcommit: bool,
    // EnableDeferredCommit (2.1): "commit is not charged for each backing page until first access".
    enable_deferred_commit: bool,
    // EnableColdDiscardHint (2.2): the guest may "trim non-zeroed pages from the working set": what it frees goes
    // back to Windows, as hv_balloon's free page reporting gives it.
    enable_cold_discard_hint: bool,
}

// VirtualMachine_Processor (2.0): Count.
#[derive(Serialize)]
#[serde(rename_all = "PascalCase")]
struct Processor {
    count: u32,
}

// Devices (2.0): only the four below.
#[derive(Serialize)]
#[serde(rename_all = "PascalCase")]
struct Devices {
    // Scsi (2.0): "SCSI controllers, identified by friendly name"; one, "0", as WSL names its own.
    scsi: BTreeMap<&'static str, Scsi>,
    // HvSocket (2.1): VirtualMachine_HvSocket.
    hv_socket: HvSocket,
    // Plan9 (2.0): "any Plan9 shares to be exposed to the guest OS": none at the start; each folder is added while
    // the guest runs (share.rs).
    plan9: Plan9,
    // ComPorts (2.1): "The key in the map is the integer, starting from zero": COM1, the guest's ttyS0.
    com_ports: BTreeMap<&'static str, ComPort>,
}

// Scsi (2.0): Attachments, "where the key is the integer LUN number on the controller".
#[derive(Serialize)]
#[serde(rename_all = "PascalCase")]
struct Scsi {
    attachments: BTreeMap<&'static str, Attachment>,
}

// Attachment (2.0).
#[derive(Serialize)]
#[serde(rename_all = "PascalCase")]
struct Attachment {
    // Type (2.0): AttachmentType "VirtualDisk", a VHD or VHDX file.
    r#type: &'static str,
    // Path (2.0).
    path: String,
    // ReadOnly (2.0).
    read_only: bool,
    // AlwaysAllowSparseFiles (2.6): the image is unpacked sparse; Hyper-V otherwise refuses a sparse file.
    always_allow_sparse_files: bool,
}

// VirtualMachine_HvSocket (2.1): HvSocketConfig.
#[derive(Serialize)]
#[serde(rename_all = "PascalCase")]
struct HvSocket {
    hv_socket_config: HvSocketSystemConfig,
}

// HvSocketSystemConfig (2.0): the SDDL "HvSocket will check before allowing a host process to bind to an unlisted
// service for this specific container/VM", and to connect to one: the user's own surogate-vm.exe binds the guest's
// ports, so the user's SID and SYSTEM's alone. No ServiceTable: no service is listed, and no wildcard bind is taken.
#[derive(Serialize)]
#[serde(rename_all = "PascalCase")]
struct HvSocketSystemConfig {
    default_bind_security_descriptor: String,
    default_connect_security_descriptor: String,
}

// Plan9 (2.0); its Shares (2.1) are added later, so the object is empty.
#[derive(Serialize)]
struct Plan9 {}

// ComPort (2.1): NamedPipe.
#[derive(Serialize)]
#[serde(rename_all = "PascalCase")]
struct ComPort {
    named_pipe: String,
}

/// The ComputeSystem document of *machine*'s VM, or why there is none: a SID that is not one, or a processor count
/// out of range.
pub fn document(machine: &Machine) -> Result<String, String> {
    let descriptor = socket_descriptor(&machine.user_sid).ok_or("the caller's SID is not one")?;
    if machine.cpus == 0 || machine.cpus > MAX_CPUS {
        return Err(format!("a guest takes 1 to {MAX_CPUS} processors, not {}", machine.cpus));
    }
    let disk = |path: &str, read_only: bool| Attachment { r#type: "VirtualDisk", path: path.to_owned(), read_only, always_allow_sparse_files: true };
    let system = ComputeSystem {
        owner: "Surogate",
        schema_version: Version { major: SCHEMA.0, minor: SCHEMA.1 },
        should_terminate_on_last_handle_closed: true,
        virtual_machine: VirtualMachine {
            stop_on_reset: true,
            chipset: Chipset {
                use_utc: true,
                linux_kernel_direct: LinuxKernelDirect {
                    kernel_file_path: machine.kernel.clone(),
                    init_rd_path: machine.initrd.clone(),
                    kernel_cmd_line: machine.command_line.clone(),
                },
            },
            compute_topology: Topology {
                memory: Memory { size_in_mb: MEMORY_MB, allow_overcommit: true, enable_deferred_commit: true, enable_cold_discard_hint: true },
                processor: Processor { count: machine.cpus },
            },
            devices: Devices {
                scsi: BTreeMap::from([(
                    "0",
                    Scsi {
                        attachments: BTreeMap::from([
                            ("0", disk(&machine.rootfs, true)),
                            ("1", disk(&machine.agent_disk, true)),
                            ("2", disk(&machine.sessions, false)),
                        ]),
                    },
                )]),
                hv_socket: HvSocket {
                    hv_socket_config: HvSocketSystemConfig { default_bind_security_descriptor: descriptor.clone(), default_connect_security_descriptor: descriptor },
                },
                plan9: Plan9 {},
                com_ports: BTreeMap::from([("0", ComPort { named_pipe: machine.console.clone() })]),
            },
        },
    };
    serde_json::to_string(&system).map_err(|error| error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};

    fn machine() -> Machine {
        Machine {
            kernel: r"C:\Users\ana\AppData\Local\Surogate\vm\image\vmlinuz".into(),
            initrd: r"C:\Users\ana\AppData\Local\Surogate\vm\image\initrd".into(),
            command_line: "console=ttyS0 panic=-1 quiet surogate.disks=scsi surogate.transport=vsock surogate.names=windows lsm=landlock,lockdown,yama,integrity,apparmor,bpf".into(),
            rootfs: r"C:\Users\ana\AppData\Local\Surogate\vm\image\rootfs.vhd".into(),
            agent_disk: r"C:\Program Files\Surogate\resources\vm\agent.vhd".into(),
            sessions: r"C:\Users\ana\AppData\Local\Surogate\vm\sessions.vhdx".into(),
            cpus: 4,
            user_sid: "S-1-5-21-1-2-3-1001".into(),
            console: r"\\.\pipe\surogate-console-6b3c".into(),
        }
    }

    #[test]
    fn is_the_whole_document_field_for_field() {
        let made: Value = serde_json::from_str(&document(&machine()).unwrap()).unwrap();
        let disk = |path: &str, read_only: bool| json!({ "Type": "VirtualDisk", "Path": path, "ReadOnly": read_only, "AlwaysAllowSparseFiles": true });
        let descriptor = "D:P(A;;FA;;;SY)(A;;FA;;;S-1-5-21-1-2-3-1001)";
        assert_eq!(
            made,
            json!({
                "Owner": "Surogate",
                "SchemaVersion": { "Major": 2, "Minor": 6 },
                "ShouldTerminateOnLastHandleClosed": true,
                "VirtualMachine": {
                    "StopOnReset": true,
                    "Chipset": {
                        "UseUtc": true,
                        "LinuxKernelDirect": {
                            "KernelFilePath": r"C:\Users\ana\AppData\Local\Surogate\vm\image\vmlinuz",
                            "InitRdPath": r"C:\Users\ana\AppData\Local\Surogate\vm\image\initrd",
                            "KernelCmdLine": machine().command_line,
                        },
                    },
                    "ComputeTopology": {
                        "Memory": { "SizeInMB": 2048, "AllowOvercommit": true, "EnableDeferredCommit": true, "EnableColdDiscardHint": true },
                        "Processor": { "Count": 4 },
                    },
                    "Devices": {
                        "Scsi": { "0": { "Attachments": {
                            "0": disk(r"C:\Users\ana\AppData\Local\Surogate\vm\image\rootfs.vhd", true),
                            "1": disk(r"C:\Program Files\Surogate\resources\vm\agent.vhd", true),
                            "2": disk(r"C:\Users\ana\AppData\Local\Surogate\vm\sessions.vhdx", false),
                        } } },
                        "HvSocket": { "HvSocketConfig": { "DefaultBindSecurityDescriptor": descriptor, "DefaultConnectSecurityDescriptor": descriptor } },
                        "Plan9": {},
                        "ComPorts": { "0": { "NamedPipe": r"\\.\pipe\surogate-console-6b3c" } },
                    },
                },
            })
        );
    }

    #[test]
    fn crosses_nothing_but_its_four_devices() {
        let made: Value = serde_json::from_str(&document(&machine()).unwrap()).unwrap();
        let devices = made["VirtualMachine"]["Devices"].as_object().unwrap();
        assert_eq!(devices.keys().map(String::as_str).collect::<Vec<_>>(), ["ComPorts", "HvSocket", "Plan9", "Scsi"]);
        for never in ["NetworkAdapters", "VirtioSerial", "VirtualPMem", "VirtualPci", "FlexibleIov", "VirtualSmb", "SharedMemory", "MappedPipes", "Mappings", "Gpu"] {
            assert!(!document(&machine()).unwrap().contains(never), "{never}");
        }
        // The image and the agent disk read-only, the sessions disk alone written.
        let attachments = &made["VirtualMachine"]["Devices"]["Scsi"]["0"]["Attachments"];
        assert_eq!([&attachments["0"]["ReadOnly"], &attachments["1"]["ReadOnly"], &attachments["2"]["ReadOnly"]], [&json!(true), &json!(true), &json!(false)]);
    }

    #[test]
    fn refuses_a_sid_that_is_not_one_and_a_processor_count_out_of_range() {
        assert_eq!(document(&Machine { user_sid: "S-1-5-21-1)(A;;FA;;;WD".into(), ..machine() }), Err("the caller's SID is not one".into()));
        assert!(document(&Machine { cpus: 0, ..machine() }).is_err());
        assert!(document(&Machine { cpus: 65, ..machine() }).is_err());
        assert!(document(&Machine { cpus: 64, ..machine() }).is_ok());
    }

    #[test]
    fn keeps_a_path_s_own_characters_as_json_strings() {
        let odd = Machine { rootfs: "C:\\Users\\ana \"x\"\\rootfs.vhd".into(), ..machine() };
        let made: Value = serde_json::from_str(&document(&odd).unwrap()).unwrap();
        assert_eq!(made["VirtualMachine"]["Devices"]["Scsi"]["0"]["Attachments"]["0"]["Path"], json!("C:\\Users\\ana \"x\"\\rootfs.vhd"));
    }
}
