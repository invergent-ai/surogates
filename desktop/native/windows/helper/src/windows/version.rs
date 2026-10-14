//! A program's version, from its version resource's fixed part (Microsoft, GetFileVersionInfoSizeW,
//! GetFileVersionInfoW, VerQueryValueW with "\\", VS_FIXEDFILEINFO), as Explorer's Details tab shows its product
//! version: read from the file, so the program is never started to ask it. A browser on Windows starts a window
//! of its own for `--version` rather than printing one.

use std::ffi::OsStr;

use windows_sys::Win32::Storage::FileSystem::{GetFileVersionInfoSizeW, GetFileVersionInfoW, VerQueryValueW, VS_FIXEDFILEINFO};

use super::wide;
use crate::answers::version;

// VS_FIXEDFILEINFO's dwSignature (Microsoft, VS_FIXEDFILEINFO).
const SIGNATURE: u32 = 0xFEEF_04BD;

/// The product version of the program at *path*, or None where it has no version resource.
pub fn of(path: &OsStr) -> Option<String> {
    let path = wide(path);
    let mut ignored = 0u32;
    // SAFETY: the path is NUL-terminated.
    let size = unsafe { GetFileVersionInfoSizeW(path.as_ptr(), &mut ignored) };
    if size == 0 {
        return None;
    }
    let mut block = vec![0u64; (size as usize).div_ceil(8)];
    let root = wide("\\");
    let mut fixed: *mut core::ffi::c_void = std::ptr::null_mut();
    let mut length = 0u32;
    // SAFETY: the block holds size bytes; VerQueryValueW points into it, read while it lives.
    unsafe {
        if GetFileVersionInfoW(path.as_ptr(), 0, size, block.as_mut_ptr().cast()) == 0
            || VerQueryValueW(block.as_ptr().cast(), root.as_ptr(), &mut fixed, &mut length) == 0
            || fixed.is_null()
            || (length as usize) < size_of::<VS_FIXEDFILEINFO>()
        {
            return None;
        }
        let info = &*fixed.cast::<VS_FIXEDFILEINFO>();
        (info.dwSignature == SIGNATURE).then(|| version(info.dwProductVersionMS, info.dwProductVersionLS))
    }
}
