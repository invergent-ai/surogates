// The Host Compute Service for one caller (hcs::api): the VM made from the service's document, each file it opens
// granted to it alone (HcsGrantVmAccess), the caller's own as the caller and the installed app's agent disk by the
// service's own right, its console read from before it is made, its exit told, and every grant taken back by the right
// that made it once the VM has gone, so no entry for a VM that is no more stays in a file's ACL. No file is given to a
// group of VMs: Windows names the VM group (S-1-5-83-0) only where Hyper-V's own service is installed, and Virtual
// Machine Platform alone, as most Windows 11 PCs have it, installs no such group.

use std::sync::Arc;
use std::thread;

use hcs::api::{self, System};
use hcs::properties::{exit_status, runtime_id, supports};
use hcs::{Failure, SCHEMA};
use pipe::win::{gone, Handle, Pipe, Way};
use windows_sys::core::GUID;
use windows_sys::Win32::System::Com::CoCreateGuid;

use crate::caller::as_caller;
use crate::core::{By, Grant, Hypervisor, Vm};

/// What a VM tells its connection unasked.
pub enum Notice {
    /// The guest's console printed this.
    Console(String),
    /// The VM exited, with HCS's status.
    Exited(i32),
}

pub type Notify = Arc<dyn Fn(Notice) + Send + Sync>;

/// HCS, as the caller of *token* asks it.
pub struct Hcs<'a> {
    pub token: &'a Handle,
    pub notify: Notify,
}

/// The most of the console one notice carries.
const CONSOLE_CHUNK: usize = 4096;
/// The console's pipe's descriptor: SYSTEM and administrators all access, as the service's default descriptor gives
/// them and the only write it gives, which HCS's worker opens COM1's other end with; and no one else, not even to read,
/// as the default lets everyone do: a user who took the pipe's one instance first would leave the guest's console with
/// nowhere to go.
pub const CONSOLE_SDDL: &str = "D:P(A;;GA;;;SY)(A;;GA;;;BA)";

/// The guest's console, as the service reads it: the server's end of COM1's pipe, read on a thread of its own until
/// this goes, which ends the read.
pub struct Console(Arc<Pipe>);

impl Drop for Console {
    fn drop(&mut self) {
        self.0.cancel();
    }
}

/// The console's pipe *name*, made by the service before the VM, which HCS's worker connects to as COM1's other end,
/// and what the guest prints there told to *notify*. Both ways and one instance, as WSL's service makes its own (WSL,
/// src/windows/common/Dmesg.cpp, CreateConsolePipe: PIPE_ACCESS_DUPLEX): the worker opens a serial port's other end
/// to read and to write, which a pipe its server alone reads refuses.
pub fn console(name: &str, notify: Notify) -> Result<Console, Failure> {
    let pipe = Arc::new(Pipe::server(name, Some(CONSOLE_SDDL), Way::Duplex, true, true).map_err(|error| Failure { code: -1, words: format!("the console's pipe could not be made: {error}") })?);
    let reading = pipe.clone();
    thread::spawn(move || {
        if reading.accept().is_err() {
            return;
        }
        let mut buffer = vec![0u8; CONSOLE_CHUNK];
        loop {
            match reading.read(&mut buffer) {
                Ok(0) => return,
                Ok(read) => notify(Notice::Console(String::from_utf8_lossy(&buffer[..read]).into_owned())),
                Err(error) if gone(&error) => return,
                Err(_) => return,
            }
        }
    });
    Ok(Console(pipe))
}

/// One VM, by the service's one handle to it.
pub struct HcsVm {
    system: Option<System>,
    id: String,
    granted: Vec<Grant>,
    token: Handle,
}

impl Vm for HcsVm {
    fn start(&self) -> Result<(), Failure> {
        self.system().start()
    }

    fn share(&self, name: &str, path: &str, read_only: bool) -> Result<(), Failure> {
        // The caller's own token as the identity the share's settings are applied with (HcsModifyComputeSystem).
        unsafe { self.system().modify(&hcs::add_share(name, path, read_only), self.token.0) }
    }

    fn unshare(&self, name: &str) -> Result<(), Failure> {
        unsafe { self.system().modify(&hcs::remove_share(name), self.token.0) }
    }

    fn terminate(&self) -> Result<(), Failure> {
        self.system().terminate()
    }
}

impl HcsVm {
    fn system(&self) -> &System {
        self.system.as_ref().expect("a VM's system is held until it goes")
    }
}

impl Drop for HcsVm {
    fn drop(&mut self) {
        // The last handle closed ends the VM (ShouldTerminateOnLastHandleClosed); then its grants.
        self.system.take();
        revoke(&self.token, &self.id, &self.granted);
    }
}

// *path* granted to the VM *id*, by the caller's right (as the caller of *token*) or by the service's own.
fn grant(token: &Handle, id: &str, path: &str, by: By) -> Result<(), Failure> {
    match by {
        By::Caller => as_caller(token, || api::grant_vm_access(id, path)).map_err(|words| Failure { code: -1, words })?,
        By::Service => api::grant_vm_access(id, path),
    }
}

fn revoke(token: &Handle, id: &str, granted: &[Grant]) {
    for Grant { path, by } in granted {
        let _ = match by {
            By::Caller => as_caller(token, || api::revoke_vm_access(id, path)).map(drop),
            By::Service => api::revoke_vm_access(id, path).map_err(|failure| failure.words),
        };
    }
}

fn duplicate(token: &Handle) -> Result<Handle, Failure> {
    use windows_sys::Win32::Foundation::{DuplicateHandle, DUPLICATE_SAME_ACCESS};
    use windows_sys::Win32::System::Threading::GetCurrentProcess;
    let mut copy = std::ptr::null_mut();
    if unsafe { DuplicateHandle(GetCurrentProcess(), token.0, GetCurrentProcess(), &mut copy, 0, 0, DUPLICATE_SAME_ACCESS) } == 0 {
        return Err(Failure { code: -1, words: format!("the caller's token could not be kept: {}", std::io::Error::last_os_error()) });
    }
    Ok(Handle(copy))
}

impl Hypervisor for Hcs<'_> {
    type Vm = HcsVm;
    type Console = Console;

    fn new_id(&self) -> String {
        let mut guid: GUID = unsafe { std::mem::zeroed() };
        unsafe { CoCreateGuid(&mut guid) };
        format!(
            "{:08x}-{:04x}-{:04x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
            guid.data1, guid.data2, guid.data3, guid.data4[0], guid.data4[1], guid.data4[2], guid.data4[3], guid.data4[4], guid.data4[5], guid.data4[6], guid.data4[7]
        )
    }

    fn console(&self, id: &str) -> String {
        format!(r"\\.\pipe\surogate-console-{id}")
    }

    fn listen(&self, name: &str) -> Result<Console, Failure> {
        console(name, self.notify.clone())
    }

    fn create(&self, id: &str, document: &str, granted: &[Grant]) -> Result<(HcsVm, String), Failure> {
        let basic = api::service_properties(r#"{"PropertyTypes":["Basic"]}"#)?;
        if !supports(&basic, SCHEMA) {
            return Err(Failure { code: -1, words: format!("this Windows's Host Compute Service does not take schema {}.{}: {basic}", SCHEMA.0, SCHEMA.1) });
        }
        let token = duplicate(self.token)?;
        let mut given = Vec::new();
        for each in granted {
            if let Err(failure) = grant(&token, id, &each.path, each.by) {
                revoke(&token, id, &given);
                return Err(Failure { code: failure.code, words: format!("{} for {}", failure.words, each.path) });
            }
            given.push(each.clone());
        }
        let made = System::create(id, document).and_then(|system| {
            let notify = self.notify.clone();
            system.on_exit(Box::new(move |status| notify(Notice::Exited(exit_status(status.as_deref())))))?;
            let properties = system.properties(None)?;
            let runtime = runtime_id(&properties).ok_or_else(|| Failure { code: -1, words: format!("HCS named no runtime ID for the VM: {properties}") })?;
            Ok((system, runtime))
        });
        match made {
            Ok((system, runtime)) => Ok((HcsVm { system: Some(system), id: id.to_owned(), granted: given, token }, runtime)),
            Err(failure) => {
                revoke(&token, id, &given);
                Err(failure)
            }
        }
    }
}
