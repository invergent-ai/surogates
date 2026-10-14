//! This user's processes, each with its program and its arguments, as Linux reads them from /proc: what the browser
//! host asks to find the processes on a browser's profile. Each process of a snapshot (CreateToolhelp32Snapshot,
//! TH32CS_SNAPPROCESS) is opened for limited information alone, and kept where its token's user is this process's
//! (OpenProcessToken, GetTokenInformation with TokenUser, the SID's bytes as GetLengthSid measures them): another
//! user's is passed over, as /proc does not give its command line. Its program by QueryFullProcessImageNameW, its command line by NtQueryInformationProcess's
//! ProcessCommandLineInformation, which needs PROCESS_QUERY_LIMITED_INFORMATION alone (Microsoft,
//! NtQueryInformationProcess, ProcessCommandLineInformation), split as the C runtime reads it (CommandLineToArgvW).

use std::ptr::null_mut;

use windows_sys::Wdk::System::Threading::{NtQueryInformationProcess, ProcessCommandLineInformation};
use windows_sys::Win32::Foundation::{
    CloseHandle, LocalFree, HANDLE, INVALID_HANDLE_VALUE, STATUS_BUFFER_OVERFLOW, STATUS_BUFFER_TOO_SMALL, STATUS_INFO_LENGTH_MISMATCH, UNICODE_STRING,
};
use windows_sys::Win32::Security::{GetLengthSid, GetTokenInformation, IsValidSid, TokenUser, TOKEN_QUERY, TOKEN_USER};
use windows_sys::Win32::System::Diagnostics::ToolHelp::{CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W, TH32CS_SNAPPROCESS};
use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcess, OpenProcessToken, QueryFullProcessImageNameW, PROCESS_QUERY_LIMITED_INFORMATION};
use windows_sys::Win32::UI::Shell::CommandLineToArgvW;

use super::text;
use crate::answers::Process;

// A command line is at most 32 767 characters (Microsoft, CreateProcessW, lpCommandLine): more than that is no
// command line's.
const MOST_BYTES: u32 = 64 * 1024 + 64;

/// Every process of this user's that answers, by pid.
pub fn listed() -> Result<Vec<Process>, String> {
    // SAFETY: the current process's pseudo handle needs no closing.
    let own = user_of(unsafe { GetCurrentProcess() }).ok_or("this process's own user could not be read")?;
    // SAFETY: a snapshot of every process; closed below.
    let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) };
    if snapshot == INVALID_HANDLE_VALUE {
        return Err("Windows gave no list of its processes".to_string());
    }
    let mut entry = PROCESSENTRY32W { dwSize: size_of::<PROCESSENTRY32W>() as u32, ..Default::default() };
    let mut found = Vec::new();
    // SAFETY: the entry's size is set, and the snapshot open.
    let mut more = unsafe { Process32FirstW(snapshot, &mut entry) } != 0;
    while more {
        if let Some(process) = described(entry.th32ProcessID, &own) {
            found.push(process);
        }
        // SAFETY: as above.
        more = unsafe { Process32NextW(snapshot, &mut entry) } != 0;
    }
    // SAFETY: opened above.
    unsafe { CloseHandle(snapshot) };
    found.sort_by_key(|process| process.pid);
    Ok(found)
}

// The process *pid*, where it is this user's and can be read.
fn described(pid: u32, own: &[u8]) -> Option<Process> {
    if pid == 0 {
        return None;
    }
    // SAFETY: a handle of the process's for limited information, closed below.
    let process = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
    if process.is_null() {
        return None;
    }
    let found = (|| {
        // Two SIDs are one where their bytes are (Microsoft, EqualSid: the revision, the identifier authority and
        // each subauthority).
        if user_of(process)? != own {
            return None;
        }
        let command = command_line(process)?;
        Some(Process { pid, path: image(process).unwrap_or_default(), args: split(&command) })
    })();
    // SAFETY: opened above.
    unsafe { CloseHandle(process) };
    found
}

// The bytes of the SID of *process*'s token's user: copied out of the TOKEN_USER that GetTokenInformation fills,
// whose SID points into that buffer, before the buffer goes.
fn user_of(process: HANDLE) -> Option<Vec<u8>> {
    let mut token: HANDLE = null_mut();
    // SAFETY: a token handle for querying, closed below.
    if unsafe { OpenProcessToken(process, TOKEN_QUERY, &mut token) } == 0 {
        return None;
    }
    let mut size = 0u32;
    // SAFETY: the first call asks for the size alone.
    unsafe { GetTokenInformation(token, TokenUser, null_mut(), 0, &mut size) };
    let mut buffer = vec![0u64; (size as usize).div_ceil(8).max(1)];
    // SAFETY: the buffer holds size bytes, aligned for TOKEN_USER.
    let read = unsafe { GetTokenInformation(token, TokenUser, buffer.as_mut_ptr().cast(), size, &mut size) } != 0;
    // SAFETY: opened above.
    unsafe { CloseHandle(token) };
    if !read {
        return None;
    }
    // SAFETY: the buffer holds the TOKEN_USER the call filled, and the SID it points to, while it is read here.
    unsafe {
        let sid = (*buffer.as_ptr().cast::<TOKEN_USER>()).User.Sid;
        if sid.is_null() || IsValidSid(sid) == 0 {
            return None;
        }
        Some(std::slice::from_raw_parts(sid.cast::<u8>(), GetLengthSid(sid) as usize).to_vec())
    }
}

// The path of *process*'s program.
fn image(process: HANDLE) -> Option<String> {
    let mut path = [0u16; 32_768];
    let mut length = path.len() as u32;
    // SAFETY: the length is the buffer's own, in characters.
    (unsafe { QueryFullProcessImageNameW(process, 0, path.as_mut_ptr(), &mut length) } != 0).then(|| text(&path[..length as usize]))
}

// *process*'s command line, as the UNICODE_STRING at the head of NtQueryInformationProcess's buffer names it. The first
// call asks for its size: Microsoft documents no one status for that answer, and a call too small for its information
// answers any of three (STATUS_INFO_LENGTH_MISMATCH, STATUS_BUFFER_TOO_SMALL, STATUS_BUFFER_OVERFLOW).
fn command_line(process: HANDLE) -> Option<Vec<u16>> {
    let mut size = 0u32;
    // SAFETY: the first call asks for the size alone.
    let asked = unsafe { NtQueryInformationProcess(process, ProcessCommandLineInformation, null_mut(), 0, &mut size) };
    let sized = [STATUS_INFO_LENGTH_MISMATCH, STATUS_BUFFER_TOO_SMALL, STATUS_BUFFER_OVERFLOW].contains(&asked);
    if !sized || size < size_of::<UNICODE_STRING>() as u32 || size > MOST_BYTES {
        return None;
    }
    let mut buffer = vec![0u64; (size as usize).div_ceil(8)];
    // SAFETY: the buffer holds size bytes, aligned for the UNICODE_STRING the call writes at its head.
    if unsafe { NtQueryInformationProcess(process, ProcessCommandLineInformation, buffer.as_mut_ptr().cast(), size, &mut size) } < 0 {
        return None;
    }
    // SAFETY: the string's buffer points into the same buffer, which outlives this read; its length is checked
    // against the buffer's end first.
    unsafe {
        let named = &*buffer.as_ptr().cast::<UNICODE_STRING>();
        let start = named.Buffer as usize;
        let (head, end) = (buffer.as_ptr() as usize, buffer.as_ptr() as usize + buffer.len() * 8);
        if named.Buffer.is_null() || start < head || start + usize::from(named.Length) > end {
            return None;
        }
        Some(std::slice::from_raw_parts(named.Buffer, usize::from(named.Length) / 2).to_vec())
    }
}

// *command*'s arguments as CommandLineToArgvW reads them. An empty command line has none: CommandLineToArgvW would
// give this program's own path for it.
fn split(command: &[u16]) -> Vec<String> {
    if command.iter().all(|&unit| unit == u16::from(b' ') || unit == u16::from(b'\t')) {
        return Vec::new();
    }
    let line: Vec<u16> = command.iter().copied().chain(std::iter::once(0)).collect();
    let mut count = 0i32;
    // SAFETY: the line is NUL-terminated; the array the call gives is freed below.
    let argv = unsafe { CommandLineToArgvW(line.as_ptr(), &mut count) };
    if argv.is_null() {
        return Vec::new();
    }
    let args = (0..count.max(0) as usize)
        .map(|index| {
            // SAFETY: each of the count entries is a NUL-terminated wide string in the array's own block.
            unsafe {
                let arg = *argv.add(index);
                let length = (0..).take_while(|&at| *arg.add(at) != 0).count();
                text(std::slice::from_raw_parts(arg, length))
            }
        })
        .collect();
    // SAFETY: the array CommandLineToArgvW gave, freed once.
    unsafe { LocalFree(argv.cast()) };
    args
}
