//! Where Windows says a program is installed: the default value of its App Paths key (Microsoft, "Application
//! Registration", HKEY_LOCAL_MACHINE and HKEY_CURRENT_USER\SOFTWARE\Microsoft\Windows\CurrentVersion\App
//! Paths\<file name>), as a browser installed outside its usual folder records it. The machine's first, then the
//! user's; a REG_SZ alone (RegGetValueW, RRF_RT_REG_SZ): an expanded one would name the user's environment.

use windows_sys::Win32::Foundation::ERROR_SUCCESS;
use windows_sys::Win32::System::Registry::{RegGetValueW, HKEY, HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE, RRF_RT_REG_SZ};

use super::{text, wide};
use crate::answers::{program_name, unquoted};

/// The paths the App Paths keys give the program *name*, HKLM's then HKCU's, each where there is one.
pub fn paths(name: &str) -> Result<Vec<String>, String> {
    if !program_name(name) {
        return Err(format!("{name} is not a program's file name"));
    }
    let key = wide(format!("SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\{name}"));
    Ok([HKEY_LOCAL_MACHINE, HKEY_CURRENT_USER].into_iter().filter_map(|root| value(root, &key)).collect())
}

// The default value of *key* under *root*, a whole path's worth at most.
fn value(root: HKEY, key: &[u16]) -> Option<String> {
    let mut data = [0u16; 32_768];
    let mut size = (data.len() * 2) as u32;
    // SAFETY: the key is NUL-terminated, and size is the buffer's own, in bytes.
    let status = unsafe { RegGetValueW(root, key.as_ptr(), std::ptr::null(), RRF_RT_REG_SZ, std::ptr::null_mut(), data.as_mut_ptr().cast(), &mut size) };
    if status != ERROR_SUCCESS {
        return None;
    }
    let found = text(&data[..(size as usize / 2).min(data.len())]);
    let path = unquoted(&found);
    (!path.is_empty()).then(|| path.to_string())
}
