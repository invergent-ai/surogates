//! The helper's answers from Windows's own libraries.

pub mod app_path;
pub mod look;
pub mod procs;
pub mod spawn;
pub mod trust;
pub mod verify;
pub mod version;

use std::ffi::OsStr;
use std::os::windows::ffi::OsStrExt;

/// *text* as a wide string with its NUL, as Windows's W functions take one.
pub fn wide(text: impl AsRef<OsStr>) -> Vec<u16> {
    text.as_ref().encode_wide().chain(std::iter::once(0)).collect()
}

/// The wide string *units*, up to its first NUL, as text; an unpaired surrogate becomes U+FFFD.
pub fn text(units: &[u16]) -> String {
    let end = units.iter().position(|&unit| unit == 0).unwrap_or(units.len());
    String::from_utf16_lossy(&units[..end])
}
