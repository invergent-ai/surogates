// surogate-vm run: one VM for the app's VM manager, as the signed-in user. It makes the guest's three pipes, for this
// user alone, says "listening", and then does what session.rs decides: the sessions disk and the console log, the VM
// asked of the Surogate VM service (service.rs), the guest's three Hyper-V sockets bound on the VM's runtime ID, each
// relayed to its pipe, the shares, and the VM's end at a kill, at the end of stdin or when the process that started
// this one has gone.
//
// Hyper-V sockets: a Linux guest's AF_VSOCK connection to the host's port P reaches the host's AF_HYPERV listener
// bound to the VM's runtime ID and the service GUID of P (Microsoft Learn, "Make your own integration services":
// SOCKADDR_HV, HV_PROTOCOL_RAW; hcs::service_id). The service lets this user bind them: the VM's document gives the
// caller's SID the HvSocket bind rights (hcs::document).

use std::fs::File;
use std::io::{self, Read as _, Write as _};
use std::os::windows::io::FromRawHandle;
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use pipe::win::{wide, Handle, Pipe, Way};
use windows_sys::core::GUID;
use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, LocalFree, FILETIME, HANDLE, INVALID_HANDLE_VALUE};
use windows_sys::Win32::Networking::WinSock::{
    accept, bind, closesocket, listen, recv, send, setsockopt, shutdown, socket, WSAGetLastError, WSAStartup, AF_HYPERV, INVALID_SOCKET, SD_BOTH, SOCKADDR, SOCKET, SOCKET_ERROR,
    SOCK_STREAM, WSADATA,
};
use windows_sys::Win32::Security::Authorization::ConvertSidToStringSidW;
use windows_sys::Win32::Security::{GetTokenInformation, TokenUser, TOKEN_QUERY, TOKEN_USER};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, GetFileAttributesW, GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION, FILE_APPEND_DATA, FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_OPEN_REPARSE_POINT,
    FILE_SHARE_READ, INVALID_FILE_ATTRIBUTES, OPEN_ALWAYS,
};
use windows_sys::Win32::Storage::Vhd::{
    CreateVirtualDisk, CREATE_VIRTUAL_DISK_FLAG_NONE, CREATE_VIRTUAL_DISK_PARAMETERS, CREATE_VIRTUAL_DISK_VERSION_2, VIRTUAL_DISK_ACCESS_NONE, VIRTUAL_STORAGE_TYPE,
    VIRTUAL_STORAGE_TYPE_DEVICE_VHDX, VIRTUAL_STORAGE_TYPE_VENDOR_MICROSOFT,
};
use windows_sys::Win32::System::Diagnostics::ToolHelp::{CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W, TH32CS_SNAPPROCESS};
use windows_sys::Win32::System::Hypervisor::{HVSOCKET_CONNECTED_SUSPEND, HV_PROTOCOL_RAW, SOCKADDR_HV};
use windows_sys::Win32::System::Threading::{GetCurrentProcess, GetProcessTimes, OpenProcess, OpenProcessToken, WaitForSingleObject, INFINITE, PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_SYNCHRONIZE};

use crate::service::{self, Heard};
use crate::session::{Act, Input, Session};
use crate::{guid, pipe_descriptor, Lines, Read, Run, Said, Told, PORTS};

/// The sessions disk's size, as Linux's and macOS's (src/vm/linux.ts, SESSIONS_BYTES): a dynamic VHDX, which takes
/// on the computer's disk only what the guest writes.
const SESSIONS_BYTES: u64 = 32 * 1024 * 1024 * 1024;
/// How long the service has to start and open its pipe.
const SERVICE_MS: u64 = 60_000;
/// How long a stop asked of the service is waited for. The VM manager kills this process a second after its kill line;
/// the service ends the VM when this process ends besides.
const STOP_MS: u64 = 900;
/// The bytes each direction of a relay holds at once.
const RELAY: usize = 64 * 1024;

// A line to the VM manager, on stdout.
fn say(said: &Said) {
    let mut out = io::stdout().lock();
    let _ = out.write_all(said.line().as_bytes());
    let _ = out.flush();
}

// The signed-in user's SID, as this process's token names it (GetTokenInformation, TokenUser; ConvertSidToStringSidW).
fn user_sid() -> io::Result<String> {
    let mut token: HANDLE = std::ptr::null_mut();
    if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let token = Handle(token);
    let mut needed = 0u32;
    unsafe { GetTokenInformation(token.0, TokenUser, std::ptr::null_mut(), 0, &mut needed) };
    // u64s, so the TOKEN_USER read from it is aligned.
    let mut buffer = vec![0u64; (needed as usize).div_ceil(8).max(1)];
    if unsafe { GetTokenInformation(token.0, TokenUser, buffer.as_mut_ptr().cast(), (buffer.len() * 8) as u32, &mut needed) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let user = unsafe { &*(buffer.as_ptr() as *const TOKEN_USER) };
    let mut text: *mut u16 = std::ptr::null_mut();
    if unsafe { ConvertSidToStringSidW(user.User.Sid, &mut text) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let length = (0..).take_while(|&at| unsafe { *text.add(at) } != 0).count();
    let sid = String::from_utf16_lossy(unsafe { std::slice::from_raw_parts(text, length) });
    unsafe { LocalFree(text.cast()) };
    Ok(sid)
}

// The guest's three pipes, each this one instance, made first (so a pipe of the name someone else made fails the boot
// rather than being joined), for this user alone.
fn pipes(run: &Run) -> Result<[Pipe; 3], String> {
    let sid = user_sid().map_err(|error| format!("this user's SID could not be read: {error}"))?;
    let descriptor = pipe_descriptor(&sid).ok_or_else(|| format!("this user's SID is not of a SID's form: {sid}"))?;
    let make = |name: &str, port: &str| Pipe::server(name, Some(&descriptor), Way::Duplex, true, true).map_err(|error| format!("the guest's {port} pipe could not be made: {error}"));
    Ok([make(&run.pipes.control, "control")?, make(&run.pipes.net, "net")?, make(&run.pipes.inbound, "inbound")?])
}

// Whether *path* is a reparse point: its own attributes, not those of what it names (GetFileAttributesW does not
// follow one). None where nothing is there.
fn reparse_point(path: &str) -> Option<bool> {
    let attributes = unsafe { GetFileAttributesW(wide(path).as_ptr()) };
    (attributes != INVALID_FILE_ATTRIBUTES).then_some(attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0)
}

// The sessions disk, made as an empty dynamic VHDX where none is there (CreateVirtualDisk, CREATE_VIRTUAL_DISK_VERSION_2,
// which fails where a file of the name already is); the guest makes its file system at its first boot. A link
// planted at its name is refused.
fn sessions_disk(path: &str) -> Result<(), String> {
    match reparse_point(path) {
        Some(true) => return Err(format!("the sessions disk is a link, not a file: {path}")),
        Some(false) => return Ok(()),
        None => {}
    }
    let kind = VIRTUAL_STORAGE_TYPE { DeviceId: VIRTUAL_STORAGE_TYPE_DEVICE_VHDX, VendorId: VIRTUAL_STORAGE_TYPE_VENDOR_MICROSOFT };
    let mut parameters = CREATE_VIRTUAL_DISK_PARAMETERS { Version: CREATE_VIRTUAL_DISK_VERSION_2, ..Default::default() };
    parameters.Anonymous.Version2.MaximumSize = SESSIONS_BYTES;
    let mut made: HANDLE = std::ptr::null_mut();
    let error = unsafe {
        CreateVirtualDisk(&kind, wide(path).as_ptr(), VIRTUAL_DISK_ACCESS_NONE, std::ptr::null_mut(), CREATE_VIRTUAL_DISK_FLAG_NONE, 0, &parameters, std::ptr::null(), &mut made)
    };
    if error != 0 {
        return Err(format!("the sessions disk could not be made: {}", io::Error::from_raw_os_error(error as i32)));
    }
    unsafe { CloseHandle(made) };
    Ok(())
}

// The console log, opened to add to, without following a reparse point at its name, and refused if it is one.
fn console_log(path: &str) -> Result<File, String> {
    let opened = unsafe { CreateFileW(wide(path).as_ptr(), FILE_APPEND_DATA, FILE_SHARE_READ, std::ptr::null(), OPEN_ALWAYS, FILE_FLAG_OPEN_REPARSE_POINT, std::ptr::null_mut()) };
    if opened == INVALID_HANDLE_VALUE {
        return Err(format!("the console log could not be opened: {}", io::Error::last_os_error()));
    }
    let file = unsafe { File::from_raw_handle(opened) };
    let mut information: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
    if unsafe { GetFileInformationByHandle(opened, &mut information) } == 0 {
        return Err(format!("the console log could not be read: {}", io::Error::last_os_error()));
    }
    if information.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err(format!("the console log is a link, not a file: {path}"));
    }
    Ok(file)
}

// A GUID from its string form (lib.rs, guid).
fn guid_of(text: &str) -> Option<GUID> {
    let (data1, data2, data3, data4) = guid(text)?;
    Some(GUID { data1, data2, data3, data4 })
}

struct Socket(SOCKET);
unsafe impl Send for Socket {}
unsafe impl Sync for Socket {}
impl Drop for Socket {
    fn drop(&mut self) {
        unsafe { closesocket(self.0) };
    }
}

fn last_socket_error() -> io::Error {
    io::Error::from_raw_os_error(unsafe { WSAGetLastError() })
}

// A Hyper-V socket listening for the guest's connection to its vsock *port*, on the VM *vm*.
fn listener(vm: GUID, port: u32) -> io::Result<Socket> {
    let made = unsafe { socket(AF_HYPERV as i32, SOCK_STREAM, HV_PROTOCOL_RAW as i32) };
    if made == INVALID_SOCKET {
        return Err(last_socket_error());
    }
    let socket = Socket(made);
    let service = guid_of(&hcs::service_id(port)).expect("a service GUID is a GUID");
    let address = SOCKADDR_HV { Family: AF_HYPERV, Reserved: 0, VmId: vm, ServiceId: service };
    if unsafe { bind(socket.0, (&address as *const SOCKADDR_HV).cast::<SOCKADDR>(), std::mem::size_of::<SOCKADDR_HV>() as i32) } == SOCKET_ERROR {
        return Err(last_socket_error());
    }
    if unsafe { listen(socket.0, 1) } == SOCKET_ERROR {
        return Err(last_socket_error());
    }
    Ok(socket)
}

// Every byte of *bytes* sent on *socket*.
fn send_all(socket: &Socket, mut bytes: &[u8]) -> io::Result<()> {
    while !bytes.is_empty() {
        let length = bytes.len().min(i32::MAX as usize) as i32;
        let sent = unsafe { send(socket.0, bytes.as_ptr(), length, 0) };
        if sent == SOCKET_ERROR || sent == 0 {
            return Err(last_socket_error());
        }
        bytes = &bytes[sent as usize..];
    }
    Ok(())
}

// The guest's connection to one port, taken on *listening*, and its bytes carried to and from *pipe*, the VM manager's
// end, until either end goes: then both are closed, so the other side sees the port's end. Each direction holds at
// most RELAY bytes, written before more is read.
fn relay(listening: Socket, pipe: Pipe) {
    let taken = unsafe { accept(listening.0, std::ptr::null_mut(), std::ptr::null_mut()) };
    drop(listening);
    if taken == INVALID_SOCKET {
        pipe.disconnect();
        return;
    }
    let socket = Arc::new(Socket(taken));
    // The connection outlives this computer's sleep (HVSOCKET_CONNECTED_SUSPEND), as the guest's own run does.
    let keep: u32 = 1;
    unsafe { setsockopt(socket.0, HV_PROTOCOL_RAW as i32, HVSOCKET_CONNECTED_SUSPEND as i32, (&keep as *const u32).cast(), 4) };
    // The VM manager connected before the boot; this takes it.
    if pipe.accept().is_err() {
        return;
    }
    let pipe = Arc::new(pipe);
    let (to_pipe, from_socket) = (Arc::clone(&pipe), Arc::clone(&socket));
    let guest = thread::spawn(move || {
        let mut buffer = vec![0u8; RELAY];
        loop {
            let read = unsafe { recv(from_socket.0, buffer.as_mut_ptr(), RELAY as i32, 0) };
            if read <= 0 || to_pipe.write_all(&buffer[..read as usize]).is_err() {
                break;
            }
        }
        to_pipe.cancel();
        to_pipe.disconnect();
    });
    let mut buffer = vec![0u8; RELAY];
    loop {
        match pipe.read(&mut buffer) {
            Ok(0) | Err(_) => break,
            Ok(read) => {
                if send_all(&socket, &buffer[..read]).is_err() {
                    break;
                }
            }
        }
    }
    unsafe { shutdown(socket.0, SD_BOTH) };
    let _ = guest.join();
}

// The VM manager's lines, read off stdin within the line bound, each told to *to*; then its end.
fn read_stdin(to: Sender<Input>) {
    thread::spawn(move || {
        let mut lines = Lines::new();
        let mut stdin = io::stdin().lock();
        let mut buffer = vec![0u8; 16 * 1024];
        loop {
            let read = match stdin.read(&mut buffer) {
                Ok(0) | Err(_) => break,
                Ok(read) => read,
            };
            lines.push(&buffer[..read]);
            while let Some(line) = lines.next_line() {
                let input = match line {
                    Read::Line(line) => Input::Told(Told::parse(&line)),
                    Read::Refused(why) => {
                        let _ = to.send(Input::Told(Err(Said::Refused { id: None, why: why.into() })));
                        let _ = to.send(Input::StdinEnd);
                        return;
                    }
                };
                if to.send(input).is_err() {
                    return;
                }
            }
        }
        let _ = to.send(Input::StdinEnd);
    });
}

fn created(process: HANDLE) -> Option<u64> {
    let (mut creation, mut exit, mut kernel, mut user): (FILETIME, FILETIME, FILETIME, FILETIME) = unsafe { std::mem::zeroed() };
    if unsafe { GetProcessTimes(process, &mut creation, &mut exit, &mut kernel, &mut user) } == 0 {
        return None;
    }
    Some(((creation.dwHighDateTime as u64) << 32) | creation.dwLowDateTime as u64)
}

// The process that started this one, waited on: its end told to *to*. A process of that id made after this one is
// another, its id reused, and the parent has already gone. Where it cannot be found, stdin's end alone ends the VM.
fn watch_parent(to: Sender<Input>) {
    let me = std::process::id();
    let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) };
    if snapshot == INVALID_HANDLE_VALUE {
        return;
    }
    let snapshot = Handle(snapshot);
    let mut entry: PROCESSENTRY32W = unsafe { std::mem::zeroed() };
    entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
    let mut parent = None;
    let mut more = unsafe { Process32FirstW(snapshot.0, &mut entry) } != 0;
    while more {
        if entry.th32ProcessID == me {
            parent = Some(entry.th32ParentProcessID);
            break;
        }
        more = unsafe { Process32NextW(snapshot.0, &mut entry) } != 0;
    }
    let Some(parent) = parent else { return };
    let opened = unsafe { OpenProcess(PROCESS_SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, 0, parent) };
    if opened.is_null() {
        let _ = to.send(Input::ParentGone);
        return;
    }
    let process = Handle(opened);
    match (created(process.0), created(unsafe { GetCurrentProcess() })) {
        (Some(theirs), Some(mine)) if theirs <= mine => {}
        _ => {
            let _ = to.send(Input::ParentGone);
            return;
        }
    }
    thread::spawn(move || {
        // The whole handle, moved here, so it is closed when the wait is over.
        let process = process;
        unsafe { WaitForSingleObject(process.0, INFINITE) };
        let _ = to.send(Input::ParentGone);
    });
}

fn heard(heard: Heard) -> Input {
    match heard {
        Heard::Answer(answer) => Input::Service(answer),
        Heard::End(why) => Input::ServiceEnd(why),
    }
}

/// One VM, as `surogate-vm run` is asked for it: its exit code.
pub fn run(run: Run) -> i32 {
    let mut data: WSADATA = unsafe { std::mem::zeroed() };
    if unsafe { WSAStartup(0x0202, &mut data) } != 0 {
        say(&Said::Failed { why: format!("Windows's sockets could not be started: {}", io::Error::from_raw_os_error(unsafe { GetLastError() } as i32)) });
        return 1;
    }
    let mut pipes = match pipes(&run) {
        Ok(pipes) => Some(pipes),
        Err(why) => {
            say(&Said::Failed { why });
            return 1;
        }
    };
    say(&Said::Listening);

    let (to, inputs): (Sender<Input>, Receiver<Input>) = mpsc::channel();
    read_stdin(to.clone());
    watch_parent(to.clone());

    let mut session = Session::new(run.data.clone(), std::process::id());
    let mut service: Option<Arc<Pipe>> = None;
    let mut console: Option<File> = None;
    let mut stop_by: Option<Instant> = None;
    loop {
        let input = match stop_by {
            Some(by) => match inputs.recv_timeout(by.saturating_duration_since(Instant::now())) {
                Ok(input) => input,
                Err(_) => Input::StopLate,
            },
            None => match inputs.recv() {
                Ok(input) => input,
                Err(_) => Input::StdinEnd,
            },
        };
        let mut acts = session.on(input);
        while !acts.is_empty() {
            let mut next = Vec::new();
            for act in acts {
                match act {
                    Act::Say(said) => say(&said),
                    Act::Prepare { sessions, console: log } => {
                        let prepared = sessions_disk(&sessions).and_then(|()| console_log(&log)).and_then(|file| {
                            console = Some(file);
                            let pipe = service::connect(Instant::now() + Duration::from_millis(SERVICE_MS))?;
                            service::listen(Arc::clone(&pipe), to.clone(), heard);
                            service = Some(pipe);
                            Ok(())
                        });
                        next.extend(session.on(Input::Prepared(prepared)));
                    }
                    Act::Ask(request) => {
                        let asked = match &service {
                            Some(pipe) => service::ask(pipe, &request),
                            None => Err("the Surogate VM service is not connected".into()),
                        };
                        if let Err(why) = asked {
                            next.extend(session.on(Input::ServiceEnd(why)));
                        }
                    }
                    Act::Bind(runtime) => {
                        let bound = match (guid_of(&runtime), pipes.take()) {
                            (Some(vm), Some(three)) => PORTS
                                .iter()
                                .zip(["control", "net", "inbound"])
                                .map(|(&port, name)| listener(vm, port).map_err(|error| format!("the guest's {name} port could not be bound: {error}")))
                                .collect::<Result<Vec<Socket>, String>>()
                                .map(|listeners| {
                                    for (listening, pipe) in listeners.into_iter().zip(three) {
                                        thread::spawn(move || relay(listening, pipe));
                                    }
                                }),
                            (None, _) => Err("the VM's runtime ID is not a GUID".into()),
                            (_, None) => Err("the guest's ports were bound already".into()),
                        };
                        next.extend(session.on(Input::Bound(bound)));
                    }
                    Act::Console(text) => {
                        if let Some(file) = console.as_mut() {
                            let _ = file.write_all(text.as_bytes());
                        }
                    }
                    Act::AwaitStop => stop_by = Some(Instant::now() + Duration::from_millis(STOP_MS)),
                    Act::Exit(code) => return code,
                }
            }
            acts = next;
        }
    }
}
