//! The computer's root certificates, as an administrator gave them: the Root stores at the locations only an
//! administrator writes. CertOpenStore's system store provider opens a store by its name at a location (Microsoft,
//! CertOpenStore, CERT_STORE_PROV_SYSTEM; "System Store Locations"):
//! - CERT_SYSTEM_STORE_LOCAL_MACHINE: the computer's own, HKLM\SOFTWARE\Microsoft\SystemCertificates;
//! - CERT_SYSTEM_STORE_LOCAL_MACHINE_GROUP_POLICY: what Group Policy gives the computer,
//!   HKLM\SOFTWARE\Policies\Microsoft\SystemCertificates;
//! - CERT_SYSTEM_STORE_LOCAL_MACHINE_ENTERPRISE: the enterprise's, HKLM\SOFTWARE\Microsoft\EnterpriseCertificates;
//! - CERT_SYSTEM_STORE_CURRENT_USER_GROUP_POLICY: what Group Policy gives the user,
//!   HKCU\Software\Policies\Microsoft\SystemCertificates, which Group Policy writes and the user does not. Unsure:
//!   Microsoft documents that the Policies keys hold Group Policy's settings, not their ACL; a standard user's write
//!   to them is checked on a PC.
//!
//! The user's own Root store (CERT_SYSTEM_STORE_CURRENT_USER) is never opened: any program of the user's may add a
//! root to it, after one click on Windows's question, and it holds the computer's roots too. Each store is opened
//! read-only and only where it is there; a certificate in two of them is said once.

use windows_sys::Win32::Foundation::{GetLastError, ERROR_FILE_NOT_FOUND};
use windows_sys::Win32::Security::Cryptography::{
    CertCloseStore, CertEnumCertificatesInStore, CertOpenStore, CERT_STORE_OPEN_EXISTING_FLAG, CERT_STORE_PROV_SYSTEM_W,
    CERT_STORE_READONLY_FLAG, CERT_SYSTEM_STORE_CURRENT_USER_GROUP_POLICY_ID, CERT_SYSTEM_STORE_LOCAL_MACHINE_ENTERPRISE_ID,
    CERT_SYSTEM_STORE_LOCAL_MACHINE_GROUP_POLICY_ID, CERT_SYSTEM_STORE_LOCAL_MACHINE_ID, CERT_SYSTEM_STORE_LOCATION_SHIFT,
    X509_ASN_ENCODING,
};

use super::wide;

/// The stores read, each by its location's id and the name its words give it.
pub const STORES: [(u32, &str); 4] = [
    (CERT_SYSTEM_STORE_LOCAL_MACHINE_ID, "the computer's Root store"),
    (CERT_SYSTEM_STORE_LOCAL_MACHINE_GROUP_POLICY_ID, "Group Policy's Root store for the computer"),
    (CERT_SYSTEM_STORE_LOCAL_MACHINE_ENTERPRISE_ID, "the enterprise's Root store"),
    (CERT_SYSTEM_STORE_CURRENT_USER_GROUP_POLICY_ID, "Group Policy's Root store for this user"),
];

/// Each root certificate of STORES, its DER, in the stores' order.
pub fn roots() -> Result<Vec<Vec<u8>>, String> {
    let mut found: Vec<Vec<u8>> = Vec::new();
    let name = wide("Root");
    for (location, named) in STORES {
        let flags = (location << CERT_SYSTEM_STORE_LOCATION_SHIFT) | CERT_STORE_READONLY_FLAG | CERT_STORE_OPEN_EXISTING_FLAG;
        // SAFETY: the name is a NUL-terminated wide string that outlives the call.
        let store = unsafe { CertOpenStore(CERT_STORE_PROV_SYSTEM_W, 0, 0, flags, name.as_ptr().cast()) };
        if store.is_null() {
            // SAFETY: no call between.
            let error = unsafe { GetLastError() };
            // A store no one has made, as Group Policy's where it gave none: no root there. The provider says it as
            // the registry's error, or as that error's HRESULT.
            if error == ERROR_FILE_NOT_FOUND || error == 0x8007_0002 {
                continue;
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
            let der = unsafe {
                let certificate = &*context;
                if certificate.dwCertEncodingType & X509_ASN_ENCODING == 0 {
                    continue;
                }
                std::slice::from_raw_parts(certificate.pbCertEncoded, certificate.cbCertEncoded as usize).to_vec()
            };
            if !found.contains(&der) {
                found.push(der);
            }
        }
        // SAFETY: opened above, and every context of it freed by the enumeration's end.
        unsafe { CertCloseStore(store, 0) };
    }
    Ok(found)
}

#[cfg(test)]
mod tests {
    use windows_sys::Win32::Security::Cryptography::{CERT_SYSTEM_STORE_CURRENT_USER_ID, CERT_SYSTEM_STORE_LOCAL_MACHINE_ID};

    use super::STORES;

    #[test]
    fn reads_the_stores_an_administrator_writes_and_never_the_user_s_own() {
        let ids: Vec<u32> = STORES.iter().map(|(id, _)| *id).collect();
        assert_eq!(ids, vec![CERT_SYSTEM_STORE_LOCAL_MACHINE_ID, 8, 9, 7]);
        assert!(!ids.contains(&CERT_SYSTEM_STORE_CURRENT_USER_ID));
    }
}
