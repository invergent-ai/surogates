//! A program's Authenticode signature, as WinVerifyTrust checks one with the generic verify action (Microsoft,
//! WinVerifyTrust; "Example C Program: Verifying the Signature of a PE File"): embedded in the file, its chain to a
//! root this computer trusts, with no window shown (WTD_UI_NONE) and nothing fetched from the network
//! (WTD_CACHE_ONLY_URL_RETRIEVAL, no revocation check), so that a check never waits on one. Its signer is the leaf
//! certificate of the first signer's chain (WTHelperProvDataFromStateData, WTHelperGetProvSignerFromChain), named by
//! its subject's simple display name (CertGetNameStringW, CERT_NAME_SIMPLE_DISPLAY_TYPE), "Google LLC" for Chrome.
//! And whether the program is on a network volume (GetVolumePathNameW, GetDriveTypeW, DRIVE_REMOTE).

use std::ffi::OsStr;

use windows_sys::Win32::Foundation::{HWND, INVALID_HANDLE_VALUE};
use windows_sys::Win32::Security::Cryptography::{CertGetNameStringW, CERT_NAME_SIMPLE_DISPLAY_TYPE};
use windows_sys::Win32::Security::WinTrust::{
    WTHelperGetProvSignerFromChain, WTHelperProvDataFromStateData, WinVerifyTrust, WINTRUST_ACTION_GENERIC_VERIFY_V2, WINTRUST_DATA,
    WINTRUST_DATA_0, WINTRUST_FILE_INFO, WTD_CACHE_ONLY_URL_RETRIEVAL, WTD_CHOICE_FILE, WTD_REVOKE_NONE, WTD_STATEACTION_CLOSE,
    WTD_STATEACTION_VERIFY, WTD_UI_NONE,
};
use windows_sys::Win32::Storage::FileSystem::{GetDriveTypeW, GetVolumePathNameW};
use windows_sys::Win32::System::WindowsProgramming::DRIVE_REMOTE;

use super::{text, wide};
use crate::answers::Verdict;

/// What WinVerifyTrust says of the program at *path*.
pub fn verdict(path: &OsStr) -> Result<Verdict, String> {
    let path = wide(path);
    let mut file = WINTRUST_FILE_INFO { cbStruct: size_of::<WINTRUST_FILE_INFO>() as u32, pcwszFilePath: path.as_ptr(), ..Default::default() };
    let mut data = WINTRUST_DATA {
        cbStruct: size_of::<WINTRUST_DATA>() as u32,
        dwUIChoice: WTD_UI_NONE,
        fdwRevocationChecks: WTD_REVOKE_NONE,
        dwUnionChoice: WTD_CHOICE_FILE,
        Anonymous: WINTRUST_DATA_0 { pFile: &mut file },
        dwStateAction: WTD_STATEACTION_VERIFY,
        dwProvFlags: WTD_CACHE_ONLY_URL_RETRIEVAL,
        ..Default::default()
    };
    let mut action = WINTRUST_ACTION_GENERIC_VERIFY_V2;
    // INVALID_HANDLE_VALUE: no interactive user, so the provider shows nothing (Microsoft, WinVerifyTrust, hWnd).
    let window = INVALID_HANDLE_VALUE as HWND;
    // SAFETY: data and file, and the path they name, outlive both calls; the state the first opens the second closes.
    let status = unsafe { WinVerifyTrust(window, &mut action, (&raw mut data).cast()) };
    let signer = if status == 0 { signer_of(&data) } else { None };
    data.dwStateAction = WTD_STATEACTION_CLOSE;
    // SAFETY: as above.
    unsafe { WinVerifyTrust(window, &mut action, (&raw mut data).cast()) };
    Ok(Verdict { trusted: status == 0 && signer.is_some(), signer, status, remote: remote(&path) })
}

// The first signer's leaf certificate's name, from the state a verify left open.
fn signer_of(data: &WINTRUST_DATA) -> Option<String> {
    // SAFETY: each pointer is the state's own, checked before it is read, while the state is open.
    unsafe {
        let provider = WTHelperProvDataFromStateData(data.hWVTStateData);
        if provider.is_null() {
            return None;
        }
        let signer = WTHelperGetProvSignerFromChain(provider, 0, 0, 0);
        if signer.is_null() || (*signer).csCertChain == 0 || (*signer).pasCertChain.is_null() {
            return None;
        }
        let leaf = (*(*signer).pasCertChain).pCert;
        if leaf.is_null() {
            return None;
        }
        let mut name = [0u16; 512];
        let length = CertGetNameStringW(leaf, CERT_NAME_SIMPLE_DISPLAY_TYPE, 0, std::ptr::null(), name.as_mut_ptr(), name.len() as u32);
        let named = text(&name[..length as usize]);
        (!named.is_empty()).then_some(named)
    }
}

// Whether the NUL-terminated *path* is on a network volume: a share's, or a drive mapped to one.
fn remote(path: &[u16]) -> bool {
    let mut volume = [0u16; 1024];
    // SAFETY: the path is NUL-terminated, and the buffer's length is its own.
    unsafe { GetVolumePathNameW(path.as_ptr(), volume.as_mut_ptr(), volume.len() as u32) != 0 && GetDriveTypeW(volume.as_ptr()) == DRIVE_REMOTE }
}
