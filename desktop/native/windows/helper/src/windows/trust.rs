//! The company's root certificates, as an administrator or a policy gave them, and only those Windows itself would
//! trust for TLS.
//!
//! The Root stores read are physical stores, each opened by its name (Microsoft, CertOpenStore,
//! CERT_STORE_PROV_PHYSICAL: "a system store name and a name of a physical, separated by a backslash"; "Physical Store
//! Names"; "System Store Locations"):
//! - LocalMachine's Root\.Default, the computer's own (HKLM\SOFTWARE\Microsoft\SystemCertificates\Root), which an
//!   administrator writes;
//! - LocalMachine's Root\.GroupPolicy, what Group Policy gives the computer (HKLM\SOFTWARE\Policies\…);
//! - LocalMachine's Root\.Enterprise, the enterprise's (HKLM\SOFTWARE\Microsoft\EnterpriseCertificates\Root);
//! - the user's Root\.GroupPolicy, what Group Policy gives the user (HKCU\Software\Policies\…), whose key "a standard
//!   user cannot change" (Microsoft, "Implementing Registry-Based Group Policy for Applications").
//!
//! Never LocalMachine's Root\.AuthRoot, Microsoft's root program, which Windows fills whenever any user's program
//! builds a chain to one of its roots (Microsoft, "Automatic updating of trusted root authorities"), each root with
//! purposes of its own; nor Root\.SmartCard, nor the user's own Root\.Default, which any program of the user's may add
//! to after one click on Windows's question. LocalMachine's Root system store, opened whole, is all of them together.
//!
//! A root is taken only where Windows would trust it for TLS: its own extended-key-usage property, as certlm's
//! "Enable only the following purposes" or "Disable all purposes" sets it, allows server authentication
//! (CertGetEnhancedKeyUsage, CERT_FIND_PROP_ONLY_ENHKEY_USAGE_FLAG); it has no distrust date
//! (CERT_DISALLOWED_FILETIME_PROP_ID); and no Disallowed store holds it: LocalMachine's and the user's, each opened
//! whole with every physical store of its own, Group Policy's "Untrusted Certificates" and Microsoft's list of
//! fraudulent certificates among them, and their Group Policy stores besides (Microsoft, "Configure trusted roots and
//! disallowed certificates"). A store that is not there gives none; one that cannot be read fails the answer whole,
//! as no root is taken whose distrust could not be read.

use windows_sys::Win32::Foundation::{GetLastError, SetLastError, CRYPT_E_NOT_FOUND, ERROR_FILE_NOT_FOUND};
use windows_sys::Win32::Security::Cryptography::{
    CertCloseStore, CertEnumCertificatesInStore, CertGetCertificateContextProperty, CertGetEnhancedKeyUsage, CertOpenStore, CERT_CONTEXT,
    CERT_DISALLOWED_FILETIME_PROP_ID, CERT_FIND_PROP_ONLY_ENHKEY_USAGE_FLAG, CERT_STORE_OPEN_EXISTING_FLAG, CERT_STORE_PROV_PHYSICAL_W,
    CERT_STORE_PROV_SYSTEM_W, CERT_STORE_READONLY_FLAG, CERT_SYSTEM_STORE_CURRENT_USER_GROUP_POLICY_ID, CERT_SYSTEM_STORE_CURRENT_USER_ID,
    CERT_SYSTEM_STORE_LOCAL_MACHINE_GROUP_POLICY_ID, CERT_SYSTEM_STORE_LOCAL_MACHINE_ID, CERT_SYSTEM_STORE_LOCATION_SHIFT, CTL_USAGE, HCERTSTORE,
    X509_ASN_ENCODING,
};

use super::wide;
use crate::answers::{for_server_auth, Usages};

/// The Root stores read: each one's location, its physical store's name, and the name its words give it.
pub const ROOTS: [(u32, &str, &str); 4] = [
    (CERT_SYSTEM_STORE_LOCAL_MACHINE_ID, "Root\\.Default", "the computer's Root store"),
    (CERT_SYSTEM_STORE_LOCAL_MACHINE_ID, "Root\\.GroupPolicy", "Group Policy's Root store for the computer"),
    (CERT_SYSTEM_STORE_LOCAL_MACHINE_ID, "Root\\.Enterprise", "the enterprise's Root store"),
    (CERT_SYSTEM_STORE_CURRENT_USER_ID, "Root\\.GroupPolicy", "Group Policy's Root store for this user"),
];

/// The Disallowed stores read, each a system store opened whole at its location, with the name its words give it.
pub const DISALLOWED: [(u32, &str); 4] = [
    (CERT_SYSTEM_STORE_LOCAL_MACHINE_ID, "the computer's Untrusted Certificates"),
    (CERT_SYSTEM_STORE_CURRENT_USER_ID, "this user's Untrusted Certificates"),
    (CERT_SYSTEM_STORE_LOCAL_MACHINE_GROUP_POLICY_ID, "Group Policy's Untrusted Certificates for the computer"),
    (CERT_SYSTEM_STORE_CURRENT_USER_GROUP_POLICY_ID, "Group Policy's Untrusted Certificates for this user"),
];

/// Each root of ROOTS that Windows would trust for TLS and no Disallowed store holds, its DER, in the stores' order.
pub fn roots() -> Result<Vec<Vec<u8>>, String> {
    let mut disallowed: Vec<Vec<u8>> = Vec::new();
    for (location, named) in DISALLOWED {
        each(CERT_STORE_PROV_SYSTEM_W, location, "Disallowed", named, |_, der| {
            if !disallowed.iter().any(|found| found == der) {
                disallowed.push(der.to_vec());
            }
        })?;
    }
    let mut found: Vec<Vec<u8>> = Vec::new();
    for (location, store, named) in ROOTS {
        each(CERT_STORE_PROV_PHYSICAL_W, location, store, named, |certificate, der| {
            if disallowed.iter().any(|distrusted| distrusted == der) || found.iter().any(|known| known == der) {
                return;
            }
            // SAFETY: a context of the open store's, which the enumeration holds while this runs.
            if unsafe { distrust_date(certificate) } {
                return;
            }
            // SAFETY: as above.
            if unsafe { usages(certificate) }.is_some_and(|usages| for_server_auth(&usages)) {
                found.push(der.to_vec());
            }
        })?;
    }
    Ok(found)
}

// Each X.509 certificate of the store *name* at *location*, opened read-only by *provider* where it is there, given
// to *take* with its DER.
fn each(provider: windows_sys::core::PCSTR, location: u32, name: &str, named: &str, mut take: impl FnMut(*const CERT_CONTEXT, &[u8])) -> Result<(), String> {
    let name = wide(name);
    let flags = (location << CERT_SYSTEM_STORE_LOCATION_SHIFT) | CERT_STORE_READONLY_FLAG | CERT_STORE_OPEN_EXISTING_FLAG;
    // SAFETY: the name is a NUL-terminated wide string that outlives the call.
    let store: HCERTSTORE = unsafe { CertOpenStore(provider, 0, 0, flags, name.as_ptr().cast()) };
    if store.is_null() {
        // SAFETY: no call between.
        let error = unsafe { GetLastError() };
        // A store no one has made, as Group Policy's where it gave none: nothing there. The provider says it as the
        // registry's error, or as that error's HRESULT.
        if error == ERROR_FILE_NOT_FOUND || error == 0x8007_0002 {
            return Ok(());
        }
        return Err(format!("{named} could not be read: error 0x{error:08x}"));
    }
    let mut context = std::ptr::null();
    loop {
        // SAFETY: the store is open; the previous context is the enumeration's own, which this call frees.
        context = unsafe { CertEnumCertificatesInStore(store, context) };
        if context.is_null() {
            break;
        }
        // SAFETY: a context the store gave holds its encoded certificate for as long as it is not freed.
        unsafe {
            let certificate = &*context;
            if certificate.dwCertEncodingType & X509_ASN_ENCODING != 0 {
                take(context, std::slice::from_raw_parts(certificate.pbCertEncoded, certificate.cbCertEncoded as usize));
            }
        }
    }
    // SAFETY: opened above, and every context of it freed by the enumeration's end.
    unsafe { CertCloseStore(store, 0) };
    Ok(())
}

// Whether *certificate* carries a distrust date: Microsoft's program sets one on a root it no longer trusts from then.
unsafe fn distrust_date(certificate: *const CERT_CONTEXT) -> bool {
    let mut size = 0u32;
    // SAFETY: the caller's context; the call asks for the size alone.
    unsafe { CertGetCertificateContextProperty(certificate, CERT_DISALLOWED_FILETIME_PROP_ID, std::ptr::null_mut(), &mut size) != 0 }
}

// What *certificate*'s own extended-key-usage property allows; None where it cannot be read, which takes no root.
unsafe fn usages(certificate: *const CERT_CONTEXT) -> Option<Usages> {
    let mut size = 0u32;
    // SAFETY: the caller's context; the first call asks for the size alone.
    unsafe {
        SetLastError(0);
        if CertGetEnhancedKeyUsage(certificate, CERT_FIND_PROP_ONLY_ENHKEY_USAGE_FLAG, std::ptr::null_mut(), &mut size) == 0 {
            return (GetLastError() == CRYPT_E_NOT_FOUND as u32).then_some(Usages::Unrestricted);
        }
        let mut buffer = vec![0u64; (size as usize).div_ceil(8).max(1)];
        SetLastError(0);
        if CertGetEnhancedKeyUsage(certificate, CERT_FIND_PROP_ONLY_ENHKEY_USAGE_FLAG, buffer.as_mut_ptr().cast(), &mut size) == 0 {
            return (GetLastError() == CRYPT_E_NOT_FOUND as u32).then_some(Usages::Unrestricted);
        }
        let usage = &*buffer.as_ptr().cast::<CTL_USAGE>();
        if usage.cUsageIdentifier == 0 {
            return Some(if GetLastError() == CRYPT_E_NOT_FOUND as u32 { Usages::Unrestricted } else { Usages::Only(Vec::new()) });
        }
        let purposes = (0..usage.cUsageIdentifier as usize)
            .map(|index| std::ffi::CStr::from_ptr((*usage.rgpszUsageIdentifier.add(index)).cast()).to_string_lossy().into_owned())
            .collect();
        Some(Usages::Only(purposes))
    }
}

#[cfg(test)]
mod tests {
    use windows_sys::Win32::Security::Cryptography::{CERT_SYSTEM_STORE_CURRENT_USER_ID, CERT_SYSTEM_STORE_LOCAL_MACHINE_ID};

    use super::ROOTS;

    #[test]
    fn reads_the_root_stores_an_administrator_or_a_policy_writes_and_never_microsoft_s_program_or_the_user_s_own() {
        let stores: Vec<(u32, &str)> = ROOTS.iter().map(|(location, store, _)| (*location, *store)).collect();
        assert_eq!(stores, vec![
            (CERT_SYSTEM_STORE_LOCAL_MACHINE_ID, "Root\\.Default"),
            (CERT_SYSTEM_STORE_LOCAL_MACHINE_ID, "Root\\.GroupPolicy"),
            (CERT_SYSTEM_STORE_LOCAL_MACHINE_ID, "Root\\.Enterprise"),
            (CERT_SYSTEM_STORE_CURRENT_USER_ID, "Root\\.GroupPolicy"),
        ]);
    }
}
