// The Surogate VM service, as the user reaches it: started by the service control manager if it is not running
// (StartServiceW, which the installer lets interactive users do), and its pipe opened only once the pipe's server is
// the service's own process (GetNamedPipeServerProcessId against the service's SC_STATUS_PROCESS_INFO). Nothing is
// written before that: anyone may make a pipe of the service's name while the service is stopped, and a pipe's server
// may act as its client once the client has written to it.

use std::io;
use std::sync::mpsc::Sender;
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use pipe::win::{wide, Pipe};
use pipe::{Answer, Lines, Read, Request, SERVICE_PIPE};
use windows_sys::Win32::Foundation::{ERROR_ACCESS_DENIED, ERROR_FILE_NOT_FOUND, ERROR_PIPE_BUSY, ERROR_SERVICE_ALREADY_RUNNING, ERROR_SERVICE_DOES_NOT_EXIST};
use windows_sys::Win32::System::Pipes::WaitNamedPipeW;
use windows_sys::Win32::System::Services::{
    CloseServiceHandle, OpenSCManagerW, OpenServiceW, QueryServiceStatusEx, StartServiceW, SC_HANDLE, SC_MANAGER_CONNECT, SC_STATUS_PROCESS_INFO, SERVICE_QUERY_STATUS,
    SERVICE_RUNNING, SERVICE_START, SERVICE_STATUS_PROCESS,
};

/// The service's name, as the installer registers it (service/src/server.rs, SERVICE_NAME).
pub const SERVICE_NAME: &str = "SurogateVM";
/// Section 11's words for a computer the service is missing from.
pub const MISSING: &str = "Surogate's sandbox tools are missing: the Surogate VM service is not installed. Install Surogate again";

struct Scm(SC_HANDLE);
impl Drop for Scm {
    fn drop(&mut self) {
        unsafe { CloseServiceHandle(self.0) };
    }
}

/// The service's state as the service control manager says it: whether it is installed, running, and its process.
pub enum State {
    Missing,
    Stopped,
    Running(u32),
}

fn status(service: &Scm) -> io::Result<SERVICE_STATUS_PROCESS> {
    let mut status: SERVICE_STATUS_PROCESS = unsafe { std::mem::zeroed() };
    let mut needed = 0u32;
    if unsafe { QueryServiceStatusEx(service.0, SC_STATUS_PROCESS_INFO, (&mut status as *mut SERVICE_STATUS_PROCESS).cast(), std::mem::size_of::<SERVICE_STATUS_PROCESS>() as u32, &mut needed) } == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(status)
}

// The service opened for *access*: None where it is not installed.
fn open(access: u32) -> io::Result<Option<Scm>> {
    let manager = unsafe { OpenSCManagerW(std::ptr::null(), std::ptr::null(), SC_MANAGER_CONNECT) };
    if manager.is_null() {
        return Err(io::Error::last_os_error());
    }
    let manager = Scm(manager);
    let service = unsafe { OpenServiceW(manager.0, wide(SERVICE_NAME).as_ptr(), access) };
    if service.is_null() {
        let error = io::Error::last_os_error();
        return match error.raw_os_error().map(|code| code as u32) {
            Some(ERROR_SERVICE_DOES_NOT_EXIST) => Ok(None),
            _ => Err(error),
        };
    }
    Ok(Some(Scm(service)))
}

fn opened(access: u32) -> Result<Option<Scm>, String> {
    open(access).map_err(|error| format!("the Surogate VM service could not be opened: {error}"))
}

/// The service's state, asked of the service control manager.
pub fn state() -> Result<State, String> {
    let Some(service) = opened(SERVICE_QUERY_STATUS)? else { return Ok(State::Missing) };
    let status = status(&service).map_err(|error| format!("the Surogate VM service's state could not be read: {error}"))?;
    Ok(if status.dwCurrentState == SERVICE_RUNNING { State::Running(status.dwProcessId) } else { State::Stopped })
}

/// The service, running, by *deadline*: its process.
fn running(deadline: Instant) -> Result<u32, String> {
    // A user the installer did not let start it may still find it running.
    let service = match open(SERVICE_QUERY_STATUS | SERVICE_START) {
        Err(error) if error.raw_os_error() == Some(ERROR_ACCESS_DENIED as i32) => opened(SERVICE_QUERY_STATUS)?,
        other => other.map_err(|error| format!("the Surogate VM service could not be opened: {error}"))?,
    }
    .ok_or_else(|| MISSING.to_owned())?;
    let mut started = false;
    loop {
        let status = status(&service).map_err(|error| format!("the Surogate VM service's state could not be read: {error}"))?;
        if status.dwCurrentState == SERVICE_RUNNING {
            return Ok(status.dwProcessId);
        }
        if !started {
            started = true;
            if unsafe { StartServiceW(service.0, 0, std::ptr::null()) } == 0 {
                let error = io::Error::last_os_error();
                if error.raw_os_error() != Some(ERROR_SERVICE_ALREADY_RUNNING as i32) {
                    return Err(format!("the Surogate VM service did not start: {error}"));
                }
            }
        }
        if Instant::now() > deadline {
            return Err("the Surogate VM service did not start in time".into());
        }
        thread::sleep(Duration::from_millis(50));
    }
}

/// The service's pipe, opened once its server is the running service's own process, by *deadline*.
pub fn connect(deadline: Instant) -> Result<Arc<Pipe>, String> {
    let pid = running(deadline)?;
    loop {
        match Pipe::open(SERVICE_PIPE) {
            Ok(pipe) => {
                let server = pipe.server_pid().map_err(|error| format!("the service's pipe could not say whose it is: {error}"))?;
                if server != pid {
                    return Err("the pipe of the Surogate VM service's name is not the service's: it was not used".into());
                }
                return Ok(Arc::new(pipe));
            }
            Err(error) if Instant::now() > deadline => return Err(format!("the Surogate VM service's pipe could not be opened: {error}")),
            Err(error) if error.raw_os_error() == Some(ERROR_PIPE_BUSY as i32) => unsafe {
                WaitNamedPipeW(wide(SERVICE_PIPE).as_ptr(), 100);
            },
            Err(error) if error.raw_os_error() == Some(ERROR_FILE_NOT_FOUND as i32) => thread::sleep(Duration::from_millis(20)),
            Err(error) => return Err(format!("the Surogate VM service's pipe could not be opened: {error}")),
        }
    }
}

/// What the service says, read off its pipe.
pub enum Heard {
    Answer(Answer),
    /// A line that is not one of the service's answers, or the pipe's end.
    End(String),
}

/// The service's answers on *pipe*, each told to *to* by *wrap*, until the pipe ends.
pub fn listen<E: Send + 'static>(pipe: Arc<Pipe>, to: Sender<E>, wrap: fn(Heard) -> E) {
    thread::spawn(move || {
        let mut lines = Lines::new();
        let mut buffer = vec![0u8; 16 * 1024];
        loop {
            let read = match pipe.read(&mut buffer) {
                Ok(0) | Err(_) => {
                    let _ = to.send(wrap(Heard::End("the Surogate VM service closed its pipe".into())));
                    return;
                }
                Ok(read) => read,
            };
            lines.push(&buffer[..read]);
            while let Some(line) = lines.next_line() {
                let heard = match line {
                    Read::Line(line) => match serde_json::from_str::<Answer>(&line) {
                        Ok(answer) => Heard::Answer(answer),
                        Err(_) => Heard::End("the Surogate VM service said a line that is not one of its answers".into()),
                    },
                    Read::Refused(why) => Heard::End(format!("the Surogate VM service said {why}")),
                };
                let ended = matches!(heard, Heard::End(_));
                if to.send(wrap(heard)).is_err() || ended {
                    return;
                }
            }
        }
    });
}

/// *request* written to the service.
pub fn ask(pipe: &Pipe, request: &Request) -> Result<(), String> {
    pipe.write_all(request.line().as_bytes()).map_err(|error| format!("the Surogate VM service could not be asked: {error}"))
}
