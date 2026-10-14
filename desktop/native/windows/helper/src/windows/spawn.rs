//! A program started holding the caller's first descriptors and nothing else of the caller's (spawn.rs). Windows
//! hands a child every inheritable handle of its parent's unless the parent names those it may have (Microsoft,
//! UpdateProcThreadAttribute, PROC_THREAD_ATTRIBUTE_HANDLE_LIST), and libuv starts every child of Node's with
//! inheritance on, for its stdio: so the program is started from here, by CreateProcessW, given the helper's standard
//! handles and its runtime's table cut to the descriptors kept, and those handles alone.
//!
//! Windows has no exec: the helper stays, as the program's parent, and ends as it ends, with its exit code. The program
//! is in a job of the helper's own that ends it when the helper goes (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE): killed, or
//! ended with the app that started it, the helper takes the program and whatever it started along, as a process group
//! killed whole does on Linux. A program that cannot be started is said as a shell says it, and the helper ends 127.

use std::io::Write;
use std::os::windows::ffi::OsStrExt;

use windows_sys::Win32::Foundation::{CloseHandle, GetHandleInformation, GetLastError, SetHandleInformation, HANDLE, HANDLE_FLAG_INHERIT, WAIT_OBJECT_0};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation, SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};
use windows_sys::Win32::System::Threading::{
    CreateProcessW, DeleteProcThreadAttributeList, GetExitCodeProcess, GetStartupInfoW, InitializeProcThreadAttributeList, ResumeThread,
    TerminateProcess, UpdateProcThreadAttribute, WaitForSingleObject, CREATE_NO_WINDOW, CREATE_SUSPENDED, EXTENDED_STARTUPINFO_PRESENT, INFINITE,
    LPPROC_THREAD_ATTRIBUTE_LIST, PROCESS_INFORMATION, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, STARTF_USESTDHANDLES, STARTUPINFOEXW, STARTUPINFOW,
};

use super::wide;
use crate::spawn::{command_line, inherited, Spawn};

// Ends the helper with *code*, *words* on its stderr: the program's, now, as the helper starts nothing more.
fn fail(code: i32, words: &str) -> ! {
    let _ = writeln!(std::io::stderr().lock(), "{words}");
    std::process::exit(code);
}

/// Starts *spawn*'s program, waits for it, and ends with its exit code.
pub fn run(spawn: &Spawn) -> ! {
    let mut given = STARTUPINFOW { cb: size_of::<STARTUPINFOW>() as u32, ..Default::default() };
    // SAFETY: the structure is the size its cb says.
    unsafe { GetStartupInfoW(&mut given) };
    // The runtime's table, as the helper was given it: none where its caller wrote none.
    let table = if given.lpReserved2.is_null() || given.cbReserved2 == 0 {
        None
    } else {
        // SAFETY: Windows keeps the table the process was started with for its life, cbReserved2 bytes long.
        Some(unsafe { std::slice::from_raw_parts(given.lpReserved2, usize::from(given.cbReserved2)) })
    };
    let (mut cut, mut handles) = match table {
        Some(table) => inherited(table, spawn.keep).unwrap_or_else(|| fail(126, "surogate-helper: its caller's table of descriptors is not one")),
        None => (Vec::new(), Vec::new()),
    };
    let standard = (given.dwFlags & STARTF_USESTDHANDLES) != 0;
    if standard {
        handles.extend([given.hStdInput, given.hStdOutput, given.hStdError].iter().map(|&handle| handle as usize));
    }
    handles.sort_unstable();
    handles.dedup();
    // Each one inheritable, as an inherited handle is; what is no handle of this process's is left out.
    let handles: Vec<HANDLE> = handles
        .into_iter()
        .filter(|&handle| handle != 0 && handle != usize::MAX && handle != usize::MAX - 1)
        .map(|handle| handle as HANDLE)
        .filter(|&handle| {
            let mut flags = 0u32;
            // SAFETY: a handle value is only asked about, and its inherit flag set on this process's own handle.
            unsafe { GetHandleInformation(handle, &mut flags) != 0 && (flags & HANDLE_FLAG_INHERIT != 0 || SetHandleInformation(handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT) != 0) }
        })
        .collect();

    let mut size = 0usize;
    // SAFETY: the first call only says how large a list of one attribute is.
    unsafe { InitializeProcThreadAttributeList(std::ptr::null_mut(), 1, 0, &mut size) };
    let mut list = vec![0u64; size.div_ceil(8)];
    let attributes = list.as_mut_ptr() as LPPROC_THREAD_ATTRIBUTE_LIST;
    // SAFETY: the list is the size the first call said; the handles outlive it, and it is deleted before they go.
    if unsafe { InitializeProcThreadAttributeList(attributes, 1, 0, &mut size) } == 0 {
        fail(126, &format!("surogate-helper: no attribute list for the program (error {})", unsafe { GetLastError() }));
    }
    let inherits = !handles.is_empty();
    if inherits
        && unsafe {
            UpdateProcThreadAttribute(
                attributes, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize, handles.as_ptr().cast(), handles.len() * size_of::<HANDLE>(),
                std::ptr::null_mut(), std::ptr::null(),
            )
        } == 0
    {
        fail(126, &format!("surogate-helper: the program's handles could not be named (error {})", unsafe { GetLastError() }));
    }

    let mut startup = STARTUPINFOEXW { StartupInfo: STARTUPINFOW { cb: size_of::<STARTUPINFOEXW>() as u32, ..Default::default() }, lpAttributeList: attributes };
    if standard {
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
        startup.StartupInfo.hStdInput = given.hStdInput;
        startup.StartupInfo.hStdOutput = given.hStdOutput;
        startup.StartupInfo.hStdError = given.hStdError;
    }
    if !cut.is_empty() {
        startup.StartupInfo.cbReserved2 = u16::try_from(cut.len()).unwrap_or_else(|_| fail(126, "surogate-helper: the table of descriptors is too long"));
        startup.StartupInfo.lpReserved2 = cut.as_mut_ptr();
    }
    let program = wide(spawn.program);
    let args: Vec<Vec<u16>> = spawn.args.iter().map(|arg| arg.encode_wide().collect()).collect();
    let mut line = command_line(&spawn.program.encode_wide().collect::<Vec<_>>(), &args);
    line.push(0);
    let mut started = PROCESS_INFORMATION::default();
    // SAFETY: every string is NUL-terminated and outlives the call, which may write into the line; no environment
    // is given, so the program has the helper's, its caller's own; nor a folder, so it starts in the helper's.
    let made = unsafe {
        CreateProcessW(
            program.as_ptr(), line.as_mut_ptr(), std::ptr::null(), std::ptr::null(), i32::from(inherits),
            EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED | CREATE_NO_WINDOW, std::ptr::null(), std::ptr::null(),
            (&raw const startup).cast(), &mut started,
        )
    };
    let error = if made == 0 { Some(std::io::Error::last_os_error()) } else { None };
    // SAFETY: the list was initialized above, and the program has been made with it, or not at all.
    unsafe { DeleteProcThreadAttributeList(attributes) };
    if let Some(error) = error {
        fail(127, &format!("{}: {error}", spawn.program.to_string_lossy()));
    }
    // SAFETY: the job's handle is this process's alone, never inheritable, and closes with it.
    unsafe {
        let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if job.is_null()
            || SetInformationJobObject(job, JobObjectExtendedLimitInformation, (&raw const limits).cast(), size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32) == 0
            || AssignProcessToJobObject(job, started.hProcess) == 0
        {
            let error = GetLastError();
            TerminateProcess(started.hProcess, 126);
            fail(126, &format!("surogate-helper: the program could not be held to the helper's life (error {error})"));
        }
        ResumeThread(started.hThread);
        CloseHandle(started.hThread);
        let mut code = 1u32;
        if WaitForSingleObject(started.hProcess, INFINITE) != WAIT_OBJECT_0 || GetExitCodeProcess(started.hProcess, &mut code) == 0 {
            fail(126, &format!("surogate-helper: the program's end could not be read (error {})", GetLastError()));
        }
        std::process::exit(code as i32);
    }
}
