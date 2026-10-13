// The disk as the caller reaches it. Each path a request names is opened here as the caller (caller.rs, as_caller),
// never as the service: what the caller cannot read, or write where it must, is refused with the caller's own access
// denied. The path is opened without following a reparse point at its end (FILE_FLAG_OPEN_REPARSE_POINT), refused if
// it is one (FILE_ATTRIBUTE_REPARSE_POINT), and its final path (GetFinalPathNameByHandleW, FILE_NAME_NORMALIZED,
// VOLUME_NAME_DOS) must be the path asked for, ASCII case aside: a junction, a symbolic link or a mount point on the
// way, a short name, or a drive that is another computer's, each gives another final path, and is refused.

use std::io;

use pipe::win::{wide, Handle};
use windows_sys::Win32::Foundation::{ERROR_ACCESS_DENIED, ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND, INVALID_HANDLE_VALUE};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, GetFileInformationByHandle, GetFinalPathNameByHandleW, BY_HANDLE_FILE_INFORMATION, FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT,
    FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_LIST_DIRECTORY, FILE_NAME_NORMALIZED, FILE_SHARE_DELETE, FILE_SHARE_READ,
    FILE_SHARE_WRITE, FILE_TRAVERSE, OPEN_EXISTING, SYNCHRONIZE, VOLUME_NAME_DOS,
};
use windows_sys::Win32::System::LibraryLoader::GetModuleFileNameW;

use crate::caller::as_caller;
use crate::core::{Files, Kind};

const GENERIC_READ: u32 = 0x8000_0000;
const GENERIC_WRITE: u32 = 0x4000_0000;

/// The installed app's folder, if the service runs from one: <folder>\service\surogate-service.exe, which the
/// installer puts under Program Files, where only administrators write.
pub fn installed() -> Option<String> {
    let mut buffer = vec![0u16; 32_768];
    let length = unsafe { GetModuleFileNameW(std::ptr::null_mut(), buffer.as_mut_ptr(), buffer.len() as u32) } as usize;
    if length == 0 || length >= buffer.len() {
        return None;
    }
    let program = String::from_utf16_lossy(&buffer[..length]);
    let (folder, _) = program.rsplit_once('\\')?;
    let (install, last) = folder.rsplit_once('\\')?;
    last.eq_ignore_ascii_case("service").then(|| install.to_owned())
}

/// The caller's disk.
pub struct CallerFiles<'a> {
    pub token: &'a Handle,
    pub profile: String,
    pub installed: Option<String>,
}

impl Files for CallerFiles<'_> {
    fn profile(&self) -> &str {
        &self.profile
    }

    fn installed(&self) -> Option<&str> {
        self.installed.as_deref()
    }

    fn check(&self, path: &str, kind: Kind) -> Result<(), String> {
        as_caller(self.token, || reached(path, kind))?
    }
}

fn final_path(handle: &Handle) -> io::Result<String> {
    let mut buffer = vec![0u16; 32_768];
    let length = unsafe { GetFinalPathNameByHandleW(handle.0, buffer.as_mut_ptr(), buffer.len() as u32, FILE_NAME_NORMALIZED | VOLUME_NAME_DOS) } as usize;
    if length == 0 || length >= buffer.len() {
        return Err(io::Error::last_os_error());
    }
    Ok(String::from_utf16_lossy(&buffer[..length]))
}

/// Whether the thread's own token reaches *path* as *kind*, as above.
pub fn reached(path: &str, kind: Kind) -> Result<(), String> {
    let (access, flags) = match kind {
        Kind::File => (GENERIC_READ, FILE_FLAG_OPEN_REPARSE_POINT),
        Kind::Written => (GENERIC_READ | GENERIC_WRITE, FILE_FLAG_OPEN_REPARSE_POINT),
        Kind::Folder => (FILE_LIST_DIRECTORY | FILE_TRAVERSE | SYNCHRONIZE, FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS),
    };
    let opened = unsafe { CreateFileW(wide(path).as_ptr(), access, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, std::ptr::null(), OPEN_EXISTING, flags, std::ptr::null_mut()) };
    if opened == INVALID_HANDLE_VALUE {
        let error = io::Error::last_os_error();
        return Err(match error.raw_os_error().map(|code| code as u32) {
            Some(ERROR_ACCESS_DENIED) if kind == Kind::Written => "the caller may not read and write it".into(),
            Some(ERROR_ACCESS_DENIED) => "the caller may not read it".into(),
            Some(ERROR_FILE_NOT_FOUND | ERROR_PATH_NOT_FOUND) => "it is not there".into(),
            _ => format!("it could not be opened as the caller: {error}"),
        });
    }
    let handle = Handle(opened);
    let mut information: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
    if unsafe { GetFileInformationByHandle(handle.0, &mut information) } == 0 {
        return Err(format!("it could not be looked at: {}", io::Error::last_os_error()));
    }
    if information.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err("it is a link or another reparse point".into());
    }
    let folder = information.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY != 0;
    if folder != (kind == Kind::Folder) {
        return Err(if folder { "it is a folder, not a file".into() } else { "it is not a folder".into() });
    }
    let real = final_path(&handle).map_err(|error| format!("its path could not be read: {error}"))?;
    let real = real.strip_prefix(r"\\?\").unwrap_or(&real);
    if real.starts_with(r"UNC\") {
        return Err("it is on another computer's drive".into());
    }
    // The opened file's real last name must be the one asked for, ASCII case aside: a short name (GIT~1) expands to
    // another, and is refused. The caller is impersonated, so an ancestor the caller may traverse is theirs to reach;
    // the leaf's own reparse point was refused above. Full no-reparse enforcement on every ancestor is the file
    // host's component walk (milestone D); here the open is the caller's own right.
    let leaf = |text: &str| text.rsplit('\\').next().unwrap_or(text).to_owned();
    if !leaf(real).eq_ignore_ascii_case(&leaf(path)) {
        return Err("a path whose file Windows names otherwise, a short name among them".into());
    }
    Ok(())
}
