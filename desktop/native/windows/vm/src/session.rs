// What surogate-vm.exe decides, one VM's life, as plain data: it is told the VM manager's lines, the service's answers
// and the ends of its stdin, its parent and the service's pipe, and answers each with what to do. run.rs does each
// thing on Windows and tells the session how it went; nothing here touches Windows, so the whole order is tested on any
// OS. The order (pipe/src/protocol.rs): boot, then the VM's runtime ID, then the guest's ports bound on it, then
// start, so the guest finds its ports when it dials them; shares only while the VM runs; and the VM ended at a kill,
// at the end of stdin or of the parent, and on any answer that leaves it half made.

use std::collections::HashMap;

use hcs::properties::is_guid;
use pipe::{Answer, Reason, Request};

use crate::{Said, Told};

/// What the session is told.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Input {
    /// A line of the VM manager's, or why it was not one.
    Told(Result<Told, Said>),
    /// The VM manager's stdin ended: the app has gone.
    StdinEnd,
    /// The process that started surogate-vm.exe has gone.
    ParentGone,
    /// What the boot's own work came to: the sessions disk, the console log and the service's pipe.
    Prepared(Result<(), String>),
    /// What binding the guest's ports on the VM's runtime ID came to.
    Bound(Result<(), String>),
    /// An answer of the service's.
    Service(Answer),
    /// The service's pipe ended, or could not be written to.
    ServiceEnd(String),
    /// A stop asked of the service was not answered in time.
    StopLate,
}

/// What run.rs is to do.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Act {
    /// A line to the VM manager.
    Say(Said),
    /// The boot's own work before the service is asked: make the sessions disk if it is not there, open the console
    /// log, and connect to the service.
    Prepare { sessions: String, console: String },
    /// A request to the service.
    Ask(Request),
    /// The guest's three ports bound on the VM's runtime ID, each relayed to its pipe.
    Bind(String),
    /// Text the guest's console printed, for its log.
    Console(String),
    /// Wait this long for the service's stop, then StopLate.
    AwaitStop,
    /// surogate-vm.exe ends, with this code.
    Exit(i32),
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum Phase {
    /// No boot asked yet.
    Waiting,
    /// The boot's own work under way; the boot to ask once it is done.
    Preparing(Box<Request>),
    /// Boot asked of the service, by its id.
    Creating(u32),
    /// The VM made; its ports being bound.
    Binding,
    /// Start asked, by its id.
    Starting(u32),
    Running,
    /// A stop asked of the service; the code to exit with, and whether the VM manager is told it stopped.
    Stopping { code: i32, said: Option<Said> },
    Done,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum Pending {
    Share { told: u32, name: String },
    Unshare { told: u32 },
}

/// One VM's session: the data folder it boots for and this process's id, which the service ties the VM's life to.
#[derive(Debug)]
pub struct Session {
    data: String,
    pid: u32,
    phase: Phase,
    next: u32,
    pending: HashMap<u32, Pending>,
}

const NOT_RUNNING: &str = "the VM is not running";
const BOOTED: &str = "the VM was booted already";

fn reason(reason: Reason) -> &'static str {
    match reason {
        Reason::Guest => "guest",
        Reason::Killed => "killed",
        Reason::Error => "error",
    }
}

impl Session {
    pub fn new(data: String, pid: u32) -> Session {
        Session { data, pid, phase: Phase::Waiting, next: 0, pending: HashMap::new() }
    }

    fn id(&mut self) -> u32 {
        self.next += 1;
        self.next
    }

    // Whether the service has been asked for the VM, so a stop must go through it.
    fn asked(&self) -> bool {
        matches!(self.phase, Phase::Creating(_) | Phase::Binding | Phase::Starting(_) | Phase::Running)
    }

    // The VM ended: through the service where it was asked for it, else at once. *said*, the line the VM manager is
    // told once it has stopped.
    fn stop(&mut self, code: i32, said: Option<Said>) -> Vec<Act> {
        if matches!(self.phase, Phase::Stopping { .. } | Phase::Done) {
            return vec![];
        }
        if self.asked() {
            let id = self.id();
            self.phase = Phase::Stopping { code, said };
            return vec![Act::Ask(Request::Kill { id }), Act::AwaitStop];
        }
        self.phase = Phase::Done;
        said.into_iter().map(Act::Say).chain([Act::Exit(code)]).collect()
    }

    // A boot that went wrong: the VM manager told why, and the VM ended if it was made.
    fn failed(&mut self, why: String) -> Vec<Act> {
        let made = matches!(self.phase, Phase::Binding | Phase::Starting(_));
        let mut acts = vec![Act::Say(Said::Failed { why })];
        if made {
            acts.extend(self.stop(1, None));
        } else {
            self.phase = Phase::Done;
            acts.push(Act::Exit(1));
        }
        acts
    }

    // An answer that leaves the VM half made, or one out of turn: said, and the VM ended.
    fn broken(&mut self, why: String) -> Vec<Act> {
        match self.phase {
            Phase::Running => self.stop(1, Some(Said::Stopped { reason: "error", why: Some(why) })),
            Phase::Stopping { .. } | Phase::Done => vec![],
            _ => self.failed(why),
        }
    }

    /// What to do on *input*.
    pub fn on(&mut self, input: Input) -> Vec<Act> {
        if self.phase == Phase::Done {
            return vec![];
        }
        match input {
            Input::Told(Err(refused)) => vec![Act::Say(refused)],
            Input::Told(Ok(told)) => self.told(told),
            Input::StdinEnd | Input::ParentGone => self.stop(0, None),
            Input::Prepared(done) => match (&self.phase, done) {
                (Phase::Preparing(_), Ok(())) => {
                    let Phase::Preparing(boot) = std::mem::replace(&mut self.phase, Phase::Waiting) else { unreachable!() };
                    let id = boot.id();
                    self.phase = Phase::Creating(id);
                    vec![Act::Ask(*boot)]
                }
                (Phase::Preparing(_), Err(why)) => self.failed(why),
                _ => vec![],
            },
            Input::Bound(done) => match (&self.phase, done) {
                (Phase::Binding, Ok(())) => {
                    let id = self.id();
                    self.phase = Phase::Starting(id);
                    vec![Act::Ask(Request::Start { id })]
                }
                (Phase::Binding, Err(why)) => self.failed(why),
                _ => vec![],
            },
            Input::Service(answer) => self.answered(answer),
            Input::ServiceEnd(why) => match &self.phase {
                Phase::Stopping { code, said } => {
                    let (code, said) = (*code, said.clone());
                    self.phase = Phase::Done;
                    said.into_iter().map(Act::Say).chain([Act::Exit(code)]).collect()
                }
                Phase::Running => {
                    self.phase = Phase::Done;
                    vec![Act::Say(Said::Stopped { reason: "error", why: Some(why) }), Act::Exit(1)]
                }
                _ => {
                    self.phase = Phase::Done;
                    vec![Act::Say(Said::Failed { why }), Act::Exit(1)]
                }
            },
            Input::StopLate => match &self.phase {
                Phase::Stopping { code, said } => {
                    let (code, said) = (*code, said.clone());
                    self.phase = Phase::Done;
                    said.into_iter().map(Act::Say).chain([Act::Exit(code)]).collect()
                }
                _ => vec![],
            },
        }
    }

    fn told(&mut self, told: Told) -> Vec<Act> {
        match told {
            Told::Boot { kernel, initrd, rootfs, agent_disk, sessions, console, cpus, command_line } => {
                if self.phase != Phase::Waiting {
                    return vec![Act::Say(Said::Refused { id: None, why: BOOTED.into() })];
                }
                let id = self.id();
                let boot = Request::Boot { id, data: self.data.clone(), kernel, initrd, rootfs, agent_disk, sessions: sessions.clone(), cpus, command_line, helper_pid: self.pid };
                self.phase = Phase::Preparing(Box::new(boot));
                vec![Act::Prepare { sessions, console }]
            }
            Told::Share { id: told, name, path, read_only } => {
                if self.phase != Phase::Running {
                    return vec![Act::Say(Said::Refused { id: Some(told), why: NOT_RUNNING.into() })];
                }
                let id = self.id();
                self.pending.insert(id, Pending::Share { told, name: name.clone() });
                vec![Act::Ask(Request::Share { id, name, path, read_only })]
            }
            Told::Unshare { id: told, name } => {
                if self.phase != Phase::Running {
                    return vec![Act::Say(Said::Refused { id: Some(told), why: NOT_RUNNING.into() })];
                }
                let id = self.id();
                self.pending.insert(id, Pending::Unshare { told });
                vec![Act::Ask(Request::Unshare { id, name })]
            }
            Told::Kill {} => self.stop(0, Some(Said::Stopped { reason: "killed", why: None })),
        }
    }

    fn answered(&mut self, answer: Answer) -> Vec<Act> {
        match answer {
            Answer::Console { text } => vec![Act::Console(text)],
            Answer::Stopped { reason: why_stopped, why } => match &self.phase {
                Phase::Running => {
                    self.phase = Phase::Done;
                    vec![Act::Say(Said::Stopped { reason: reason(why_stopped), why }), Act::Exit(0)]
                }
                Phase::Stopping { code, said } => {
                    let (code, said) = (*code, said.clone());
                    self.phase = Phase::Done;
                    said.into_iter().map(Act::Say).chain([Act::Exit(code)]).collect()
                }
                _ => {
                    self.phase = Phase::Done;
                    vec![Act::Say(Said::Failed { why: why.unwrap_or_else(|| "the VM stopped before it started".into()) }), Act::Exit(1)]
                }
            },
            Answer::Created { id, runtime_id } => match self.phase {
                Phase::Creating(boot) if boot == id => {
                    if !is_guid(&runtime_id) {
                        self.phase = Phase::Binding;
                        return self.failed("the Surogate VM service named the VM by no runtime ID".into());
                    }
                    self.phase = Phase::Binding;
                    vec![Act::Bind(runtime_id)]
                }
                _ => self.broken("the Surogate VM service answered out of turn".into()),
            },
            Answer::Started { id } => match self.phase {
                Phase::Starting(start) if start == id => {
                    self.phase = Phase::Running;
                    vec![Act::Say(Said::Started)]
                }
                _ => self.broken("the Surogate VM service answered out of turn".into()),
            },
            Answer::Shared { id } => match self.pending.remove(&id) {
                Some(Pending::Share { told, name }) => vec![Act::Say(Said::Shared { id: told, tag: name })],
                _ => self.broken("the Surogate VM service answered out of turn".into()),
            },
            Answer::Unshared { id } => match self.pending.remove(&id) {
                Some(Pending::Unshare { told }) => vec![Act::Say(Said::Unshared { id: told })],
                _ => self.broken("the Surogate VM service answered out of turn".into()),
            },
            Answer::Refused { id, why, .. } => {
                if let Some(pending) = id.and_then(|id| self.pending.remove(&id)) {
                    let told = match pending {
                        Pending::Share { told, .. } | Pending::Unshare { told } => told,
                    };
                    return vec![Act::Say(Said::Refused { id: Some(told), why })];
                }
                match self.phase {
                    // A stop refused: the VM is ended by the service when this process exits.
                    Phase::Stopping { .. } => self.on(Input::StopLate),
                    _ => self.broken(why),
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const DATA: &str = r"C:\Users\ana\AppData\Local\Surogate";
    const GUID: &str = "6b3c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f";

    fn boot() -> Told {
        Told::Boot {
            kernel: format!(r"{DATA}\vm\image\vmlinuz"),
            initrd: format!(r"{DATA}\vm\image\initrd"),
            rootfs: format!(r"{DATA}\vm\image\rootfs.vhd"),
            agent_disk: r"C:\Program Files\Surogate\resources\vm\agent.vhd".into(),
            sessions: format!(r"{DATA}\vm\sessions.vhdx"),
            console: format!(r"{DATA}\vm\console.log"),
            cpus: 4,
            command_line: "console=ttyS0 quiet".into(),
        }
    }

    // A session through its start: the boot asked with id 1, the start with id 2.
    fn running() -> Session {
        let mut session = Session::new(DATA.into(), 4242);
        session.on(Input::Told(Ok(boot())));
        session.on(Input::Prepared(Ok(())));
        session.on(Input::Service(Answer::Created { id: 1, runtime_id: GUID.into() }));
        session.on(Input::Bound(Ok(())));
        assert_eq!(session.on(Input::Service(Answer::Started { id: 2 })), vec![Act::Say(Said::Started)]);
        session
    }

    #[test]
    fn boots_in_order_its_own_work_then_the_vm_made_then_its_ports_bound_then_its_start() {
        let mut session = Session::new(DATA.into(), 4242);
        assert_eq!(session.on(Input::Told(Ok(boot()))), vec![Act::Prepare { sessions: format!(r"{DATA}\vm\sessions.vhdx"), console: format!(r"{DATA}\vm\console.log") }]);
        let asked = session.on(Input::Prepared(Ok(())));
        assert!(matches!(&asked[..], [Act::Ask(Request::Boot { id: 1, data, helper_pid: 4242, cpus: 4, .. })] if data == DATA), "{asked:?}");
        assert_eq!(session.on(Input::Service(Answer::Created { id: 1, runtime_id: GUID.into() })), vec![Act::Bind(GUID.into())]);
        assert_eq!(session.on(Input::Bound(Ok(()))), vec![Act::Ask(Request::Start { id: 2 })]);
        assert_eq!(session.on(Input::Service(Answer::Started { id: 2 })), vec![Act::Say(Said::Started)]);
    }

    #[test]
    fn shares_only_while_the_vm_runs_and_answers_each_by_the_vm_manager_s_id_and_tag() {
        let mut waiting = Session::new(DATA.into(), 1);
        assert_eq!(waiting.on(Input::Told(Ok(Told::Share { id: 7, name: "r1".into(), path: r"C:\x".into(), read_only: false }))), vec![Act::Say(Said::Refused { id: Some(7), why: NOT_RUNNING.into() })]);
        let mut session = running();
        assert_eq!(
            session.on(Input::Told(Ok(Told::Share { id: 7, name: "r1".into(), path: r"C:\Users\ana\Documents\shop".into(), read_only: true }))),
            vec![Act::Ask(Request::Share { id: 3, name: "r1".into(), path: r"C:\Users\ana\Documents\shop".into(), read_only: true })]
        );
        assert_eq!(session.on(Input::Service(Answer::Shared { id: 3 })), vec![Act::Say(Said::Shared { id: 7, tag: "r1".into() })]);
        assert_eq!(session.on(Input::Told(Ok(Told::Unshare { id: 8, name: "r1".into() }))), vec![Act::Ask(Request::Unshare { id: 4, name: "r1".into() })]);
        assert_eq!(session.on(Input::Service(Answer::Unshared { id: 4 })), vec![Act::Say(Said::Unshared { id: 8 })]);
        // A share the service refuses is refused to the VM manager by its own id; the VM runs on.
        session.on(Input::Told(Ok(Told::Share { id: 9, name: "r2".into(), path: r"C:\y".into(), read_only: false })));
        assert_eq!(session.on(Input::Service(Answer::refused(Some(5), "no"))), vec![Act::Say(Said::Refused { id: Some(9), why: "no".into() })]);
        assert_eq!(session.on(Input::Service(Answer::Console { text: "hello".into() })), vec![Act::Console("hello".into())]);
    }

    #[test]
    fn ends_the_vm_through_the_service_at_a_kill_and_says_it_stopped_once_it_has() {
        let mut session = running();
        assert_eq!(session.on(Input::Told(Ok(Told::Kill {}))), vec![Act::Ask(Request::Kill { id: 3 }), Act::AwaitStop]);
        // A second kill asks nothing more.
        assert_eq!(session.on(Input::Told(Ok(Told::Kill {}))), vec![]);
        assert_eq!(session.on(Input::Service(Answer::Stopped { reason: Reason::Killed, why: None })), vec![Act::Say(Said::Stopped { reason: "killed", why: None }), Act::Exit(0)]);
        assert_eq!(session.on(Input::StdinEnd), vec![]);
    }

    #[test]
    fn ends_the_vm_at_the_end_of_its_stdin_or_its_parent_and_exits_when_the_stop_is_late() {
        for end in [Input::StdinEnd, Input::ParentGone] {
            let mut session = running();
            assert_eq!(session.on(end), vec![Act::Ask(Request::Kill { id: 3 }), Act::AwaitStop]);
            assert_eq!(session.on(Input::StopLate), vec![Act::Exit(0)]);
        }
        // Before any boot there is no VM to end.
        assert_eq!(Session::new(DATA.into(), 1).on(Input::StdinEnd), vec![Act::Exit(0)]);
    }

    #[test]
    fn says_how_the_vm_stopped_by_itself() {
        let mut session = running();
        assert_eq!(
            session.on(Input::Service(Answer::Stopped { reason: Reason::Guest, why: None })),
            vec![Act::Say(Said::Stopped { reason: "guest", why: None }), Act::Exit(0)]
        );
        let mut session = running();
        assert_eq!(session.on(Input::ServiceEnd("the Surogate VM service closed its pipe".into())), vec![Act::Say(Said::Stopped { reason: "error", why: Some("the Surogate VM service closed its pipe".into()) }), Act::Exit(1)]);
    }

    #[test]
    fn fails_a_boot_whose_own_work_or_whose_vm_the_service_refuses_and_ends_a_vm_it_made() {
        let mut session = Session::new(DATA.into(), 1);
        session.on(Input::Told(Ok(boot())));
        assert_eq!(session.on(Input::Prepared(Err("the sessions disk could not be made".into()))), vec![Act::Say(Said::Failed { why: "the sessions disk could not be made".into() }), Act::Exit(1)]);

        let mut session = Session::new(DATA.into(), 1);
        session.on(Input::Told(Ok(boot())));
        session.on(Input::Prepared(Ok(())));
        assert_eq!(session.on(Input::Service(Answer::refused(Some(1), "HCS_E_HYPERV_NOT_INSTALLED"))), vec![Act::Say(Said::Failed { why: "HCS_E_HYPERV_NOT_INSTALLED".into() }), Act::Exit(1)]);

        // A VM made whose ports could not be bound is ended, not left half made.
        let mut session = Session::new(DATA.into(), 1);
        session.on(Input::Told(Ok(boot())));
        session.on(Input::Prepared(Ok(())));
        session.on(Input::Service(Answer::Created { id: 1, runtime_id: GUID.into() }));
        assert_eq!(session.on(Input::Bound(Err("the guest's net port could not be bound".into()))), vec![Act::Say(Said::Failed { why: "the guest's net port could not be bound".into() }), Act::Ask(Request::Kill { id: 2 }), Act::AwaitStop]);
        assert_eq!(session.on(Input::Service(Answer::Stopped { reason: Reason::Killed, why: None })), vec![Act::Exit(1)]);
    }

    #[test]
    fn refuses_a_runtime_id_that_is_not_a_guid_and_an_answer_out_of_turn() {
        let mut session = Session::new(DATA.into(), 1);
        session.on(Input::Told(Ok(boot())));
        session.on(Input::Prepared(Ok(())));
        let acts = session.on(Input::Service(Answer::Created { id: 1, runtime_id: "../../x".into() }));
        assert_eq!(acts[0], Act::Say(Said::Failed { why: "the Surogate VM service named the VM by no runtime ID".into() }));
        assert!(acts.contains(&Act::Ask(Request::Kill { id: 2 })), "{acts:?}");

        let mut session = running();
        let acts = session.on(Input::Service(Answer::Shared { id: 99 }));
        assert_eq!(acts, vec![Act::Ask(Request::Kill { id: 3 }), Act::AwaitStop]);
        assert_eq!(session.on(Input::StopLate), vec![Act::Say(Said::Stopped { reason: "error", why: Some("the Surogate VM service answered out of turn".into()) }), Act::Exit(1)]);
    }

    #[test]
    fn takes_one_boot_and_passes_on_a_line_it_could_not_read() {
        let mut session = running();
        assert_eq!(session.on(Input::Told(Ok(boot()))), vec![Act::Say(Said::Refused { id: None, why: BOOTED.into() })]);
        let refused = Said::Refused { id: Some(4), why: "a line that is not a JSON object of a type, its fields each of theirs".into() };
        assert_eq!(session.on(Input::Told(Err(refused.clone()))), vec![Act::Say(refused)]);
    }
}
