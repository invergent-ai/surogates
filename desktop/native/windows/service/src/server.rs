// The Surogate VM service's life (Microsoft Learn, "Service Programs": StartServiceCtrlDispatcherW, a ServiceMain
// that registers its control handler with RegisterServiceCtrlHandlerExW and says its state with SetServiceStatus).
// It serves \\.\pipe\surogate-vm, a connection a thread, and ends five minutes after its last VM, so an idle computer
// runs nothing of Surogate's; the service control manager starts it again for the next caller.
//
// The pipe: its first instance made the service's own (FILE_FLAG_FIRST_PIPE_INSTANCE), so a pipe of its name that
// someone made first stops the service instead of taking its callers; a descriptor that lets SYSTEM in, and
// interactive users read and write, which gives no one but SYSTEM the right to make another instance; and no client
// from another computer (PIPE_REJECT_REMOTE_CLIENTS).
//
// A connection's VM ends with the connection, with its helper's process, and with the service: each is the end of
// the session, which closes the VM's one handle (core.rs, Session::end).

use std::sync::atomic::{AtomicPtr, AtomicUsize, Ordering};
use std::sync::mpsc::{channel, Sender};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use pipe::win::{wide, Accepted, Handle, Pipe, Way};
use pipe::{Answer, Lines, Read, Request, SERVICE_PIPE};
use windows_sys::Win32::Foundation::{ERROR_CALL_NOT_IMPLEMENTED, NO_ERROR};
use windows_sys::Win32::System::Services::{
    RegisterServiceCtrlHandlerExW, SetServiceStatus, StartServiceCtrlDispatcherW, SERVICE_ACCEPT_STOP, SERVICE_CONTROL_INTERROGATE, SERVICE_CONTROL_STOP,
    SERVICE_RUNNING, SERVICE_STATUS, SERVICE_STATUS_CURRENT_STATE, SERVICE_STATUS_HANDLE, SERVICE_STOPPED, SERVICE_STOP_PENDING, SERVICE_TABLE_ENTRYW,
    SERVICE_WIN32_OWN_PROCESS,
};
use windows_sys::Win32::System::Threading::{CreateEventW, OpenProcess, SetEvent, WaitForSingleObject, INFINITE, PROCESS_SYNCHRONIZE};

use crate::caller::identify;
use crate::core::{Roots, Session};
use crate::files::{installed, CallerFiles};
use crate::hyper::{Hcs, Notice, Notify};

/// The service's name, as the installer registers it and surogate-vm.exe starts it.
pub const SERVICE_NAME: &str = "SurogateVM";
/// The pipe's descriptor: SYSTEM all access; interactive users exactly the rights a client needs -- read and write
/// the pipe, read its attributes and wait on it (0x00100083) -- and no more. Not FILE_APPEND_DATA (0x4), which for a
/// pipe is FILE_CREATE_PIPE_INSTANCE, so no one but SYSTEM may make another instance of it (pipe::win opens a client
/// with exactly these rights, never the generic ones the mapping would widen to include FILE_APPEND_DATA). No one else.
pub const PIPE_SDDL: &str = "D:P(A;;GA;;;SY)(A;;0x00100083;;;IU)";
/// How long the service stays without a VM before it ends.
pub const IDLE: Duration = Duration::from_secs(5 * 60);

static STATUS: AtomicPtr<std::ffi::c_void> = AtomicPtr::new(std::ptr::null_mut());
static STOP: AtomicPtr<std::ffi::c_void> = AtomicPtr::new(std::ptr::null_mut());

/// The service's entry: its dispatcher, until its ServiceMain returns. Run other than by the service control
/// manager, it says so and ends.
pub fn main() -> i32 {
    let name = wide(SERVICE_NAME);
    let table = [
        SERVICE_TABLE_ENTRYW { lpServiceName: name.as_ptr() as *mut u16, lpServiceProc: Some(service_main) },
        SERVICE_TABLE_ENTRYW { lpServiceName: std::ptr::null_mut(), lpServiceProc: None },
    ];
    if unsafe { StartServiceCtrlDispatcherW(table.as_ptr()) } == 0 {
        eprintln!("surogate-service runs as the Surogate VM service, which Windows starts: {}", std::io::Error::last_os_error());
        return 2;
    }
    0
}

fn report(state: SERVICE_STATUS_CURRENT_STATE, code: u32) {
    let status = SERVICE_STATUS {
        dwServiceType: SERVICE_WIN32_OWN_PROCESS,
        dwCurrentState: state,
        dwControlsAccepted: if state == SERVICE_RUNNING { SERVICE_ACCEPT_STOP } else { 0 },
        dwWin32ExitCode: code,
        dwServiceSpecificExitCode: 0,
        dwCheckPoint: 0,
        dwWaitHint: if state == SERVICE_STOP_PENDING { 5_000 } else { 0 },
    };
    let handle = STATUS.load(Ordering::SeqCst) as SERVICE_STATUS_HANDLE;
    if !handle.is_null() {
        unsafe { SetServiceStatus(handle, &status) };
    }
}

unsafe extern "system" fn control(control: u32, _kind: u32, _data: *mut std::ffi::c_void, _context: *mut std::ffi::c_void) -> u32 {
    match control {
        SERVICE_CONTROL_STOP => {
            report(SERVICE_STOP_PENDING, NO_ERROR);
            let stop = STOP.load(Ordering::SeqCst);
            if !stop.is_null() {
                SetEvent(stop);
            }
            NO_ERROR
        }
        SERVICE_CONTROL_INTERROGATE => NO_ERROR,
        _ => ERROR_CALL_NOT_IMPLEMENTED,
    }
}

unsafe extern "system" fn service_main(_count: u32, _arguments: *mut windows_sys::core::PWSTR) {
    let stop = Handle(CreateEventW(std::ptr::null(), 1, 0, std::ptr::null()));
    STOP.store(stop.0, Ordering::SeqCst);
    let name = wide(SERVICE_NAME);
    STATUS.store(RegisterServiceCtrlHandlerExW(name.as_ptr(), Some(control), std::ptr::null()) as *mut _, Ordering::SeqCst);
    report(SERVICE_RUNNING, NO_ERROR);
    let code = serve(&stop);
    report(SERVICE_STOPPED, code);
}

// What every connection shares: the data folders VMs run for, and how many VMs run, for the idle end.
struct State {
    roots: Roots,
    connections: AtomicUsize,
    vms: AtomicUsize,
    last: Mutex<Instant>,
}

impl State {
    fn idle(&self) -> bool {
        self.connections.load(Ordering::SeqCst) == 0 && self.vms.load(Ordering::SeqCst) == 0 && self.last.lock().unwrap_or_else(|poisoned| poisoned.into_inner()).elapsed() >= IDLE
    }
}

/// The pipe served until the service is stopped, or has been idle: its exit code.
fn serve(stop: &Handle) -> u32 {
    let state = Arc::new(State { roots: Roots::default(), connections: AtomicUsize::new(0), vms: AtomicUsize::new(0), last: Mutex::new(Instant::now()) });
    let folder = installed();
    let mut first = true;
    loop {
        let pipe = match Pipe::server(SERVICE_PIPE, Some(PIPE_SDDL), Way::Duplex, first, false) {
            Ok(pipe) => pipe,
            Err(error) => {
                eprintln!("surogate-service could not make its pipe: {error}");
                return error.raw_os_error().unwrap_or(1) as u32;
            }
        };
        first = false;
        loop {
            match pipe.accept_until(stop.0, 30_000) {
                Ok(Accepted::Client) => break,
                Ok(Accepted::Stopped) => return NO_ERROR,
                Ok(Accepted::Nobody) if state.idle() => return NO_ERROR,
                Ok(Accepted::Nobody) => {}
                Err(error) => {
                    eprintln!("surogate-service could not take a caller: {error}");
                    break;
                }
            }
        }
        state.connections.fetch_add(1, Ordering::SeqCst);
        let (state, folder) = (state.clone(), folder.clone());
        thread::spawn(move || {
            connection(Arc::new(pipe), &state, folder);
            *state.last.lock().unwrap_or_else(|poisoned| poisoned.into_inner()) = Instant::now();
            state.connections.fetch_sub(1, Ordering::SeqCst);
        });
    }
}

enum Event {
    Line(Read),
    Gone,
    HelperGone,
    Notice(Notice),
}

// The connection's bytes, read as lines into *to*, until the other end goes or a line is refused.
fn read(pipe: Arc<Pipe>, to: Sender<Event>) {
    let mut lines = Lines::new();
    let mut buffer = vec![0u8; 16 * 1024];
    loop {
        match pipe.read(&mut buffer) {
            Ok(0) | Err(_) => {
                let _ = to.send(Event::Gone);
                return;
            }
            Ok(read) => lines.push(&buffer[..read]),
        }
        while let Some(line) = lines.next_line() {
            let refused = matches!(line, Read::Refused(_));
            if to.send(Event::Line(line)).is_err() || refused {
                return;
            }
        }
    }
}

// The helper's process *pid*, waited on: its end told to *to*.
fn wait_for(pid: u32, to: Sender<Event>) {
    let process = unsafe { OpenProcess(PROCESS_SYNCHRONIZE, 0, pid) };
    if process.is_null() {
        let _ = to.send(Event::HelperGone);
        return;
    }
    let process = Handle(process);
    thread::spawn(move || {
        unsafe { WaitForSingleObject(process.0, INFINITE) };
        drop(process);
        let _ = to.send(Event::HelperGone);
    });
}

fn connection(pipe: Arc<Pipe>, state: &State, folder: Option<String>) {
    let (to, from) = channel::<Event>();
    {
        let (pipe, to) = (pipe.clone(), to.clone());
        thread::spawn(move || read(pipe, to));
    }
    let write = |answer: &Answer| pipe.write_all(answer.line().as_bytes()).is_ok();
    // Nothing is acted on before the caller is known, and the caller is known once a line of theirs has been read.
    let first = match from.recv() {
        Ok(Event::Line(Read::Line(line))) => line,
        Ok(Event::Line(Read::Refused(why))) => {
            write(&Answer::refused(None, why));
            return end(&pipe);
        }
        _ => return end(&pipe),
    };
    let identified = match identify(&pipe) {
        Ok(identified) => identified,
        Err(why) => {
            write(&Answer::refused(None, why));
            return end(&pipe);
        }
    };
    let files = CallerFiles { token: &identified.token, profile: identified.profile.clone(), installed: folder };
    let notify: Notify = {
        let to = Mutex::new(to.clone());
        Arc::new(move |notice| {
            let _ = to.lock().unwrap_or_else(|poisoned| poisoned.into_inner()).send(Event::Notice(notice));
        })
    };
    let hcs = Hcs { token: &identified.token, notify };
    let mut session = Session::new(&hcs, &files, &state.roots, identified.caller.clone());
    let mut counted = false;
    let mut pending = Some(first);
    loop {
        let event = match pending.take() {
            Some(line) => Event::Line(Read::Line(line)),
            None => match from.recv() {
                Ok(event) => event,
                Err(_) => break,
            },
        };
        match event {
            Event::Line(Read::Line(line)) => {
                let answers = match Request::parse(&line) {
                    Ok(request) => session.handle(request),
                    Err(refusal) => vec![refusal],
                };
                if !answers.iter().all(write) {
                    break;
                }
                if !counted {
                    if let Some(pid) = session.helper() {
                        counted = true;
                        state.vms.fetch_add(1, Ordering::SeqCst);
                        wait_for(pid, to.clone());
                    }
                }
                if session.ended() {
                    break;
                }
            }
            Event::Line(Read::Refused(why)) => {
                write(&Answer::refused(None, why));
                break;
            }
            Event::Notice(Notice::Console(text)) => {
                write(&Answer::Console { text });
            }
            Event::Notice(Notice::Exited(status)) => {
                if let Some(answer) = session.exited(status) {
                    write(&answer);
                    break;
                }
            }
            Event::Gone | Event::HelperGone => break,
        }
    }
    drop(session);
    if counted {
        state.vms.fetch_sub(1, Ordering::SeqCst);
    }
    end(&pipe);
}

// The connection's end: the reader's read ended, and the pipe closed as its last holder lets it go. Not disconnected,
// which would throw away an answer the caller has not read yet (DisconnectNamedPipe: "any unread data in the pipe is
// discarded"); a closed server's end leaves it to be read.
fn end(pipe: &Pipe) {
    pipe.cancel();
}

/// One caller of *pipe*, a server's end a client has connected to, served as the service serves each: for the
/// service's own tests, on a pipe of their own.
pub fn serve_one(pipe: Pipe, installed: Option<String>) {
    let state = State { roots: Roots::default(), connections: AtomicUsize::new(1), vms: AtomicUsize::new(0), last: Mutex::new(Instant::now()) };
    connection(Arc::new(pipe), &state, installed);
}
