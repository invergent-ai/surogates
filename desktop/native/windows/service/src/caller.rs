// Who is at the other end of the service's pipe, by the pipe's own word and the caller's token, never by what the
// caller says. The pipe's descriptor lets in SYSTEM and interactive users alone, and no other computer
// (server.rs); this looks again, at the token itself:
// - the process and its session (GetNamedPipeClientProcessId, GetNamedPipeClientSessionId): session 0 is services';
// - the token, taken by impersonating the client once a line of theirs has been read (ImpersonateNamedPipeClient:
//   "Unable to impersonate using a named pipe until data has been read from that pipe", ERROR_CANNOT_IMPERSONATE),
//   at once reverted to the service's own (RevertToSelf);
// - an impersonation level that lets the service act as the caller (TokenImpersonationLevel, SecurityImpersonation);
// - INTERACTIVE among its groups and NETWORK not (CheckTokenMembership, WinInteractiveSid, WinNetworkSid);
// - an integrity level of at least medium (TokenIntegrityLevel), so a low-integrity process of the user's, as a
//   browser's sandboxed one, is not taken for the user;
// - its user's SID (TokenUser, ConvertSidToStringSidW) and profile folder (GetUserProfileDirectoryW).

use std::io;

use pipe::win::{Handle, Pipe};
use windows_sys::Win32::Foundation::{LocalFree, HANDLE};
use windows_sys::Win32::Security::Authorization::ConvertSidToStringSidW;
use windows_sys::Win32::Security::{
    CheckTokenMembership, CreateWellKnownSid, GetSidSubAuthority, GetSidSubAuthorityCount, GetTokenInformation, RevertToSelf, SecurityImpersonation,
    TokenImpersonationLevel, TokenIntegrityLevel, TokenUser, WinInteractiveSid, WinNetworkSid, SECURITY_MAX_SID_SIZE, TOKEN_DUPLICATE, TOKEN_IMPERSONATE,
    TOKEN_INFORMATION_CLASS, TOKEN_MANDATORY_LABEL, TOKEN_QUERY, TOKEN_USER, WELL_KNOWN_SID_TYPE,
};
use windows_sys::Win32::System::Pipes::ImpersonateNamedPipeClient;
use windows_sys::Win32::System::Threading::{GetCurrentThread, OpenThreadToken, SetThreadToken};
use windows_sys::Win32::UI::Shell::GetUserProfileDirectoryW;

use crate::core::Caller;

// SECURITY_MANDATORY_MEDIUM_RID (winnt.h): the integrity level of a user's own processes.
const MEDIUM: u32 = 0x2000;

/// The caller, with the token the service acts as it with.
pub struct Identified {
    pub caller: Caller,
    pub token: Handle,
    pub profile: String,
}

fn token_information(token: HANDLE, class: TOKEN_INFORMATION_CLASS) -> io::Result<Vec<u64>> {
    let mut needed = 0u32;
    unsafe { GetTokenInformation(token, class, std::ptr::null_mut(), 0, &mut needed) };
    // u64s, so the structures read from it are aligned.
    let mut buffer = vec![0u64; (needed as usize).div_ceil(8).max(1)];
    if unsafe { GetTokenInformation(token, class, buffer.as_mut_ptr().cast(), (buffer.len() * 8) as u32, &mut needed) } == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(buffer)
}

fn member(token: HANDLE, kind: WELL_KNOWN_SID_TYPE) -> io::Result<bool> {
    let mut sid = [0u8; SECURITY_MAX_SID_SIZE as usize];
    let mut size = SECURITY_MAX_SID_SIZE;
    if unsafe { CreateWellKnownSid(kind, std::ptr::null_mut(), sid.as_mut_ptr().cast(), &mut size) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let mut is = 0;
    if unsafe { CheckTokenMembership(token, sid.as_mut_ptr().cast(), &mut is) } == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(is != 0)
}

/// Whether the token is of a caller the service serves: why not, if not.
fn judged(token: HANDLE) -> Result<(), String> {
    let failed = |error: io::Error| format!("the service could not read its caller's token: {error}");
    let level = token_information(token, TokenImpersonationLevel).map_err(failed)?;
    if (level[0] as u32 as i32) < SecurityImpersonation {
        return Err("a caller that does not let the service act as it".into());
    }
    if !member(token, WinInteractiveSid).map_err(failed)? {
        return Err("a caller that is not an interactive user".into());
    }
    if member(token, WinNetworkSid).map_err(failed)? {
        return Err("a caller from the network".into());
    }
    let label = token_information(token, TokenIntegrityLevel).map_err(failed)?;
    let integrity = unsafe {
        let label = &*(label.as_ptr() as *const TOKEN_MANDATORY_LABEL);
        let count = *GetSidSubAuthorityCount(label.Label.Sid);
        if count == 0 {
            0
        } else {
            *GetSidSubAuthority(label.Label.Sid, u32::from(count) - 1)
        }
    };
    if integrity < MEDIUM {
        return Err("a caller below a user's own integrity level".into());
    }
    Ok(())
}

fn sid_of(token: HANDLE) -> io::Result<String> {
    let user = token_information(token, TokenUser)?;
    unsafe {
        let user = &*(user.as_ptr() as *const TOKEN_USER);
        let mut text: *mut u16 = std::ptr::null_mut();
        if ConvertSidToStringSidW(user.User.Sid, &mut text) == 0 {
            return Err(io::Error::last_os_error());
        }
        let mut length = 0;
        while *text.add(length) != 0 {
            length += 1;
        }
        let sid = String::from_utf16_lossy(std::slice::from_raw_parts(text, length));
        LocalFree(text.cast());
        Ok(sid)
    }
}

fn profile_of(token: HANDLE) -> io::Result<String> {
    let mut size = 0u32;
    unsafe { GetUserProfileDirectoryW(token, std::ptr::null_mut(), &mut size) };
    let mut buffer = vec![0u16; size.max(1) as usize];
    if unsafe { GetUserProfileDirectoryW(token, buffer.as_mut_ptr(), &mut size) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let end = buffer.iter().position(|&unit| unit == 0).unwrap_or(buffer.len());
    Ok(String::from_utf16_lossy(&buffer[..end]))
}

/// The caller at *pipe*'s other end, once a line of theirs has been read; or why the service does not serve them.
pub fn identify(pipe: &Pipe) -> Result<Identified, String> {
    let (pid, session) = pipe.client().map_err(|error| format!("the service could not tell who its caller is: {error}"))?;
    if session == 0 {
        return Err("a caller in session 0, a service's, not an interactive user's".into());
    }
    let mut token: HANDLE = std::ptr::null_mut();
    unsafe {
        if ImpersonateNamedPipeClient(pipe.raw()) == 0 {
            return Err(format!("the service could not act as its caller: {}", io::Error::last_os_error()));
        }
        let opened = OpenThreadToken(GetCurrentThread(), TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_IMPERSONATE, 1, &mut token);
        let error = io::Error::last_os_error();
        RevertToSelf();
        if opened == 0 {
            return Err(format!("the service could not read its caller's token: {error}"));
        }
    }
    let token = Handle(token);
    judged(token.0)?;
    let sid = sid_of(token.0).map_err(|error| format!("the service could not read its caller's user: {error}"))?;
    if !hcs::is_sid(&sid) {
        return Err("a caller whose user is not a SID the service takes".into());
    }
    let profile = profile_of(token.0).map_err(|error| format!("the service could not find its caller's profile folder: {error}"))?;
    Ok(Identified { caller: Caller { sid, pid }, token, profile })
}

/// *act*, run on this thread as the caller of *token*, then as the service again, whatever it did.
pub fn as_caller<T>(token: &Handle, act: impl FnOnce() -> T) -> Result<T, String> {
    if unsafe { SetThreadToken(std::ptr::null(), token.0) } == 0 {
        return Err(format!("the service could not act as its caller: {}", io::Error::last_os_error()));
    }
    let done = act();
    unsafe { RevertToSelf() };
    Ok(done)
}
