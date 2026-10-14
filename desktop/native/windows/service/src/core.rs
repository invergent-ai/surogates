// What the service does with one caller's requests, on any OS: the checks every request passes before anything is
// made, and the one VM a connection may hold. The disk, the caller's identity and the Host Compute Service are behind
// two traits, so every refusal is tested on Linux with fakes; on Windows they are the caller's own token and HCS
// (files.rs, hyper.rs).
//
// A caller is anyone who can open the pipe, so nothing a request names is taken on trust:
// - the caller is an interactive user, by its token (caller.rs), before a line of theirs is acted on;
// - the helper the VM ends with is the caller's own process, by the pipe's own word for it;
// - every file is of path.rs's form, in the caller's own data folder (the agent disk may also be the installed app's
//   own, and no other file of the installed app's), and that data folder in the caller's own profile; and each is
//   opened as the caller, so what the caller cannot reach is refused, with no reparse point on its way;
// - each file is granted to the VM alone (HcsGrantVmAccess) for its life, by the caller's own right to change its ACL;
//   but the installed app's agent disk, whose ACL only administrators may change, by the service's, once the caller has
//   reached it as itself;
// - the document is built here, from those few fields, never taken from the caller;
// - one VM for one data folder, eight shares at most, and a share's name never given twice in a VM.

use std::collections::HashSet;
use std::sync::Mutex;

use hcs::{Failure, Machine};
use pipe::path::within;
use pipe::protocol::MAX_SHARES;
use pipe::{Answer, Reason, Request};

/// What a path must be for the request that names it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    /// A file the caller may read.
    File,
    /// A file the caller may read and write: the sessions disk.
    Written,
    /// A folder the caller may list.
    Folder,
}

/// What a path's check holds until the Host Compute Service has opened it: on Windows, the opened handles of every
/// component of the path, each without delete sharing, so nothing on the path can be renamed or swapped in between
/// (files.rs); nothing on other OSes. Dropped once HCS is done, which closes the handles.
pub struct Checked(#[allow(dead_code)] Box<dyn std::any::Any + Send>);

impl Checked {
    /// The handles (or whatever a backend must keep) held across the HCS call.
    pub fn hold(held: Box<dyn std::any::Any + Send>) -> Checked {
        Checked(held)
    }

    /// Nothing to hold (a check that opens no handle, as the fakes').
    pub fn none() -> Checked {
        Checked(Box::new(()))
    }
}

/// The installed app's agent disk, in its folder (the installer's layout), which every user's VM boots.
pub const AGENT_DISK: &str = r"resources\vm\agent.vhd";

/// Whose right to change a file's ACL its grant to the VM is made with.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum By {
    /// The caller's, as it impersonates the caller: for the caller's own files.
    Caller,
    /// The service's own: for the installed app's agent disk, which only administrators may change.
    Service,
}

/// A file the VM is given access to for its life (HcsGrantVmAccess), taken back as it ends, by the same right.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Grant {
    pub path: String,
    pub by: By,
}

/// The disk as the caller reaches it.
pub trait Files {
    /// The caller's own profile folder (GetUserProfileDirectoryW of its token).
    fn profile(&self) -> &str;
    /// The installed app's folder, which only administrators write; None for a service not run from one.
    fn installed(&self) -> Option<&str>;
    /// That *path* is there as *kind*, reached by the caller through no reparse point on any component, by its own
    /// name; its chain's handles, held so nothing on the path can be swapped before HCS opens it. Else why not.
    fn check(&self, path: &str, kind: Kind) -> Result<Checked, String>;
}

/// A VM, as the Host Compute Service holds it for the service. Dropped, its handle closes, and the VM ends.
pub trait Vm {
    fn start(&self) -> Result<(), Failure>;
    fn share(&self, name: &str, path: &str, read_only: bool) -> Result<(), Failure>;
    fn unshare(&self, name: &str) -> Result<(), Failure>;
    fn terminate(&self) -> Result<(), Failure>;
}

/// The Host Compute Service, for one caller.
pub trait Hypervisor {
    type Vm: Vm;
    /// A VM's console, read while it is held.
    type Console;
    /// A new VM ID, never used before.
    fn new_id(&self) -> String;
    /// The named pipe the VM *id*'s console is read from.
    fn console(&self, id: &str) -> String;
    /// The console's pipe *console* made, and read from now on.
    fn listen(&self, console: &str) -> Result<Self::Console, Failure>;
    /// The VM *id* made from *document*, not started, with the VM given access to each of *granted*, by whose right
    /// each says; its runtime ID.
    fn create(&self, id: &str, document: &str, granted: &[Grant]) -> Result<(Self::Vm, String), Failure>;
}

/// The data folders a VM runs for, of every caller: one VM for each.
#[derive(Default)]
pub struct Roots(Mutex<HashSet<String>>);

impl Roots {
    fn take(&self, data: &str) -> bool {
        self.0.lock().unwrap_or_else(|poisoned| poisoned.into_inner()).insert(data.to_ascii_lowercase())
    }

    fn give_back(&self, data: &str) {
        self.0.lock().unwrap_or_else(|poisoned| poisoned.into_inner()).remove(&data.to_ascii_lowercase());
    }
}

/// Who asks, as their token and the pipe say.
#[derive(Debug, Clone)]
pub struct Caller {
    /// The token's user, in its string form.
    pub sid: String,
    /// The process at the pipe's other end (GetNamedPipeClientProcessId).
    pub pid: u32,
}

struct Booted<V, C> {
    vm: V,
    // Read from before the VM was made until after its handle has closed.
    console: C,
    data: String,
    started: bool,
    // The shares in the guest now, and every name given in this VM.
    live: HashSet<String>,
    named: HashSet<String>,
}

/// One connection's caller, and the VM it holds.
pub struct Session<'a, H: Hypervisor, F: Files> {
    hypervisor: &'a H,
    files: &'a F,
    roots: &'a Roots,
    caller: Caller,
    booted: Option<Booted<H::Vm, H::Console>>,
    ended: bool,
}

const LIMIT: &str = "the service's limit";

impl<'a, H: Hypervisor, F: Files> Session<'a, H, F> {
    pub fn new(hypervisor: &'a H, files: &'a F, roots: &'a Roots, caller: Caller) -> Self {
        Session { hypervisor, files, roots, caller, booted: None, ended: false }
    }

    /// The process whose end is the VM's, once a boot named it.
    pub fn helper(&self) -> Option<u32> {
        self.booted.as_ref().map(|_| self.caller.pid)
    }

    /// Whether the session has ended: its VM stopped, and nothing more is asked of it.
    pub fn ended(&self) -> bool {
        self.ended
    }

    /// The answers to *request*.
    pub fn handle(&mut self, request: Request) -> Vec<Answer> {
        let id = request.id();
        if self.ended {
            return vec![Answer::refused(Some(id), "the VM of this connection has stopped")];
        }
        match request {
            Request::Boot { id, data, kernel, initrd, rootfs, agent_disk, sessions, cpus, command_line, helper_pid } => {
                match self.boot(&data, [&kernel, &initrd, &rootfs, &agent_disk, &sessions], cpus, &command_line, helper_pid) {
                    Ok(runtime_id) => vec![Answer::Created { id, runtime_id }],
                    Err(answer) => vec![with_id(answer, id)],
                }
            }
            Request::Start { id } => match self.start() {
                Ok(()) => vec![Answer::Started { id }],
                Err(answer) => vec![with_id(answer, id)],
            },
            Request::Share { id, name, path, read_only } => match self.share(&name, &path, read_only) {
                Ok(()) => vec![Answer::Shared { id }],
                Err(answer) => vec![with_id(answer, id)],
            },
            Request::Unshare { id, name } => match self.unshare(&name) {
                Ok(()) => vec![Answer::Unshared { id }],
                Err(answer) => vec![with_id(answer, id)],
            },
            Request::Kill { id } => match self.booted.as_ref() {
                None => vec![Answer::refused(Some(id), "no VM runs on this connection")],
                Some(booted) => {
                    let why = booted.vm.terminate().err().map(|failure| failure.words);
                    self.end();
                    vec![Answer::Stopped { reason: Reason::Killed, why }]
                }
            },
        }
    }

    fn boot(&mut self, data: &str, [kernel, initrd, rootfs, agent_disk, sessions]: [&str; 5], cpus: u32, command_line: &str, helper: u32) -> Result<String, Answer> {
        let refuse = |why: &str| Answer::refused(None, why);
        if self.booted.is_some() {
            return Err(refuse("this connection holds a VM already"));
        }
        if helper != self.caller.pid {
            return Err(refuse("a helper that is not the caller's own process"));
        }
        let profile = self.files.profile();
        if !within(data, profile) || data.len() == profile.trim_end_matches('\\').len() {
            return Err(refuse("a data folder outside the caller's own profile"));
        }
        for (path, what) in [(kernel, "the kernel"), (initrd, "the initrd"), (rootfs, "the image"), (sessions, "the sessions disk")] {
            if !within(path, data) || path.len() == data.len() {
                return Err(refuse(&format!("{what} outside the caller's data folder")));
            }
        }
        let installed = match self.files.installed() {
            Some(folder) if within(agent_disk, folder) => {
                // The one file of the installed app's that a VM is given, as the service grants it by its own right.
                let own = format!(r"{}\{AGENT_DISK}", folder.trim_end_matches('\\'));
                if !agent_disk.eq_ignore_ascii_case(&own) {
                    return Err(refuse("a file of the installed app's that is not its agent disk"));
                }
                true
            }
            _ => false,
        };
        if !installed && (!within(agent_disk, data) || agent_disk.len() == data.len()) {
            return Err(refuse("the agent disk outside the caller's data folder and the installed app's"));
        }
        // Every path's chain of handles, held without delete sharing until HCS has made the VM (which opens the
        // disks and the kernel), so nothing on any path can be swapped between the check and HCS's open.
        let mut held = vec![self.files.check(data, Kind::Folder).map_err(|why| refuse(&format!("the data folder: {why}")))?];
        for (path, kind, what) in [(kernel, Kind::File, "the kernel"), (initrd, Kind::File, "the initrd"), (rootfs, Kind::File, "the image"), (agent_disk, Kind::File, "the agent disk"), (sessions, Kind::Written, "the sessions disk")] {
            held.push(self.files.check(path, kind).map_err(|why| refuse(&format!("{what}: {why}")))?);
        }
        if !self.roots.take(data) {
            return Err(refuse("a VM runs for this data folder already"));
        }
        let id = self.hypervisor.new_id();
        let machine = Machine {
            kernel: kernel.to_owned(),
            initrd: initrd.to_owned(),
            command_line: command_line.to_owned(),
            rootfs: rootfs.to_owned(),
            agent_disk: agent_disk.to_owned(),
            sessions: sessions.to_owned(),
            cpus,
            user_sid: self.caller.sid.clone(),
            console: self.hypervisor.console(&id),
        };
        let made = hcs::document(&machine).map_err(|why| refuse(&why)).and_then(|document| {
            // The console read before the VM is made, which HCS's worker connects COM1 to: the kernel's first lines are read.
            let console = self.hypervisor.listen(&machine.console).map_err(refused)?;
            // Every file the VM opens, granted to it alone: the caller's own by the caller's right, and the installed
            // app's agent disk, which the caller has just reached as itself, by the service's.
            let granted: Vec<Grant> = [(kernel, By::Caller), (initrd, By::Caller), (rootfs, By::Caller), (agent_disk, if installed { By::Service } else { By::Caller }), (sessions, By::Caller)]
                .into_iter()
                .map(|(path, by)| Grant { path: path.to_owned(), by })
                .collect();
            let (vm, runtime_id) = self.hypervisor.create(&id, &document, &granted).map_err(refused)?;
            Ok((vm, console, runtime_id))
        });
        let outcome = match made {
            Ok((vm, console, runtime_id)) => {
                self.booted = Some(Booted { vm, console, data: data.to_owned(), started: false, live: HashSet::new(), named: HashSet::new() });
                Ok(runtime_id)
            }
            Err(answer) => {
                self.roots.give_back(data);
                Err(answer)
            }
        };
        // HCS has opened the disks and the kernel (or failed): the chain's handles can go.
        drop(held);
        outcome
    }

    fn start(&mut self) -> Result<(), Answer> {
        let booted = self.booted.as_mut().ok_or_else(|| Answer::refused(None, "no VM was made on this connection"))?;
        if booted.started {
            return Err(Answer::refused(None, "the VM has started already"));
        }
        if let Err(failure) = booted.vm.start() {
            self.end();
            return Err(refused(failure));
        }
        booted.started = true;
        Ok(())
    }

    fn share(&mut self, name: &str, path: &str, read_only: bool) -> Result<(), Answer> {
        let booted = self.booted.as_mut().filter(|booted| booted.started).ok_or_else(|| Answer::refused(None, "the guest is not running"))?;
        if booted.named.contains(name) {
            return Err(Answer::refused(None, "a share's name given before in this VM"));
        }
        if booted.live.len() >= MAX_SHARES {
            return Err(Answer::refused(None, format!("{LIMIT}: {MAX_SHARES} folders are shared already")));
        }
        // The folder's chain held until HCS has added the share (which opens the folder), so it cannot be swapped.
        let held = self.files.check(path, Kind::Folder).map_err(|why| Answer::refused(None, format!("the folder: {why}")))?;
        booted.named.insert(name.to_owned());
        let shared = booted.vm.share(name, path, read_only).map_err(refused);
        drop(held);
        shared?;
        booted.live.insert(name.to_owned());
        Ok(())
    }

    fn unshare(&mut self, name: &str) -> Result<(), Answer> {
        let booted = self.booted.as_mut().ok_or_else(|| Answer::refused(None, "the guest is not running"))?;
        if !booted.live.contains(name) {
            return Err(Answer::refused(None, "no share of that name is in the guest"));
        }
        booted.vm.unshare(name).map_err(refused)?;
        booted.live.remove(name);
        Ok(())
    }

    /// What the VM's exit, with HCS's *status*, tells the caller; the session ends.
    pub fn exited(&mut self, status: i32) -> Option<Answer> {
        if self.ended || self.booted.is_none() {
            return None;
        }
        self.end();
        Some(if status >= 0 {
            Answer::Stopped { reason: Reason::Guest, why: None }
        } else {
            Answer::Stopped { reason: Reason::Error, why: Some(Failure::of("the VM", status, None).words) }
        })
    }

    /// The session's end, however it came: the connection gone, the helper gone, a kill or an exit. The VM's handle
    /// closes, which ends it, then its console's read, and its data folder is free for another.
    pub fn end(&mut self) {
        self.ended = true;
        if let Some(booted) = self.booted.take() {
            self.roots.give_back(&booted.data);
            drop(booted.vm);
            drop(booted.console);
        }
    }
}

impl<H: Hypervisor, F: Files> Drop for Session<'_, H, F> {
    fn drop(&mut self) {
        self.end();
    }
}

fn with_id(answer: Answer, id: u32) -> Answer {
    match answer {
        Answer::Refused { why, code, .. } => Answer::Refused { id: Some(id), why, code },
        other => other,
    }
}

fn refused(failure: Failure) -> Answer {
    Answer::Refused { id: None, why: failure.words, code: Some(failure.code) }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    use std::rc::Rc;

    #[derive(Default)]
    struct Log(RefCell<Vec<String>>);
    impl Log {
        fn said(&self, what: impl Into<String>) {
            self.0.borrow_mut().push(what.into());
        }
        fn all(&self) -> Vec<String> {
            self.0.borrow().clone()
        }
    }

    struct FakeVm {
        log: Rc<Log>,
        fail: Option<&'static str>,
    }
    impl Vm for FakeVm {
        fn start(&self) -> Result<(), Failure> {
            self.log.said("start");
            match self.fail {
                Some("start") => Err(Failure::of("HcsStartComputeSystem", hcs::failure::HYPERV_NOT_INSTALLED, None)),
                _ => Ok(()),
            }
        }
        fn share(&self, name: &str, path: &str, read_only: bool) -> Result<(), Failure> {
            self.log.said(format!("share {name} {path} {read_only}"));
            Ok(())
        }
        fn unshare(&self, name: &str) -> Result<(), Failure> {
            self.log.said(format!("unshare {name}"));
            Ok(())
        }
        fn terminate(&self) -> Result<(), Failure> {
            self.log.said("terminate");
            Ok(())
        }
    }
    impl Drop for FakeVm {
        fn drop(&mut self) {
            self.log.said("closed");
        }
    }

    struct FakeConsole {
        log: Rc<Log>,
    }
    impl Drop for FakeConsole {
        fn drop(&mut self) {
            self.log.said("console closed");
        }
    }

    struct FakeHcs {
        log: Rc<Log>,
        fail: Option<&'static str>,
        made: RefCell<u32>,
    }
    impl Hypervisor for FakeHcs {
        type Vm = FakeVm;
        type Console = FakeConsole;
        fn new_id(&self) -> String {
            *self.made.borrow_mut() += 1;
            format!("00000000-0000-0000-0000-{:012}", self.made.borrow())
        }
        fn console(&self, id: &str) -> String {
            format!(r"\\.\pipe\surogate-console-{id}")
        }
        fn listen(&self, console: &str) -> Result<FakeConsole, Failure> {
            self.log.said(format!("console {console}"));
            if self.fail == Some("console") {
                return Err(Failure { code: -1, words: "the console's pipe could not be made: Access is denied. (os error 5)".into() });
            }
            Ok(FakeConsole { log: self.log.clone() })
        }
        fn create(&self, id: &str, document: &str, granted: &[Grant]) -> Result<(FakeVm, String), Failure> {
            self.log.said(format!("create {id} granting {}", granted.iter().map(|grant| format!("{} by the {:?}", grant.path, grant.by)).collect::<Vec<_>>().join(", ")));
            self.log.said(document);
            if self.fail == Some("create") {
                return Err(Failure::of("HcsCreateComputeSystem", hcs::failure::ACCESS_DENIED, None));
            }
            Ok((FakeVm { log: self.log.clone(), fail: self.fail }, id.to_owned()))
        }
    }

    // The caller's disk: everything there and reachable, but what *unreachable* names.
    struct FakeFiles {
        unreachable: Vec<(&'static str, &'static str)>,
        log: Rc<Log>,
    }
    impl Files for FakeFiles {
        fn profile(&self) -> &str {
            r"C:\Users\ana"
        }
        fn installed(&self) -> Option<&str> {
            Some(r"C:\Program Files\Surogate")
        }
        fn check(&self, path: &str, kind: Kind) -> Result<Checked, String> {
            self.log.said(format!("check {path} {kind:?}"));
            match self.unreachable.iter().find(|(end, _)| path.ends_with(end)) {
                Some((_, why)) => Err((*why).to_owned()),
                None => Ok(Checked::none()),
            }
        }
    }

    const DATA: &str = r"C:\Users\ana\AppData\Local\Surogate";
    fn boot(id: u32) -> Request {
        let at = |name: &str| format!(r"{DATA}\vm\{name}");
        Request::Boot {
            id,
            data: DATA.into(),
            kernel: at(r"image\vmlinuz"),
            initrd: at(r"image\initrd"),
            rootfs: at(r"image\rootfs.vhd"),
            agent_disk: r"C:\Program Files\Surogate\resources\vm\agent.vhd".into(),
            sessions: at("sessions.vhdx"),
            cpus: 2,
            command_line: "console=ttyS0".into(),
            helper_pid: 4242,
        }
    }
    const ANA: fn() -> Caller = || Caller { sid: "S-1-5-21-1-2-3-1001".into(), pid: 4242 };

    struct World {
        log: Rc<Log>,
        hcs: FakeHcs,
        files: FakeFiles,
        roots: Roots,
    }
    fn world(fail: Option<&'static str>, unreachable: Vec<(&'static str, &'static str)>) -> World {
        let log = Rc::new(Log::default());
        World { hcs: FakeHcs { log: log.clone(), fail, made: RefCell::new(0) }, files: FakeFiles { unreachable, log: log.clone() }, roots: Roots::default(), log }
    }
    fn why(answers: &[Answer]) -> String {
        match answers {
            [Answer::Refused { why, .. }] => why.clone(),
            other => panic!("not refused: {other:?}"),
        }
    }

    #[test]
    fn makes_the_vm_from_its_own_document_checking_each_file_as_the_caller_then_starts_shares_and_kills_it() {
        let w = world(None, vec![]);
        let mut session = Session::new(&w.hcs, &w.files, &w.roots, ANA());
        assert_eq!(session.handle(boot(1)), [Answer::Created { id: 1, runtime_id: "00000000-0000-0000-0000-000000000001".into() }]);
        let log = w.log.all();
        assert_eq!(
            log[..6],
            [
                format!(r"check {DATA} Folder"),
                format!(r"check {DATA}\vm\image\vmlinuz File"),
                format!(r"check {DATA}\vm\image\initrd File"),
                format!(r"check {DATA}\vm\image\rootfs.vhd File"),
                r"check C:\Program Files\Surogate\resources\vm\agent.vhd File".to_owned(),
                format!(r"check {DATA}\vm\sessions.vhdx Written"),
            ]
        );
        // The console is read before the VM is made, so the kernel's first lines are.
        assert_eq!(log[6], r"console \\.\pipe\surogate-console-00000000-0000-0000-0000-000000000001");
        // Each file granted to the VM alone: the caller's own by the caller's right; the installed app's agent disk, whose
        // ACL only administrators may change, by the service's, once the caller has reached it as itself.
        assert_eq!(
            log[7],
            format!(
                r"create 00000000-0000-0000-0000-000000000001 granting {DATA}\vm\image\vmlinuz by the Caller, {DATA}\vm\image\initrd by the Caller, {DATA}\vm\image\rootfs.vhd by the Caller, C:\Program Files\Surogate\resources\vm\agent.vhd by the Service, {DATA}\vm\sessions.vhdx by the Caller"
            )
        );
        // The document is the service's own, with the caller's SID and the console's pipe.
        assert!(log[8].contains(r#""DefaultBindSecurityDescriptor":"D:P(A;;FA;;;SY)(A;;FA;;;S-1-5-21-1-2-3-1001)""#), "{}", log[8]);
        assert!(log[8].contains(r#""NamedPipe":"\\\\.\\pipe\\surogate-console-00000000-0000-0000-0000-000000000001""#), "{}", log[8]);
        assert_eq!(session.handle(Request::Start { id: 2 }), [Answer::Started { id: 2 }]);
        assert_eq!(session.helper(), Some(4242));
        assert_eq!(session.handle(Request::Share { id: 3, name: "r1".into(), path: r"C:\Users\ana\Documents\shop".into(), read_only: false }), [Answer::Shared { id: 3 }]);
        assert_eq!(session.handle(Request::Unshare { id: 4, name: "r1".into() }), [Answer::Unshared { id: 4 }]);
        assert_eq!(session.handle(Request::Kill { id: 5 }), [Answer::Stopped { reason: Reason::Killed, why: None }]);
        // The kill terminates the VM, its handle is closed, and then its console.
        assert_eq!(
            w.log.all()[w.log.all().len() - 7..],
            ["start", r"check C:\Users\ana\Documents\shop Folder", r"share r1 C:\Users\ana\Documents\shop false", "unshare r1", "terminate", "closed", "console closed"]
        );
        assert!(session.ended());
        assert_eq!(why(&session.handle(Request::Start { id: 6 })), "the VM of this connection has stopped");
    }

    #[test]
    fn grants_an_agent_disk_in_the_data_folder_as_the_caller() {
        let w = world(None, vec![]);
        let mut session = Session::new(&w.hcs, &w.files, &w.roots, ANA());
        let Request::Boot { data, kernel, initrd, rootfs, sessions, cpus, command_line, helper_pid, .. } = boot(1) else { unreachable!() };
        let agent_disk = format!(r"{DATA}\vm\agent.vhd");
        assert!(matches!(session.handle(Request::Boot { id: 1, data, kernel, initrd, rootfs, agent_disk: agent_disk.clone(), sessions, cpus, command_line, helper_pid })[..], [Answer::Created { .. }]));
        assert!(w.log.all().iter().any(|line| line.starts_with("create ") && line.contains(&format!("{agent_disk} by the Caller,"))));
    }

    #[test]
    fn takes_no_file_of_the_installed_app_s_but_its_agent_disk() {
        let w = world(None, vec![]);
        let with = |agent_disk: &str| {
            let Request::Boot { data, kernel, initrd, rootfs, sessions, cpus, command_line, helper_pid, .. } = boot(1) else { unreachable!() };
            Session::new(&w.hcs, &w.files, &w.roots, ANA()).handle(Request::Boot { id: 1, data, kernel, initrd, rootfs, agent_disk: agent_disk.into(), sessions, cpus, command_line, helper_pid })
        };
        for other in [r"C:\Program Files\Surogate\service\surogate-service.exe", r"C:\Program Files\Surogate\resources\vm\other.vhd", r"C:\Program Files\Surogate\agent.vhd"] {
            assert_eq!(why(&with(other)), "a file of the installed app's that is not its agent disk", "{other}");
        }
        assert!(!w.log.all().iter().any(|line| line.starts_with("check") || line.starts_with("create")), "{:?}", w.log.all());
        // Its own name, ASCII case aside, as NTFS takes it.
        assert!(matches!(with(r"C:\Program Files\Surogate\Resources\VM\Agent.vhd")[..], [Answer::Created { .. }]));
        assert!(w.log.all().iter().any(|line| line.contains(r"C:\Program Files\Surogate\Resources\VM\Agent.vhd by the Service,")), "{:?}", w.log.all());
    }

    #[test]
    fn refuses_a_helper_that_is_not_the_caller_s_own_process() {
        let w = world(None, vec![]);
        let mut session = Session::new(&w.hcs, &w.files, &w.roots, Caller { pid: 7, ..ANA() });
        assert_eq!(session.handle(boot(1)), [Answer::refused(Some(1), "a helper that is not the caller's own process")]);
        assert!(w.log.all().is_empty());
    }

    #[test]
    fn refuses_a_data_folder_outside_the_caller_s_profile_and_a_file_outside_the_data_folder() {
        let w = world(None, vec![]);
        let mut session = Session::new(&w.hcs, &w.files, &w.roots, ANA());
        let refused = |change: &dyn Fn(&mut Request)| {
            let mut request = boot(1);
            change(&mut request);
            why(&Session::new(&w.hcs, &w.files, &w.roots, ANA()).handle(request))
        };
        assert_eq!(refused(&|r| if let Request::Boot { data, .. } = r { *data = r"C:\Users\bob\AppData\Local\Surogate".into() }), "a data folder outside the caller's own profile");
        assert_eq!(refused(&|r| if let Request::Boot { data, .. } = r { *data = r"C:\Users\ana".into() }), "a data folder outside the caller's own profile");
        assert_eq!(refused(&|r| if let Request::Boot { data, .. } = r { *data = r"C:\Users\anabel\x".into() }), "a data folder outside the caller's own profile");
        assert_eq!(refused(&|r| if let Request::Boot { kernel, .. } = r { *kernel = r"C:\Windows\System32\drivers\etc\hosts".into() }), "the kernel outside the caller's data folder");
        assert_eq!(refused(&|r| if let Request::Boot { rootfs, .. } = r { *rootfs = r"C:\Users\ana\AppData\Local\SurogateX\rootfs.vhd".into() }), "the image outside the caller's data folder");
        assert_eq!(refused(&|r| if let Request::Boot { sessions, .. } = r { *sessions = DATA.into() }), "the sessions disk outside the caller's data folder");
        assert_eq!(
            refused(&|r| if let Request::Boot { agent_disk, .. } = r { *agent_disk = r"C:\Program Files\Other\agent.vhd".into() }),
            "the agent disk outside the caller's data folder and the installed app's"
        );
        assert!(w.log.all().is_empty());
        // Nothing was taken: a boot after them makes its VM.
        assert!(matches!(session.handle(boot(2))[..], [Answer::Created { id: 2, .. }]));
    }

    #[test]
    fn refuses_a_file_the_caller_cannot_reach_as_itself_and_makes_nothing() {
        for (end, what) in [(r"Surogate", "the data folder"), (r"vmlinuz", "the kernel"), (r"rootfs.vhd", "the image"), (r"agent.vhd", "the agent disk"), (r"sessions.vhdx", "the sessions disk")] {
            let w = world(None, vec![(end, "the caller may not read it")]);
            let answers = Session::new(&w.hcs, &w.files, &w.roots, ANA()).handle(boot(1));
            assert_eq!(why(&answers), format!("{what}: the caller may not read it"));
            assert!(!w.log.all().iter().any(|line| line.starts_with("create")), "{what}");
        }
        let w = world(None, vec![(r"Documents\secret", "a path that passes through a reparse point")]);
        let mut session = Session::new(&w.hcs, &w.files, &w.roots, ANA());
        session.handle(boot(1));
        session.handle(Request::Start { id: 2 });
        let answers = session.handle(Request::Share { id: 3, name: "r1".into(), path: r"C:\Users\ana\Documents\secret".into(), read_only: false });
        assert_eq!(answers, [Answer::refused(Some(3), "the folder: a path that passes through a reparse point")]);
        assert!(!w.log.all().iter().any(|line| line.starts_with("share")));
    }

    #[test]
    fn refuses_a_second_vm_for_one_data_folder_and_frees_it_when_the_first_ends() {
        let w = world(None, vec![]);
        let mut first = Session::new(&w.hcs, &w.files, &w.roots, ANA());
        assert!(matches!(first.handle(boot(1))[..], [Answer::Created { .. }]));
        assert_eq!(why(&first.handle(boot(2))), "this connection holds a VM already");
        let mut second = Session::new(&w.hcs, &w.files, &w.roots, ANA());
        assert_eq!(why(&second.handle(boot(1))), "a VM runs for this data folder already");
        let mut other_case = Session::new(&w.hcs, &w.files, &w.roots, ANA());
        let Request::Boot { kernel, initrd, rootfs, agent_disk, sessions, cpus, command_line, helper_pid, .. } = boot(1) else { unreachable!() };
        let upper = Request::Boot { id: 1, data: DATA.to_uppercase().replace(r"C:\USERS\ANA", r"C:\Users\ana"), kernel, initrd, rootfs, agent_disk, sessions, cpus, command_line, helper_pid };
        assert_eq!(why(&other_case.handle(upper)), "a VM runs for this data folder already");
        drop(first);
        assert!(matches!(second.handle(boot(3))[..], [Answer::Created { .. }]));
    }

    #[test]
    fn says_hcs_s_own_words_and_code_when_it_refuses_and_frees_the_data_folder() {
        let w = world(Some("create"), vec![]);
        let answers = Session::new(&w.hcs, &w.files, &w.roots, ANA()).handle(boot(1));
        assert_eq!(answers, [Answer::Refused { id: Some(1), why: "HcsCreateComputeSystem failed (HRESULT 0x8037011B)".into(), code: Some(hcs::failure::ACCESS_DENIED) }]);
        let w = world(Some("start"), vec![]);
        let mut session = Session::new(&w.hcs, &w.files, &w.roots, ANA());
        session.handle(boot(1));
        assert_eq!(session.handle(Request::Start { id: 2 }), [Answer::Refused { id: Some(2), why: "HcsStartComputeSystem failed (HRESULT 0x80370102)".into(), code: Some(hcs::failure::HYPERV_NOT_INSTALLED) }]);
        assert!(session.ended());
        assert!(matches!(Session::new(&w.hcs, &w.files, &w.roots, ANA()).handle(boot(1))[..], [Answer::Created { .. }]));
    }

    #[test]
    fn shares_eight_folders_at_most_never_gives_a_name_twice_and_shares_nothing_before_the_start() {
        let w = world(None, vec![]);
        let mut session = Session::new(&w.hcs, &w.files, &w.roots, ANA());
        let share = |n: u32| Request::Share { id: 10 + n, name: format!("r{n}"), path: r"C:\Users\ana\Documents\shop".into(), read_only: n == 8 };
        session.handle(boot(1));
        assert_eq!(why(&session.handle(share(1))), "the guest is not running");
        session.handle(Request::Start { id: 2 });
        for n in 1..=8 {
            assert_eq!(session.handle(share(n)), [Answer::Shared { id: 10 + n }]);
        }
        assert_eq!(why(&session.handle(share(9))), "the service's limit: 8 folders are shared already");
        assert_eq!(session.handle(Request::Unshare { id: 30, name: "r1".into() }), [Answer::Unshared { id: 30 }]);
        assert_eq!(why(&session.handle(share(1))), "a share's name given before in this VM");
        assert_eq!(session.handle(share(9)), [Answer::Shared { id: 19 }]);
        assert_eq!(why(&session.handle(Request::Unshare { id: 31, name: "r1".into() })), "no share of that name is in the guest");
        assert!(w.log.all().contains(&r"share r8 C:\Users\ana\Documents\shop true".to_owned()));
    }

    #[test]
    fn tells_the_vm_s_exit_once_by_hcs_s_status_and_ends() {
        let w = world(None, vec![]);
        let mut session = Session::new(&w.hcs, &w.files, &w.roots, ANA());
        assert_eq!(session.exited(0), None);
        session.handle(boot(1));
        session.handle(Request::Start { id: 2 });
        assert_eq!(session.exited(0), Some(Answer::Stopped { reason: Reason::Guest, why: None }));
        assert_eq!(session.exited(0), None);
        let mut failed = Session::new(&w.hcs, &w.files, &w.roots, ANA());
        failed.handle(boot(1));
        assert_eq!(failed.exited(0x8037_0106_u32 as i32), Some(Answer::Stopped { reason: Reason::Error, why: Some("the VM failed (HRESULT 0x80370106)".into()) }));
    }

    #[test]
    fn ends_the_vm_when_the_connection_goes() {
        let w = world(None, vec![]);
        {
            let mut session = Session::new(&w.hcs, &w.files, &w.roots, ANA());
            session.handle(boot(1));
            session.handle(Request::Start { id: 2 });
        }
        assert_eq!(w.log.all()[w.log.all().len() - 2..], ["closed", "console closed"]);
        assert!(matches!(Session::new(&w.hcs, &w.files, &w.roots, ANA()).handle(boot(1))[..], [Answer::Created { .. }]));
    }

    #[test]
    fn reads_the_console_from_before_the_vm_is_made_until_after_its_handle_has_closed() {
        let w = world(None, vec![]);
        let mut session = Session::new(&w.hcs, &w.files, &w.roots, ANA());
        session.handle(boot(1));
        session.handle(Request::Start { id: 2 });
        let log = w.log.all();
        let at = |what: &str| log.iter().position(|line| line.starts_with(what)).unwrap_or_else(|| panic!("no {what} in {log:?}"));
        assert!(at("console ") < at("create ") && at("create ") < at("start"), "{log:?}");
        assert!(!log.contains(&"console closed".to_owned()), "{log:?}");
        session.end();
        assert_eq!(w.log.all()[w.log.all().len() - 2..], ["closed", "console closed"]);
    }

    #[test]
    fn refuses_a_boot_whose_console_cannot_be_read_makes_no_vm_and_frees_the_data_folder() {
        let w = world(Some("console"), vec![]);
        for _ in 0..2 {
            let answers = Session::new(&w.hcs, &w.files, &w.roots, ANA()).handle(boot(1));
            assert_eq!(answers, [Answer::Refused { id: Some(1), why: "the console's pipe could not be made: Access is denied. (os error 5)".into(), code: Some(-1) }]);
        }
        assert!(!w.log.all().iter().any(|line| line.starts_with("create")), "{:?}", w.log.all());
    }
}
