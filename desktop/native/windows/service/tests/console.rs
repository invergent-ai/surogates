// The guest's console on Windows itself: the service's end of COM1's pipe, as HCS's worker opens its other end, and
// what the worker writes there told as the console's text.

#![cfg(windows)]

use std::io;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::mpsc::channel;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use pipe::win::{wide, Handle, Pipe, Way};
use service::hyper::{console, Notice, Notify};
use windows_sys::Win32::Foundation::{ERROR_ACCESS_DENIED, GENERIC_READ, HANDLE, INVALID_HANDLE_VALUE};
use windows_sys::Win32::Security::{
    CreateRestrictedToken, CreateWellKnownSid, DuplicateTokenEx, RevertToSelf, SecurityImpersonation, TokenImpersonation, WinBuiltinAdministratorsSid, SECURITY_MAX_SID_SIZE,
    SID_AND_ATTRIBUTES, TOKEN_ALL_ACCESS, TOKEN_DUPLICATE,
};
use windows_sys::Win32::Storage::FileSystem::{CreateFileW, OPEN_EXISTING};
use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken, SetThreadToken};

// A pipe name of the test's own, each unique.
fn name() -> String {
    static NEXT: AtomicU32 = AtomicU32::new(0);
    format!(r"\\.\pipe\surogate-console-test-{}-{}", std::process::id(), NEXT.fetch_add(1, Ordering::SeqCst))
}

#[test]
fn takes_com1_s_other_end_opened_to_read_and_to_write_and_tells_what_it_writes() {
    let name = name();
    let (to, from) = channel();
    let to = Mutex::new(to);
    let notify: Notify = Arc::new(move |notice| {
        if let Notice::Console(text) = notice {
            let _ = to.lock().unwrap().send(text);
        }
    });
    let reading = console(&name, notify).expect("the console's pipe is made");
    // HCS's worker opens COM1's pipe as a serial port's other end: to read what the host types and to write what the
    // guest prints.
    let worker = Pipe::open(&name).expect("COM1's other end opens the console's pipe to read and to write");
    let printed = "[    0.000000] Linux version 6.12.48\r\nsurogate: the protected-names rule attached 16 of 16 hooks\r\n";
    worker.write_all(printed.as_bytes()).unwrap();
    let mut told = String::new();
    while told.len() < printed.len() {
        told.push_str(&from.recv_timeout(Duration::from_secs(10)).expect("the console's text is told"));
    }
    assert_eq!(told, printed);
    drop(worker);
    drop(reading);
}

#[test]
fn an_inbound_pipe_refuses_a_client_that_opens_it_to_read_and_to_write() {
    // Why the console's pipe is made both ways: a pipe its server alone reads refuses a client that asks to read it,
    // as COM1's other end does, so nothing the guest printed reached the console's log.
    let name = name();
    let _server = Pipe::server(&name, None, Way::Inbound, true, true).unwrap();
    assert!(Pipe::open(&name).is_err(), "an inbound pipe took a client that opens it to read");
}

// This process's token, to impersonate, with Administrators made deny-only: an ordinary user of this computer, as the
// console's pipe sees them (CreateRestrictedToken, a deny-only SID).
fn ordinary_user() -> Handle {
    unsafe {
        let mut token: HANDLE = std::ptr::null_mut();
        assert!(OpenProcessToken(GetCurrentProcess(), TOKEN_DUPLICATE | TOKEN_ALL_ACCESS, &mut token) != 0);
        let token = Handle(token);
        let mut copy: HANDLE = std::ptr::null_mut();
        assert!(DuplicateTokenEx(token.0, TOKEN_ALL_ACCESS, std::ptr::null(), SecurityImpersonation, TokenImpersonation, &mut copy) != 0);
        let copy = Handle(copy);
        let mut sid = [0u8; SECURITY_MAX_SID_SIZE as usize];
        let mut size = SECURITY_MAX_SID_SIZE;
        assert!(CreateWellKnownSid(WinBuiltinAdministratorsSid, std::ptr::null_mut(), sid.as_mut_ptr().cast(), &mut size) != 0);
        let deny = [SID_AND_ATTRIBUTES { Sid: sid.as_mut_ptr().cast(), Attributes: 0 }];
        let mut made: HANDLE = std::ptr::null_mut();
        assert!(CreateRestrictedToken(copy.0, 0, 1, deny.as_ptr(), 0, std::ptr::null(), 0, std::ptr::null(), &mut made) != 0);
        Handle(made)
    }
}

// The pipe *name* opened to read alone, as *user* where one is given, or why not.
fn open_to_read(name: &str, user: Option<&Handle>) -> io::Result<Handle> {
    unsafe {
        if let Some(user) = user {
            assert!(SetThreadToken(std::ptr::null(), user.0) != 0);
        }
        let opened = CreateFileW(wide(name).as_ptr(), GENERIC_READ, 0, std::ptr::null(), OPEN_EXISTING, 0, std::ptr::null_mut());
        let error = io::Error::last_os_error();
        if user.is_some() {
            RevertToSelf();
        }
        if opened == INVALID_HANDLE_VALUE {
            return Err(error);
        }
        Ok(Handle(opened))
    }
}

#[test]
fn lets_an_ordinary_user_take_none_of_the_console_s_pipe_not_even_to_read() {
    let name = name();
    let _reading = console(&name, Arc::new(|_| {})).expect("the console's pipe is made");
    // Its one instance is HCS's worker's to take: an ordinary user who took it first, if only to read, would leave the
    // guest's console with nowhere to go.
    let refused = open_to_read(&name, Some(&ordinary_user())).err().expect("an ordinary user opened the console's pipe");
    assert_eq!(refused.raw_os_error(), Some(ERROR_ACCESS_DENIED as i32), "{refused}");
    // An administrator, as this process is, opens it.
    assert!(open_to_read(&name, None).is_ok());
}
