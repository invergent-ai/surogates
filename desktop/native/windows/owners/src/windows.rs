//! A handle's owner and writers, from its security descriptor (GetSecurityInfo, SE_FILE_OBJECT, with OWNER_ and
//! DACL_SECURITY_INFORMATION), as ntat's owners reads them for the app (native/ntat/ntat.c): a writer is the trustee of
//! an allow entry that grants any right that changes the file or folder, its name or its descriptor, and applies to it
//! (an INHERIT_ONLY_ACE entry is its children's alone). Deny entries are not read: they take rights away. An allow entry
//! of a kind whose trustee is not read here is a writer "?"; no access list at all, which grants everyone everything, is
//! Everyone's.

use std::ptr::null_mut;

use windows_sys::Win32::Foundation::{LocalFree, HANDLE};
use windows_sys::Win32::Security::Authorization::{ConvertSidToStringSidW, GetSecurityInfo, SE_FILE_OBJECT};
use windows_sys::Win32::Security::{GetAce, IsValidSid, ACCESS_ALLOWED_ACE, ACE_HEADER, ACL, DACL_SECURITY_INFORMATION, OWNER_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR, PSID};

use crate::Owners;

// winnt.h's: the allow entries, the kinds whose trustee follows the mask (an allow entry and a callback one), and
// INHERIT_ONLY_ACE.
const ACCESS_ALLOWED_ACE_TYPE: u8 = 0x0;
const ACCESS_ALLOWED_COMPOUND_ACE_TYPE: u8 = 0x4;
const ACCESS_ALLOWED_OBJECT_ACE_TYPE: u8 = 0x5;
const ACCESS_ALLOWED_CALLBACK_ACE_TYPE: u8 = 0x9;
const ACCESS_ALLOWED_CALLBACK_OBJECT_ACE_TYPE: u8 = 0xB;
const INHERIT_ONLY_ACE: u8 = 0x8;

// What changes it: FILE_WRITE_DATA (a folder's FILE_ADD_FILE), FILE_APPEND_DATA (FILE_ADD_SUBDIRECTORY), FILE_WRITE_EA,
// FILE_DELETE_CHILD, FILE_WRITE_ATTRIBUTES, DELETE, WRITE_DAC, WRITE_OWNER, MAXIMUM_ALLOWED, GENERIC_ALL and
// GENERIC_WRITE.
const CHANGES: u32 = 0x2 | 0x4 | 0x10 | 0x40 | 0x100 | 0x0001_0000 | 0x0004_0000 | 0x0008_0000 | 0x0200_0000 | 0x1000_0000 | 0x4000_0000;

fn sid_text(sid: PSID) -> String {
    let mut text = null_mut();
    // SAFETY: *sid* is from the descriptor GetSecurityInfo gave, which outlives this call; the text is freed here.
    if sid.is_null() || unsafe { IsValidSid(sid) } == 0 || unsafe { ConvertSidToStringSidW(sid, &mut text) } == 0 {
        return "?".to_string();
    }
    // SAFETY: ConvertSidToStringSidW wrote a NUL-ended string at *text*.
    let units = unsafe { std::slice::from_raw_parts(text, (0..).take_while(|&at| *text.add(at) != 0).count()) };
    let said = String::from_utf16_lossy(units);
    unsafe { LocalFree(text.cast()) };
    said
}

/// Who owns what *handle* opens, and who its access list lets change it.
///
/// # Safety
///
/// *handle* is an open handle of a file or folder that holds READ_CONTROL, for the whole call.
pub unsafe fn of(handle: HANDLE) -> Result<Owners, String> {
    let mut owner: PSID = null_mut();
    let mut dacl: *mut ACL = null_mut();
    let mut descriptor: PSECURITY_DESCRIPTOR = null_mut();
    // SAFETY: each out pointer is a local; the descriptor they point into is freed below, after the last read of them.
    let status = unsafe {
        GetSecurityInfo(handle, SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION, &mut owner, null_mut(), &mut dacl, null_mut(), &mut descriptor)
    };
    if status != 0 {
        return Err(format!("its owner and access list could not be read: Windows error {status}"));
    }
    let mut writers = Vec::new();
    if dacl.is_null() {
        writers.push("S-1-1-0".to_string());
    } else {
        // SAFETY: *dacl* points into the descriptor, alive until it is freed below.
        let count = unsafe { (*dacl).AceCount };
        for index in 0..u32::from(count) {
            let mut ace: *mut core::ffi::c_void = null_mut();
            // SAFETY: as above; GetAce points *ace* at the entry inside the list.
            if unsafe { GetAce(dacl, index, &mut ace) } == 0 {
                writers.push("?".to_string());
                continue;
            }
            // SAFETY: every entry begins with its header.
            let header = unsafe { &*(ace as *const ACE_HEADER) };
            if header.AceFlags & INHERIT_ONLY_ACE != 0 {
                continue;
            }
            match header.AceType {
                ACCESS_ALLOWED_ACE_TYPE | ACCESS_ALLOWED_CALLBACK_ACE_TYPE => {
                    // The two share ACCESS_ALLOWED_ACE's layout: the header, the mask, then the trustee's SID.
                    // SAFETY: as above.
                    let allowed = unsafe { &*(ace as *const ACCESS_ALLOWED_ACE) };
                    if allowed.Mask & CHANGES != 0 {
                        writers.push(sid_text((&raw const allowed.SidStart).cast_mut().cast()));
                    }
                }
                ACCESS_ALLOWED_OBJECT_ACE_TYPE | ACCESS_ALLOWED_CALLBACK_OBJECT_ACE_TYPE | ACCESS_ALLOWED_COMPOUND_ACE_TYPE => writers.push("?".to_string()),
                _ => {}
            }
        }
    }
    let owner = sid_text(owner);
    // SAFETY: GetSecurityInfo allocated it with LocalAlloc; nothing reads it after this.
    unsafe { LocalFree(descriptor) };
    Ok(Owners { owner, writers })
}
