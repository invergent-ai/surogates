// The disk as the caller reaches it. Each path a request names is reached here as the caller (caller.rs, as_caller),
// never as the service, by a walk from a folder the caller is known to own down to the path, one component at a time:
// each component is opened relative to its parent's handle without following a reparse point, and a reparse point on
// any component -- a junction or a symbolic link the caller could have planted on the way -- is refused, so the
// service never goes through one. The opened handles of the whole chain are held, each without delete sharing, from
// the check until the Host Compute Service has opened the file, so nothing on the path can be renamed or swapped in
// between (the walk's race-free form, as the file host pins a tree while rg runs). The whole canonical path
// (GetFinalPathNameByHandleW, FILE_NAME_NORMALIZED, VOLUME_NAME_DOS) must equal the path asked for, ASCII case aside,
// so a short name is refused too; and a drive that is another computer's is refused.

use std::io;
use std::mem::size_of;

use pipe::path::within;
use pipe::win::{wide, Handle};
use windows_sys::Wdk::Foundation::OBJECT_ATTRIBUTES;
use windows_sys::Wdk::Storage::FileSystem::{NtCreateFile, FILE_DIRECTORY_FILE, FILE_OPEN, FILE_OPEN_FOR_BACKUP_INTENT, FILE_OPEN_REPARSE_POINT};
use windows_sys::Win32::Foundation::{
    HANDLE, INVALID_HANDLE_VALUE, OBJ_CASE_INSENSITIVE, OBJ_DONT_REPARSE, STATUS_ACCESS_DENIED, STATUS_OBJECT_NAME_NOT_FOUND, STATUS_OBJECT_PATH_NOT_FOUND,
    STATUS_REPARSE_POINT_ENCOUNTERED, UNICODE_STRING,
};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, GetFileInformationByHandle, GetFinalPathNameByHandleW, BY_HANDLE_FILE_INFORMATION, FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT,
    FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_LIST_DIRECTORY, FILE_NAME_NORMALIZED, FILE_SHARE_READ, FILE_SHARE_WRITE, FILE_TRAVERSE,
    OPEN_EXISTING, SYNCHRONIZE, VOLUME_NAME_DOS,
};
use windows_sys::Win32::System::LibraryLoader::GetModuleFileNameW;
use windows_sys::Win32::System::IO::IO_STATUS_BLOCK;

use crate::caller::as_caller;
use crate::core::{Checked, Files, Kind};

const GENERIC_READ: u32 = 0x8000_0000;
const GENERIC_WRITE: u32 = 0x4000_0000;
// A directory opened only to walk through it and list it.
const DIR_ACCESS: u32 = FILE_LIST_DIRECTORY | FILE_TRAVERSE | SYNCHRONIZE;

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

// A component opened relative to *parent* by its *name*, with *access* and *options*, following no reparse point
// (OBJ_DONT_REPARSE, FILE_OPEN_REPARSE_POINT), shared for read and write but never delete, so it cannot be renamed or
// removed while held. The status says why not: a reparse point on the way, the caller's own access denied, or no
// such name.
fn open_at(parent: HANDLE, name: &str, access: u32, options: u32) -> Result<Handle, String> {
    let mut wide_name: Vec<u16> = name.encode_utf16().collect();
    let bytes = (wide_name.len() * 2) as u16;
    let mut object_name = UNICODE_STRING { Length: bytes, MaximumLength: bytes, Buffer: wide_name.as_mut_ptr() };
    let object = OBJECT_ATTRIBUTES {
        Length: size_of::<OBJECT_ATTRIBUTES>() as u32,
        RootDirectory: parent,
        ObjectName: &mut object_name,
        Attributes: OBJ_CASE_INSENSITIVE | OBJ_DONT_REPARSE,
        SecurityDescriptor: std::ptr::null(),
        SecurityQualityOfService: std::ptr::null(),
    };
    let mut status_block: IO_STATUS_BLOCK = unsafe { std::mem::zeroed() };
    let mut handle: HANDLE = std::ptr::null_mut();
    let status = unsafe {
        NtCreateFile(
            &mut handle,
            access,
            &object,
            &mut status_block,
            std::ptr::null(),
            0,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            FILE_OPEN,
            options | FILE_OPEN_REPARSE_POINT | FILE_OPEN_FOR_BACKUP_INTENT,
            std::ptr::null(),
            0,
        )
    };
    if status < 0 {
        return Err(match status {
            STATUS_REPARSE_POINT_ENCOUNTERED => format!("a link or another reparse point on the way, at {name}"),
            STATUS_ACCESS_DENIED => format!("the caller may not reach {name}"),
            STATUS_OBJECT_NAME_NOT_FOUND | STATUS_OBJECT_PATH_NOT_FOUND => format!("{name} is not there"),
            other => format!("{name} could not be opened as the caller: 0x{:08x}", other as u32),
        });
    }
    Ok(Handle(handle))
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
    let opened = unsafe {
        CreateFileW(
            wide(root).as_ptr(),
            DIR_ACCESS,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            std::ptr::null(),
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            std::ptr::null_mut(),
        )
    };
    if opened == INVALID_HANDLE_VALUE {
        return Err(format!("the folder it is checked under could not be opened: {}", io::Error::last_os_error()));
    }
    let mut chain = vec![Handle(opened)];
    if attributes(chain.last().unwrap()).map_err(|error| format!("the folder it is checked under could not be looked at: {error}"))? & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err("the folder it is checked under is a link or another reparse point".into());
    }
    let rest = path[root.len()..].trim_matches('\\');
    let components: Vec<&str> = if rest.is_empty() { Vec::new() } else { rest.split('\\').collect() };
    for (n, component) in components.iter().enumerate() {
        let last = n + 1 == components.len();
        let (access, options) = if last {
            match kind {
                Kind::File => (GENERIC_READ, 0),
                Kind::Written => (GENERIC_READ | GENERIC_WRITE, 0),
                Kind::Folder => (DIR_ACCESS, FILE_DIRECTORY_FILE),
            }
        } else {
            (DIR_ACCESS, FILE_DIRECTORY_FILE)
        };
        let parent = chain.last().unwrap().0;
        let handle = open_at(parent, component, access, options)?;
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
    // The whole canonical path of what was opened must be the path asked for, ASCII case aside: a short name on any
    // component (GIT~1) expands to another here, and is refused; a drive that is another computer's is refused.
    let real = final_path(chain.last().unwrap()).map_err(|error| format!("its path could not be read: {error}"))?;
    let real = real.strip_prefix(r"\\?\").unwrap_or(&real);
    if real.starts_with(r"UNC\") {
        return Err("it is on another computer's drive".into());
    }
    if !(real.len() == path.len() && real.eq_ignore_ascii_case(path)) {
        return Err("a path whose file Windows names otherwise, a short name among them".into());
    }
    Ok(Held(chain))
}
