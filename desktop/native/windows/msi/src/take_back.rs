//! `TakeBackDataFolders`: the install's folders of `%ProgramData%` that were there before it, taken back by handle
//! (folders.rs). Its CustomActionData is `[DataFolder]`, `%ProgramData%\Surogate\`. As LocalSystem, with the privileges
//! to take ownership and to restore, each entry is opened by a backup-semantics handle that follows no reparse point, so
//! a link is seen as a link and removed, never followed: its folder's owner and access list are set on that handle, and
//! every other entry is deleted through one. Each descriptor is set whole, as it is written, on that one entry: none is
//! carried to what a folder holds, which its maker's access rules may keep the install from reading, nor read from the
//! folder above it. A folder that is not there is left for the install to make. Any step that fails fails the install,
//! which then changes nothing.

use std::ptr::{null, null_mut};

use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, LocalFree, ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND, HANDLE, INVALID_HANDLE_VALUE, LUID};
use windows_sys::Win32::Security::Authorization::{ConvertStringSecurityDescriptorToSecurityDescriptorW, SDDL_REVISION_1};
use windows_sys::Win32::Security::{
    AdjustTokenPrivileges, LookupPrivilegeValueW, SetKernelObjectSecurity, DACL_SECURITY_INFORMATION, LUID_AND_ATTRIBUTES, OWNER_SECURITY_INFORMATION,
    PSECURITY_DESCRIPTOR, SE_PRIVILEGE_ENABLED, TOKEN_ADJUST_PRIVILEGES, TOKEN_PRIVILEGES,
};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, FindClose, FindFirstFileW, FindNextFileW, GetFileInformationByHandle, SetFileInformationByHandle, FileBasicInfo, FileDispositionInfoEx,
    BY_HANDLE_FILE_INFORMATION, DELETE, FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_NORMAL, FILE_BASIC_INFO, FILE_WRITE_ATTRIBUTES, FILE_ATTRIBUTE_REPARSE_POINT, FILE_DISPOSITION_FLAG_DELETE, FILE_DISPOSITION_FLAG_IGNORE_READONLY_ATTRIBUTE,
    FILE_DISPOSITION_FLAG_POSIX_SEMANTICS, FILE_DISPOSITION_INFO_EX, FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_LIST_DIRECTORY,
    FILE_READ_ATTRIBUTES, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING, READ_CONTROL, WIN32_FIND_DATAW, WRITE_DAC, WRITE_OWNER,
};
use windows_sys::Win32::System::ApplicationInstallationAndServicing::{MsiGetPropertyW, MSIHANDLE};
use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};

use crate::action::{log, wide};
use crate::folders::{found, Found, APPLY_SDDL, DATA_SDDL, RECORD_SDDL};

// What is removed is SYSTEM's first, so that what it holds can be listed whatever its access rules said.
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

/// The privileges a backup-semantics handle needs to be given any right, and an owner to be set to SYSTEM.
fn enable_privileges() -> Result<(), String> {
    let mut token: HANDLE = null_mut();
    if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES, &mut token) } == 0 {
        return Err(last_error("the action's token could not be opened"));
    }
    let token = Handle(token);
    for name in ["SeTakeOwnershipPrivilege", "SeRestorePrivilege", "SeBackupPrivilege"] {
        let mut luid = LUID { LowPart: 0, HighPart: 0 };
        if unsafe { LookupPrivilegeValueW(null(), wide(name).as_ptr(), &mut luid) } == 0 {
            return Err(last_error(name));
        }
        let privileges = TOKEN_PRIVILEGES { PrivilegeCount: 1, Privileges: [LUID_AND_ATTRIBUTES { Luid: luid, Attributes: SE_PRIVILEGE_ENABLED }] };
        if unsafe { AdjustTokenPrivileges(token.0, 0, &privileges, 0, null_mut(), null_mut()) } == 0 || unsafe { GetLastError() } != 0 {
            return Err(last_error(name));
        }
    }
    Ok(())
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

/// *handle*'s owner and access list made those of *sddl*, its protection and its inherited rules as the descriptor
/// says them: on that one entry, and on nothing it holds.
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

/// The names in the folder *path*, but `.` and `..`, each with whether it is a folder and whether it is a link.
fn entries(path: &str) -> Result<Vec<(String, bool, bool)>, String> {
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
            names.push((name, data.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY != 0, data.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT != 0));
        }
        if unsafe { FindNextFileW(find, &mut data) } == 0 {
            break;
        }
    }
    unsafe { FindClose(find) };
    Ok(names)
}

/// *path* deleted, a link as a link and a folder with all it holds, whatever its attributes and access rules.
fn remove(path: &str) -> Result<(), String> {
    let access = READ_CONTROL | WRITE_DAC | WRITE_OWNER | DELETE | FILE_READ_ATTRIBUTES | FILE_LIST_DIRECTORY;
    let Some((handle, attributes)) = open(path, access)? else { return Ok(()) };
    if attributes & FILE_ATTRIBUTE_DIRECTORY != 0 && attributes & FILE_ATTRIBUTE_REPARSE_POINT == 0 {
        secure(&handle, SYSTEM_ALONE, path)?;
        for (name, _, _) in entries(path)? {
            remove(&format!("{path}\\{name}"))?;
        }
    }
    let disposition = FILE_DISPOSITION_INFO_EX { Flags: FILE_DISPOSITION_FLAG_DELETE | FILE_DISPOSITION_FLAG_POSIX_SEMANTICS | FILE_DISPOSITION_FLAG_IGNORE_READONLY_ATTRIBUTE };
    let size = std::mem::size_of::<FILE_DISPOSITION_INFO_EX>() as u32;
    if unsafe { SetFileInformationByHandle(handle.0, FileDispositionInfoEx, (&disposition as *const FILE_DISPOSITION_INFO_EX).cast(), size) } == 0 {
        return Err(last_error(&format!("{path} could not be removed")));
    }
    Ok(())
}

/// The folder *path* the install's, if it is there: a link or a file in its place removed; else its owner and access
/// list *sddl*'s, and what it holds taken back or removed by *found*. What was done, a line.
fn take_back_folder(path: &str, sddl: &str, in_apply: bool) -> Result<String, String> {
    let access = READ_CONTROL | WRITE_DAC | WRITE_OWNER | DELETE | FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES;
    let Some((handle, attributes)) = open(path, access)? else { return Ok(format!("{path} was not there")) };
    if attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 || attributes & FILE_ATTRIBUTE_DIRECTORY == 0 {
        drop(handle);
        remove(path)?;
        return Ok(format!("{path} was a link or a file, and is removed"));
    }
    secure(&handle, sddl, path)?;
    let mut said = vec![format!("{path} is SYSTEM's again")];
    for (name, folder, link) in entries(path)? {
        let inner = format!("{path}\\{name}");
        match found(&name, in_apply, folder, link) {
            Found::Apply => said.push(take_back_folder(&inner, APPLY_SDDL, true)?),
            Found::Record => {
                let Some((record, _)) = open(&inner, READ_CONTROL | WRITE_DAC | WRITE_OWNER | FILE_READ_ATTRIBUTES | FILE_WRITE_ATTRIBUTES)? else { continue };
                secure(&record, RECORD_SDDL, &inner)?;
                // A plain file, which the install writes again whole: no attribute of its maker's, read-only among them.
                let basic = FILE_BASIC_INFO { CreationTime: 0, LastAccessTime: 0, LastWriteTime: 0, ChangeTime: 0, FileAttributes: FILE_ATTRIBUTE_NORMAL };
                let size = std::mem::size_of::<FILE_BASIC_INFO>() as u32;
                if unsafe { SetFileInformationByHandle(record.0, FileBasicInfo, (&basic as *const FILE_BASIC_INFO).cast(), size) } == 0 {
                    return Err(last_error(&format!("{inner}'s attributes could not be set")));
                }
                said.push(format!("{inner} is SYSTEM's again"));
            }
            Found::Remove => {
                remove(&inner)?;
                said.push(format!("{inner} is removed"));
            }
        }
    }
    Ok(said.join("; "))
}

/// The custom action: the install's folders of `%ProgramData%` taken back where they were there before it.
#[no_mangle]
pub extern "system" fn TakeBackDataFolders(install: MSIHANDLE) -> u32 {
    let data = custom_action_data(install);
    let data = data.trim_end_matches('\\');
    if data.is_empty() {
        log(install, "Surogate: the install's folder of ProgramData was not named");
        return ERROR_INSTALL_FAILURE;
    }
    // An apply that is not there, or that was a link and is removed, is the install's to make.
    let done = enable_privileges().and_then(|()| take_back_folder(data, DATA_SDDL, false));
    match done {
        Ok(said) => {
            log(install, &format!("Surogate: {said}"));
            0
        }
        Err(why) => {
            log(install, &format!("Surogate: the install's folders of ProgramData could not be taken back: {why}"));
            ERROR_INSTALL_FAILURE
        }
    }
}
