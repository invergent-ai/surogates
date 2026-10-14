// Named pipes, as the service and surogate-vm.exe make and open them: each end overlapped, so one thread reads while
// another writes (a handle opened for synchronous I/O serializes them, and a pending read would hold every write
// back), each operation with an event of its own (Microsoft Learn, "Synchronous and Asynchronous I/O"; ReadFile,
// WriteFile and ConnectNamedPipe with an OVERLAPPED, GetOverlappedResult with bWait).

use std::io;

use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, LocalFree, ERROR_BROKEN_PIPE, ERROR_IO_PENDING, ERROR_NO_DATA, ERROR_OPERATION_ABORTED, ERROR_PIPE_CONNECTED,
    ERROR_PIPE_NOT_CONNECTED, HANDLE, INVALID_HANDLE_VALUE, WAIT_OBJECT_0,
};
use windows_sys::Win32::Security::Authorization::{ConvertStringSecurityDescriptorToSecurityDescriptorW, SDDL_REVISION_1};
use windows_sys::Win32::Security::{PSECURITY_DESCRIPTOR, SECURITY_ATTRIBUTES};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, ReadFile, WriteFile, FILE_FLAG_FIRST_PIPE_INSTANCE, FILE_FLAG_OVERLAPPED, OPEN_EXISTING, PIPE_ACCESS_DUPLEX, PIPE_ACCESS_INBOUND,
    SECURITY_IMPERSONATION, SECURITY_SQOS_PRESENT,
};
use windows_sys::Win32::System::Pipes::{
    ConnectNamedPipe, CreateNamedPipeW, DisconnectNamedPipe, GetNamedPipeClientProcessId, GetNamedPipeClientSessionId, GetNamedPipeServerProcessId, PIPE_READMODE_BYTE, PIPE_REJECT_REMOTE_CLIENTS, PIPE_TYPE_BYTE,
    PIPE_UNLIMITED_INSTANCES, PIPE_WAIT,
};
use windows_sys::Win32::System::Threading::{CreateEventW, WaitForMultipleObjects};
use windows_sys::Win32::System::IO::{CancelIoEx, GetOverlappedResult, OVERLAPPED};

// The exact rights a byte-pipe client needs, and no more: read and write the pipe, read its attributes, and wait on
// it. Not FILE_APPEND_DATA (0x4), which for a pipe is FILE_CREATE_PIPE_INSTANCE, so a client granted only these
// cannot make another instance of the pipe. A client opens with these, not GENERIC_READ | GENERIC_WRITE, which the
// generic mapping would widen to include FILE_APPEND_DATA.
const CLIENT_RIGHTS: u32 = 0x0001 /* FILE_READ_DATA */ | 0x0002 /* FILE_WRITE_DATA */ | 0x0080 /* FILE_READ_ATTRIBUTES */ | 0x0010_0000 /* SYNCHRONIZE */;
// The pipes' buffers: one line of the protocol.
const BUFFER: u32 = 64 * 1024;

pub fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(Some(0)).collect()
}

/// A handle, closed once when it goes.
pub struct Handle(pub HANDLE);
unsafe impl Send for Handle {}
unsafe impl Sync for Handle {}
impl Drop for Handle {
    fn drop(&mut self) {
        if !self.0.is_null() && self.0 != INVALID_HANDLE_VALUE {
            unsafe { CloseHandle(self.0) };
        }
    }
}

/// A security descriptor made from its SDDL string, freed when it goes.
pub struct Descriptor(PSECURITY_DESCRIPTOR);
impl Descriptor {
    pub fn new(sddl: &str) -> io::Result<Descriptor> {
        let mut made: PSECURITY_DESCRIPTOR = std::ptr::null_mut();
        if unsafe { ConvertStringSecurityDescriptorToSecurityDescriptorW(wide(sddl).as_ptr(), SDDL_REVISION_1, &mut made, std::ptr::null_mut()) } == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(Descriptor(made))
    }

    pub fn attributes(&self) -> SECURITY_ATTRIBUTES {
        SECURITY_ATTRIBUTES { nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32, lpSecurityDescriptor: self.0, bInheritHandle: 0 }
    }
}
impl Drop for Descriptor {
    fn drop(&mut self) {
        unsafe { LocalFree(self.0) };
    }
}

// An event for one overlapped operation: manual-reset, as GetOverlappedResult waits on it.
fn event() -> io::Result<Handle> {
    let made = unsafe { CreateEventW(std::ptr::null(), 1, 0, std::ptr::null()) };
    if made.is_null() {
        return Err(io::Error::last_os_error());
    }
    Ok(Handle(made))
}

/// What a pipe is made for: the protocol's (both ways), or a console's (the server reads alone).
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Way {
    Duplex,
    Inbound,
}

/// What a server's wait for its client ended with.
#[derive(Debug, PartialEq, Eq)]
pub enum Accepted {
    Client,
    Stopped,
    Nobody,
}

/// One end of a named pipe, overlapped.
pub struct Pipe {
    handle: Handle,
}

impl Pipe {
    /// The server's end of a new instance of the pipe *name*, under the descriptor *sddl* (None: the creator's
    /// default), taking no client from another computer (PIPE_REJECT_REMOTE_CLIENTS). With *first*, the pipe's first
    /// instance (FILE_FLAG_FIRST_PIPE_INSTANCE): it fails, rather than joins, a pipe of that name that someone else
    /// made first. *only*: the pipe has this one instance, and takes one client at most.
    pub fn server(name: &str, sddl: Option<&str>, way: Way, first: bool, only: bool) -> io::Result<Pipe> {
        let descriptor = sddl.map(Descriptor::new).transpose()?;
        let attributes = descriptor.as_ref().map(Descriptor::attributes);
        let open = if way == Way::Duplex { PIPE_ACCESS_DUPLEX } else { PIPE_ACCESS_INBOUND } | FILE_FLAG_OVERLAPPED | if first { FILE_FLAG_FIRST_PIPE_INSTANCE } else { 0 };
        let made = unsafe {
            CreateNamedPipeW(
                wide(name).as_ptr(),
                open,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                if only { 1 } else { PIPE_UNLIMITED_INSTANCES },
                BUFFER,
                BUFFER,
                0,
                attributes.as_ref().map_or(std::ptr::null(), |attributes| attributes as *const SECURITY_ATTRIBUTES),
            )
        };
        if made == INVALID_HANDLE_VALUE {
            return Err(io::Error::last_os_error());
        }
        Ok(Pipe { handle: Handle(made) })
    }

    /// The client's end of the pipe *name*, read and written, and impersonable by its server
    /// (SECURITY_SQOS_PRESENT | SECURITY_IMPERSONATION): the service acts on the caller's files as the caller.
    pub fn open(name: &str) -> io::Result<Pipe> {
        let made = unsafe {
            CreateFileW(
                wide(name).as_ptr(),
                CLIENT_RIGHTS,
                0,
                std::ptr::null(),
                OPEN_EXISTING,
                FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT | SECURITY_IMPERSONATION,
                std::ptr::null_mut(),
            )
        };
        if made == INVALID_HANDLE_VALUE {
            return Err(io::Error::last_os_error());
        }
        Ok(Pipe { handle: Handle(made) })
    }

    pub fn raw(&self) -> HANDLE {
        self.handle.0
    }

    // *start* an overlapped operation with an OVERLAPPED and event of its own, and wait for its end: the bytes it
    // moved, or its error.
    fn overlapped(&self, start: impl FnOnce(*mut OVERLAPPED) -> i32) -> io::Result<u32> {
        let done = event()?;
        let mut overlapped: OVERLAPPED = unsafe { std::mem::zeroed() };
        overlapped.hEvent = done.0;
        let started = start(&mut overlapped);
        if started == 0 {
            let error = unsafe { GetLastError() };
            if error != ERROR_IO_PENDING {
                return Err(io::Error::from_raw_os_error(error as i32));
            }
        }
        let mut moved = 0u32;
        if unsafe { GetOverlappedResult(self.handle.0, &overlapped, &mut moved, 1) } == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(moved)
    }

    /// The server's wait for its client. A client that connected first is taken too (ERROR_PIPE_CONNECTED).
    pub fn accept(&self) -> io::Result<()> {
        let done = event()?;
        let mut overlapped: OVERLAPPED = unsafe { std::mem::zeroed() };
        overlapped.hEvent = done.0;
        if unsafe { ConnectNamedPipe(self.handle.0, &mut overlapped) } == 0 {
            match unsafe { GetLastError() } {
                ERROR_PIPE_CONNECTED => return Ok(()),
                ERROR_IO_PENDING => {}
                error => return Err(io::Error::from_raw_os_error(error as i32)),
            }
            let mut moved = 0u32;
            if unsafe { GetOverlappedResult(self.handle.0, &overlapped, &mut moved, 1) } == 0 {
                return Err(io::Error::last_os_error());
            }
        }
        Ok(())
    }

    /// The server's wait for its client, until *stop* is set or *ms* have gone by: whether a client came.
    pub fn accept_until(&self, stop: HANDLE, ms: u32) -> io::Result<Accepted> {
        let done = event()?;
        let mut overlapped: OVERLAPPED = unsafe { std::mem::zeroed() };
        overlapped.hEvent = done.0;
        if unsafe { ConnectNamedPipe(self.handle.0, &mut overlapped) } == 0 {
            match unsafe { GetLastError() } {
                ERROR_PIPE_CONNECTED => return Ok(Accepted::Client),
                ERROR_IO_PENDING => {}
                error => return Err(io::Error::from_raw_os_error(error as i32)),
            }
            let waited = unsafe { WaitForMultipleObjects(2, [done.0, stop].as_ptr(), 0, ms) };
            if waited != WAIT_OBJECT_0 {
                // Ended, and waited for, before its OVERLAPPED goes.
                self.cancel();
                let mut moved = 0u32;
                let finished = unsafe { GetOverlappedResult(self.handle.0, &overlapped, &mut moved, 1) } != 0;
                if finished {
                    return Ok(Accepted::Client);
                }
                return Ok(if waited == WAIT_OBJECT_0 + 1 { Accepted::Stopped } else { Accepted::Nobody });
            }
            let mut moved = 0u32;
            if unsafe { GetOverlappedResult(self.handle.0, &overlapped, &mut moved, 0) } == 0 {
                return Err(io::Error::last_os_error());
            }
        }
        Ok(Accepted::Client)
    }

    /// Bytes into *buffer*: how many, 0 once the other end has gone.
    pub fn read(&self, buffer: &mut [u8]) -> io::Result<usize> {
        let length = buffer.len().min(u32::MAX as usize) as u32;
        match self.overlapped(|overlapped| unsafe { ReadFile(self.handle.0, buffer.as_mut_ptr(), length, std::ptr::null_mut(), overlapped) }) {
            Ok(read) => Ok(read as usize),
            Err(error) if gone(&error) => Ok(0),
            Err(error) => Err(error),
        }
    }

    /// All of *bytes*, written.
    pub fn write_all(&self, mut bytes: &[u8]) -> io::Result<()> {
        while !bytes.is_empty() {
            let length = bytes.len().min(BUFFER as usize) as u32;
            let written = self.overlapped(|overlapped| unsafe { WriteFile(self.handle.0, bytes.as_ptr(), length, std::ptr::null_mut(), overlapped) })?;
            if written == 0 {
                return Err(io::Error::from_raw_os_error(ERROR_NO_DATA as i32));
            }
            bytes = &bytes[written as usize..];
        }
        Ok(())
    }

    /// Every operation under way on this end, ended (CancelIoEx): a read or a wait for a client returns.
    pub fn cancel(&self) {
        unsafe { CancelIoEx(self.handle.0, std::ptr::null()) };
    }

    /// The process at the server's end, as the pipe says it (GetNamedPipeServerProcessId).
    pub fn server_pid(&self) -> io::Result<u32> {
        let mut pid = 0u32;
        if unsafe { GetNamedPipeServerProcessId(self.handle.0, &mut pid) } == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(pid)
    }

    /// The process at the client's end, and its session, as the pipe says them (GetNamedPipeClientProcessId,
    /// GetNamedPipeClientSessionId).
    pub fn client(&self) -> io::Result<(u32, u32)> {
        let (mut pid, mut session) = (0u32, 0u32);
        if unsafe { GetNamedPipeClientProcessId(self.handle.0, &mut pid) } == 0 || unsafe { GetNamedPipeClientSessionId(self.handle.0, &mut session) } == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok((pid, session))
    }

    /// The server's client let go of, its end closed.
    pub fn disconnect(&self) {
        unsafe { DisconnectNamedPipe(self.handle.0) };
    }
}

/// Whether *error* says the other end has gone, or this end's operation was ended.
pub fn gone(error: &io::Error) -> bool {
    matches!(error.raw_os_error().map(|code| code as u32), Some(ERROR_BROKEN_PIPE | ERROR_PIPE_NOT_CONNECTED | ERROR_NO_DATA | ERROR_OPERATION_ABORTED))
}
