//! `TakeBackDataFolders`: `%ProgramData%\Surogate`, where it was there before the install, removed whole, so that the
//! install makes its own (folders.rs). Its CustomActionData is `[DataFolder]`, `%ProgramData%\Surogate\`. As
//! LocalSystem, with the privileges to take ownership, to restore and to back up enabled in its token, and read back
//! from it as enabled, each entry is opened by a backup-semantics handle that follows no reparse point, so that a link
//! is seen as a link and removed, never followed, and every entry is deleted through one, whatever its maker's access
//! rules say: a folder is made SYSTEM's alone first, on that one handle, so that what it holds can be listed. Then
//! nothing is there, and the install makes the folder itself. Any step that fails fails the install.

use std::ptr::{null, null_mut};

use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, LocalFree, ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND, HANDLE, INVALID_HANDLE_VALUE, LUID};
use windows_sys::Win32::Security::Authorization::{ConvertStringSecurityDescriptorToSecurityDescriptorW, SDDL_REVISION_1};
use windows_sys::Win32::Security::{
    AdjustTokenPrivileges, GetTokenInformation, LookupPrivilegeValueW, SetKernelObjectSecurity, TokenPrivileges, DACL_SECURITY_INFORMATION, LUID_AND_ATTRIBUTES,
    OWNER_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR, SE_PRIVILEGE_ENABLED, TOKEN_ADJUST_PRIVILEGES, TOKEN_PRIVILEGES, TOKEN_QUERY,
};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, FindClose, FindFirstFileW, FindNextFileW, GetFileInformationByHandle, SetFileInformationByHandle, FileDispositionInfoEx,
    BY_HANDLE_FILE_INFORMATION, DELETE, FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT, FILE_DISPOSITION_FLAG_DELETE, FILE_DISPOSITION_FLAG_IGNORE_READONLY_ATTRIBUTE,
    FILE_DISPOSITION_FLAG_POSIX_SEMANTICS, FILE_DISPOSITION_INFO_EX, FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_LIST_DIRECTORY,
    FILE_READ_ATTRIBUTES, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING, READ_CONTROL, WIN32_FIND_DATAW, WRITE_DAC, WRITE_OWNER,
};
use windows_sys::Win32::System::ApplicationInstallationAndServicing::{MsiGetPropertyW, MSIHANDLE};
use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};

use crate::action::{log, wide};
use crate::folders::{enabled, PRIVILEGES};

// A folder to remove is SYSTEM's first, so that what it holds can be listed whatever its access rules said.
const SYSTEM_ALONE: &str = "O:SYG:SYD:P(A;OICI;FA;;;SY)";

const ERROR_INSTALL_FAILURE: u32 = 1603;

struct Handle(HANDLE);

impl Drop for Handle {
    fn drop(&mut self) {
        unsafe { CloseHandle(self.0) };
    }
}

fn last_error(what: &str) -> String {
    format!("{what}: Windows error {}", unsafe { GetLastError() })
}

/// The deferred action's own data: the folder it was given.
fn custom_action_data(install: MSIHANDLE) -> String {
    let name = wide("CustomActionData");
    let mut buffer = vec![0u16; 1024];
    let mut length = buffer.len() as u32;
    let status = unsafe { MsiGetPropertyW(install, name.as_ptr(), buffer.as_mut_ptr(), &mut length) };
    if status != 0 {
        return String::new();
    }
    String::from_utf16_lossy(&buffer[..length as usize])
}

/// The privileges a backup-semantics handle needs to be given any right, and an owner to be set to SYSTEM, enabled in
/// the action's token, and each read back from the token as enabled: what was read, a line each.
fn enable_privileges() -> Result<Vec<String>, String> {
    let mut token: HANDLE = null_mut();
    if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, &mut token) } == 0 {
        return Err(last_error("the action's token could not be opened"));
    }
    let token = Handle(token);
    let mut luids = Vec::new();
    for name in PRIVILEGES {
        let mut luid = LUID { LowPart: 0, HighPart: 0 };
        if unsafe { LookupPrivilegeValueW(null(), wide(name).as_ptr(), &mut luid) } == 0 {
            return Err(last_error(name));
        }
        let privileges = TOKEN_PRIVILEGES { PrivilegeCount: 1, Privileges: [LUID_AND_ATTRIBUTES { Luid: luid, Attributes: SE_PRIVILEGE_ENABLED }] };
        // ERROR_NOT_ALL_ASSIGNED where the token does not hold it: AdjustTokenPrivileges succeeds all the same.
        if unsafe { AdjustTokenPrivileges(token.0, 0, &privileges, 0, null_mut(), null_mut()) } == 0 || unsafe { GetLastError() } != 0 {
            return Err(last_error(&format!("{name} could not be enabled")));
        }
        luids.push((name, luid));
    }
    // The token's own list, in u32s: TOKEN_PRIVILEGES's alignment.
    let mut length = 0u32;
    unsafe { GetTokenInformation(token.0, TokenPrivileges, null_mut(), 0, &mut length) };
    let mut list = vec![0u32; (length as usize).div_ceil(4)];
    if unsafe { GetTokenInformation(token.0, TokenPrivileges, list.as_mut_ptr().cast(), (list.len() * 4) as u32, &mut length) } == 0 {
        return Err(last_error("the action's token's privileges could not be read"));
    }
    luids
        .into_iter()
        .map(|(name, luid)| match enabled(&list, luid.LowPart, luid.HighPart) {
            Some(true) => Ok(format!("{name} is enabled in the action's token")),
            Some(false) => Err(format!("{name} is not enabled in the action's token")),
            None => Err(format!("the action's token does not hold {name}")),
        })
        .collect()
}

/// *path* opened for *access*, by a handle that follows no reparse point, with its attributes; None where nothing is there.
fn open(path: &str, access: u32) -> Result<Option<(Handle, u32)>, String> {
    let handle = unsafe {
        CreateFileW(
            wide(path).as_ptr(),
            access,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            null(),
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            null_mut(),
        )
    };
    if handle == INVALID_HANDLE_VALUE {
        let error = unsafe { GetLastError() };
        if error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND {
            return Ok(None);
        }
        return Err(format!("{path} could not be opened: Windows error {error}"));
    }
    let handle = Handle(handle);
    let mut information: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
    if unsafe { GetFileInformationByHandle(handle.0, &mut information) } == 0 {
        return Err(last_error(path));
    }
    Ok(Some((handle, information.dwFileAttributes)))
}

/// *handle*'s owner and access list made those of *sddl*: on that one entry, and on nothing it holds.
fn secure(handle: &Handle, sddl: &str, path: &str) -> Result<(), String> {
    let mut descriptor: PSECURITY_DESCRIPTOR = null_mut();
    if unsafe { ConvertStringSecurityDescriptorToSecurityDescriptorW(wide(sddl).as_ptr(), SDDL_REVISION_1, &mut descriptor, null_mut()) } == 0 {
        return Err(last_error(sddl));
    }
    let set = unsafe { SetKernelObjectSecurity(handle.0, OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION, descriptor) } != 0;
    let error = unsafe { GetLastError() };
    unsafe { LocalFree(descriptor) };
    if !set {
        return Err(format!("{path}'s owner and access list could not be set: Windows error {error}"));
    }
    Ok(())
}

/// The names in the folder *path*, but `.` and `..`.
fn entries(path: &str) -> Result<Vec<String>, String> {
    let mut data: WIN32_FIND_DATAW = unsafe { std::mem::zeroed() };
    let find = unsafe { FindFirstFileW(wide(&format!("{path}\\*")).as_ptr(), &mut data) };
    if find == INVALID_HANDLE_VALUE {
        return Err(last_error(path));
    }
    let mut names = Vec::new();
    loop {
        let length = data.cFileName.iter().position(|&unit| unit == 0).unwrap_or(data.cFileName.len());
        let name = String::from_utf16_lossy(&data.cFileName[..length]);
        if name != "." && name != ".." {
            names.push(name);
        }
        if unsafe { FindNextFileW(find, &mut data) } == 0 {
            break;
        }
    }
    unsafe { FindClose(find) };
    Ok(names)
}

/// *path* deleted, a link as a link and a folder with all it holds, whatever its attributes and access rules; each
/// entry deleted, by its path, added to *removed*.
fn remove(path: &str, removed: &mut Vec<String>) -> Result<(), String> {
    let access = READ_CONTROL | WRITE_DAC | WRITE_OWNER | DELETE | FILE_READ_ATTRIBUTES | FILE_LIST_DIRECTORY;
    let Some((handle, attributes)) = open(path, access)? else { return Ok(()) };
    let link = attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0;
    if attributes & FILE_ATTRIBUTE_DIRECTORY != 0 && !link {
        secure(&handle, SYSTEM_ALONE, path)?;
        for name in entries(path)? {
            remove(&format!("{path}\\{name}"), removed)?;
        }
    }
    let disposition = FILE_DISPOSITION_INFO_EX { Flags: FILE_DISPOSITION_FLAG_DELETE | FILE_DISPOSITION_FLAG_POSIX_SEMANTICS | FILE_DISPOSITION_FLAG_IGNORE_READONLY_ATTRIBUTE };
    let size = std::mem::size_of::<FILE_DISPOSITION_INFO_EX>() as u32;
    if unsafe { SetFileInformationByHandle(handle.0, FileDispositionInfoEx, (&disposition as *const FILE_DISPOSITION_INFO_EX).cast(), size) } == 0 {
        return Err(last_error(&format!("{path} could not be removed")));
    }
    removed.push(if link { format!("{path} (a link)") } else { path.to_string() });
    Ok(())
}

/// The custom action: `%ProgramData%\Surogate` removed whole where it was there before the install.
#[no_mangle]
pub extern "system" fn TakeBackDataFolders(install: MSIHANDLE) -> u32 {
    let data = custom_action_data(install);
    let data = data.trim_end_matches('\\');
    if data.is_empty() {
        log(install, "Surogate: the install's folder of ProgramData was not named");
        return ERROR_INSTALL_FAILURE;
    }
    let mut removed = Vec::new();
    let done = enable_privileges().and_then(|enabled| {
        for line in enabled {
            log(install, &format!("Surogate: {line}"));
        }
        remove(data, &mut removed)
    });
    match done {
        Ok(()) if removed.is_empty() => {
            log(install, &format!("Surogate: {data} was not there before the install"));
            0
        }
        Ok(()) => {
            log(install, &format!("Surogate: {data} was there before the install, and is removed: {}", removed.join("; ")));
            0
        }
        Err(why) => {
            log(install, &format!("Surogate: {data}, there before the install, could not be removed: {why}"));
            ERROR_INSTALL_FAILURE
        }
    }
}
