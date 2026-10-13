//! Whether a folder is there, as the app's checks look at one: opened through its links, as CreateFileW opens a path
//! without FILE_FLAG_OPEN_REPARSE_POINT, and a folder only with FILE_FLAG_BACKUP_SEMANTICS (Microsoft, CreateFileW,
//! "Directories"), for no access but its attributes. A link that leads nowhere is not there. A share that has stopped
//! answering holds the open in this process alone, which the app ends once its look's time is up.

use std::ffi::OsStr;

use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, FILE_FLAG_BACKUP_SEMANTICS, FILE_READ_ATTRIBUTES, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING,
};

use super::wide;

/// Whether *path* is there, through its links.
pub fn there(path: &OsStr) -> bool {
    let path = wide(path);
    // SAFETY: the path is NUL-terminated; a handle opened is closed at once.
    unsafe {
        let handle = CreateFileW(
            path.as_ptr(), FILE_READ_ATTRIBUTES, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, std::ptr::null(), OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS, std::ptr::null_mut(),
        );
        if handle == INVALID_HANDLE_VALUE {
            return false;
        }
        CloseHandle(handle);
        true
    }
}
