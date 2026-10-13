// The disk as the caller reaches it. Each path a request names is reached here as the caller (caller.rs, as_caller),
// never as the service, by a walk from a folder the caller is known to own down to the path, one component at a time:
// each component is opened without following a reparse point (FILE_FLAG_OPEN_REPARSE_POINT), and a reparse point on
// any component -- a junction or a symbolic link the caller could have planted on the way -- is refused, so the
// service never goes through one. Each component is opened before the next and held, each without delete sharing, so
// nothing below the one held can be renamed or swapped: a deeper component is re-resolved only through parents that
// are already pinned. The handles of the whole chain are kept from the check until the Host Compute Service has
// opened the file, so nothing on the path can change in between (the file host pins a tree the same way while rg
// runs). The whole canonical path (GetFinalPathNameByHandleW, FILE_NAME_NORMALIZED, VOLUME_NAME_DOS) must be the path
// walked, ASCII case aside, so a short name on any component is refused too; a drive that is another computer's is
// refused.

use std::io;

use pipe::path::within;
use pipe::win::{wide, Handle};
use windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE;
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, GetFileInformationByHandle, GetFinalPathNameByHandleW, BY_HANDLE_FILE_INFORMATION, FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT,
    FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_LIST_DIRECTORY, FILE_NAME_NORMALIZED, FILE_SHARE_READ, FILE_SHARE_WRITE, FILE_TRAVERSE,
    OPEN_EXISTING, SYNCHRONIZE, VOLUME_NAME_DOS,
};
use windows_sys::Win32::System::LibraryLoader::GetModuleFileNameW;

use crate::caller::as_caller;
use crate::core::{Checked, Files, Kind};

const GENERIC_READ: u32 = 0x8000_0000;
const GENERIC_WRITE: u32 = 0x4000_0000;
// A directory opened only to walk through it and list it.
const DIR_ACCESS: u32 = FILE_LIST_DIRECTORY | FILE_TRAVERSE | SYNCHRONIZE;
// Shared for read and write, never delete: a component held this way cannot be renamed or removed while the chain
// lives, so nothing on the path can be swapped between the check and the Host Compute Service's open.
const SHARE: u32 = FILE_SHARE_READ | FILE_SHARE_WRITE;

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

    fn check(&self, path: &str, kind: Kind) -> Result<Checked, String> {
        // The walk starts at a folder the caller is known to own: the installed app's folder for its agent disk, else
        // the caller's own profile. The core has already confined *path* to one of them; the walk opens every
        // component below it.
        let root = match self.installed.as_deref() {
            Some(install) if within(path, install) => install,
            _ => &self.profile,
        };
        let held = as_caller(self.token, || reached(root, path, kind))??;
        Ok(Checked::hold(Box::new(held)))
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

fn attributes(handle: &Handle) -> io::Result<u32> {
    let mut information: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
    if unsafe { GetFileInformationByHandle(handle.0, &mut information) } == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(information.dwFileAttributes)
}

// *full*, opened with *access* and *flags*, following no reparse point at its last name, shared for read and write
// but never delete. A deeper component is opened only after its parents are held, so the only reparse point that can
// be reached is this last name's own, which is opened (not followed) and refused by its caller.
fn open(full: &str, access: u32, flags: u32) -> Result<Handle, String> {
    use windows_sys::Win32::Foundation::{ERROR_ACCESS_DENIED, ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND};
    let opened = unsafe { CreateFileW(wide(full).as_ptr(), access, SHARE, std::ptr::null(), OPEN_EXISTING, flags | FILE_FLAG_OPEN_REPARSE_POINT, std::ptr::null_mut()) };
    if opened == INVALID_HANDLE_VALUE {
        let error = io::Error::last_os_error();
        return Err(match error.raw_os_error().map(|code| code as u32) {
            Some(ERROR_ACCESS_DENIED) => "the caller may not reach it".into(),
            Some(ERROR_FILE_NOT_FOUND | ERROR_PATH_NOT_FOUND) => "it is not there".into(),
            _ => format!("it could not be opened as the caller: {error}"),
        });
    }
    Ok(Handle(opened))
}

/// The handles reached() held across the Host Compute Service's open: the whole chain from the root to the file, each
/// without delete sharing. Closed, in order, when this is dropped.
pub struct Held(#[allow(dead_code)] Vec<Handle>);

/// The caller (the thread's own token) reaches *path* as *kind* through no reparse point, walking from *root* down,
/// and the chain's handles, held so nothing on the path can be swapped before HCS opens it. Else why not.
pub fn reached(root: &str, path: &str, kind: Kind) -> Result<Held, String> {
    if !within(path, root) {
        return Err("a path outside the folder it is checked under".into());
    }
    // The root the caller is known to own, opened by its whole path: a directory, not a reparse point. Its own
    // ancestors are the system's (the profile root, Program Files), not the caller's to redirect.
    let root_handle = open(root, DIR_ACCESS, FILE_FLAG_BACKUP_SEMANTICS).map_err(|why| format!("the folder it is checked under: {why}"))?;
    if attributes(&root_handle).map_err(|error| format!("the folder it is checked under could not be looked at: {error}"))? & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err("the folder it is checked under is a link or another reparse point".into());
    }
    let mut chain = vec![root_handle];
    let rest = path[root.len()..].trim_matches('\\');
    let components: Vec<&str> = if rest.is_empty() { Vec::new() } else { rest.split('\\').collect() };
    let mut prefix = root.trim_end_matches('\\').to_owned();
    for (n, component) in components.iter().enumerate() {
        prefix.push('\\');
        prefix.push_str(component);
        let last = n + 1 == components.len();
        let (access, flags) = if last {
            match kind {
                Kind::File => (GENERIC_READ, 0),
                Kind::Written => (GENERIC_READ | GENERIC_WRITE, 0),
                Kind::Folder => (DIR_ACCESS, FILE_FLAG_BACKUP_SEMANTICS),
            }
        } else {
            (DIR_ACCESS, FILE_FLAG_BACKUP_SEMANTICS)
        };
        let handle = open(&prefix, access, flags).map_err(|why| format!("{component}: {why}"))?;
        let got = attributes(&handle).map_err(|error| format!("{component} could not be looked at: {error}"))?;
        if got & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return Err(format!("a link or another reparse point on the way, at {component}"));
        }
        if last {
            let folder = got & FILE_ATTRIBUTE_DIRECTORY != 0;
            if folder != (kind == Kind::Folder) {
                return Err(if folder { "it is a folder, not a file".into() } else { "it is not a folder".into() });
            }
        }
        chain.push(handle);
    }
    // What was opened, by its whole canonical path, must be the path walked, ASCII case aside: a short name on any
    // component (GIT~1) expands to its long form here and is refused. The base is the root handle's own canonical
    // path, so the comparison does not depend on how the caller spelled the root; the components are the caller's.
    let base = final_path(&chain[0]).map_err(|error| format!("the folder's path could not be read: {error}"))?;
    let base = base.strip_prefix(r"\\?\").unwrap_or(&base);
    if base.starts_with(r"UNC\") {
        return Err("it is on another computer's drive".into());
    }
    let mut expected = base.trim_end_matches('\\').to_owned();
    for component in &components {
        expected.push('\\');
        expected.push_str(component);
    }
    let real = final_path(chain.last().unwrap()).map_err(|error| format!("its path could not be read: {error}"))?;
    let real = real.strip_prefix(r"\\?\").unwrap_or(&real);
    if !(real.len() == expected.len() && real.eq_ignore_ascii_case(&expected)) {
        return Err("a path whose file Windows names otherwise, a short name among them".into());
    }
    Ok(Held(chain))
}
