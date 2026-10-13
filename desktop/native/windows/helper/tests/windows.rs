//! surogate-helper as the app runs it, on Windows: what it prints for each command, against this computer's own stores,
//! processes, programs and registry. The tests that add roots to the computer's stores need an administrator, as a CI
//! runner's account is: `cargo test -- --include-ignored` runs them there, or elevated in a test account on a PC.
#![cfg(windows)]

use std::ffi::OsStr;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use surogate_helper::pem::certificate;
use windows_sys::Win32::Foundation::{ERROR_SUCCESS, GetLastError};
use windows_sys::Win32::Security::Cryptography::{
    CertAddEncodedCertificateToStore, CertCloseStore, CertCreateCertificateContext, CertDeleteCertificateFromStore, CertEnumCertificatesInStore,
    CertFindCertificateInStore, CertFreeCertificateContext, CertOpenStore, CertSetCertificateContextProperty, CertSetEnhancedKeyUsage, CERT_DISALLOWED_FILETIME_PROP_ID,
    CERT_FIND_EXISTING, CERT_STORE_ADD_REPLACE_EXISTING, CERT_STORE_PROV_REG, CERT_STORE_PROV_SYSTEM_W, CERT_SYSTEM_STORE_CURRENT_USER_GROUP_POLICY_ID,
    CERT_SYSTEM_STORE_CURRENT_USER_ID, CERT_SYSTEM_STORE_LOCAL_MACHINE_ENTERPRISE_ID, CERT_SYSTEM_STORE_LOCAL_MACHINE_GROUP_POLICY_ID,
    CERT_SYSTEM_STORE_LOCAL_MACHINE_ID, CERT_SYSTEM_STORE_LOCATION_SHIFT, CRYPT_INTEGER_BLOB, CTL_USAGE, HCERTSTORE, X509_ASN_ENCODING,
};
use windows_sys::Win32::System::Registry::{RegCloseKey, RegCreateKeyExW, HKEY, HKEY_CURRENT_USER, KEY_ALL_ACCESS, REG_OPTION_NON_VOLATILE};

const HELPER: &str = env!("CARGO_BIN_EXE_surogate-helper");
const EDGE: &str = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const CHROME: &str = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

fn helper(args: &[&OsStr]) -> Output {
    // As the app starts it: with no environment of the caller's but the Windows folder.
    Command::new(HELPER).args(args).env_clear().env("SystemRoot", std::env::var_os("SystemRoot").unwrap_or_else(|| "C:\\Windows".into())).output().expect("the helper runs")
}

fn said(args: &[&str]) -> String {
    let done = helper(&args.iter().map(OsStr::new).collect::<Vec<_>>());
    assert!(done.status.success(), "{args:?}: {}", String::from_utf8_lossy(&done.stderr));
    String::from_utf8(done.stdout).expect("UTF-8")
}

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(std::iter::once(0)).collect()
}

// A test's own copy of a file, in a folder of its own.
fn copy(from: &str, named: &str) -> PathBuf {
    let folder = std::env::temp_dir().join(format!("surogate-helper-{}-{named}", std::process::id()));
    std::fs::create_dir_all(&folder).unwrap();
    let to = folder.join(named);
    std::fs::copy(from, &to).unwrap();
    to
}

// The store *name* at *location*, opened for writing, made where it is not there; or, with *key*, the registry key's.
struct Store(HCERTSTORE);

// What a root is given beside its store: its own purposes, or a distrust date, as certlm and Microsoft's program set them.
#[derive(Clone, Copy)]
enum Given {
    AsItIs,
    Purposes(&'static [&'static str]),
    DistrustedFrom2020,
}

impl Store {
    fn system(location: u32, name: &str) -> Store {
        let name = wide(name);
        // SAFETY: the name outlives the call.
        let store = unsafe { CertOpenStore(CERT_STORE_PROV_SYSTEM_W, 0, 0, location << CERT_SYSTEM_STORE_LOCATION_SHIFT, name.as_ptr().cast()) };
        assert!(!store.is_null(), "store {location} opens for writing: 0x{:08x}", unsafe { GetLastError() });
        Store(store)
    }

    // The user's own Root store as its registry key holds it, written as the registry provider writes it, without
    // the question Windows asks before a root is added to that store.
    fn user_registry() -> Store {
        let mut key: HKEY = std::ptr::null_mut();
        let path = wide("Software\\Microsoft\\SystemCertificates\\Root");
        // SAFETY: the path outlives the call; the key is closed once the store has its own.
        let made = unsafe { RegCreateKeyExW(HKEY_CURRENT_USER, path.as_ptr(), 0, std::ptr::null(), REG_OPTION_NON_VOLATILE, KEY_ALL_ACCESS, std::ptr::null(), &mut key, std::ptr::null_mut()) };
        assert_eq!(made, ERROR_SUCCESS);
        // SAFETY: the provider duplicates the key it is given.
        let store = unsafe { CertOpenStore(CERT_STORE_PROV_REG, 0, 0, 0, key.cast_const().cast()) };
        unsafe { RegCloseKey(key) };
        assert!(!store.is_null());
        Store(store)
    }

    fn add(&self, der: &[u8], given: Given) {
        let mut added = std::ptr::null_mut();
        // SAFETY: the bytes outlive the call; the context it gives is the store's own, freed below.
        let done = unsafe { CertAddEncodedCertificateToStore(self.0, X509_ASN_ENCODING, der.as_ptr(), der.len() as u32, CERT_STORE_ADD_REPLACE_EXISTING, &mut added) };
        assert_ne!(done, 0, "added: 0x{:08x}", unsafe { GetLastError() });
        // SAFETY: the context is the store's; what is set on it is kept in the store.
        unsafe {
            match given {
                Given::AsItIs => {}
                Given::Purposes(purposes) => {
                    let names: Vec<std::ffi::CString> = purposes.iter().map(|purpose| std::ffi::CString::new(*purpose).unwrap()).collect();
                    let mut pointers: Vec<*mut u8> = names.iter().map(|name| name.as_ptr().cast_mut().cast()).collect();
                    let usage = CTL_USAGE { cUsageIdentifier: pointers.len() as u32, rgpszUsageIdentifier: pointers.as_mut_ptr() };
                    assert_ne!(CertSetEnhancedKeyUsage(added, &usage), 0, "purposes set");
                }
                Given::DistrustedFrom2020 => {
                    // 2020-01-01, in a FILETIME's hundreds of nanoseconds since 1601.
                    let when: u64 = 132_223_104_000_000_000;
                    let bytes = when.to_le_bytes();
                    let blob = CRYPT_INTEGER_BLOB { cbData: 8, pbData: bytes.as_ptr().cast_mut() };
                    assert_ne!(CertSetCertificateContextProperty(added, CERT_DISALLOWED_FILETIME_PROP_ID, 0, (&raw const blob).cast()), 0, "distrust date set");
                }
            }
            CertFreeCertificateContext(added);
        }
    }

    fn holds(&self, der: &[u8]) -> bool {
        let mut context = std::ptr::null();
        loop {
            // SAFETY: the enumeration frees each context it moves past.
            context = unsafe { CertEnumCertificatesInStore(self.0, context) };
            if context.is_null() {
                return false;
            }
            // SAFETY: a context of the store's, not yet freed.
            let found = unsafe { std::slice::from_raw_parts((*context).pbCertEncoded, (*context).cbCertEncoded as usize) };
            if found == der {
                unsafe { CertFreeCertificateContext(context) };
                return true;
            }
        }
    }

    fn remove(&self, der: &[u8]) {
        // SAFETY: the context made here is freed here; the one found is freed by its deletion.
        unsafe {
            let wanted = CertCreateCertificateContext(X509_ASN_ENCODING, der.as_ptr(), der.len() as u32);
            let found = CertFindCertificateInStore(self.0, X509_ASN_ENCODING, 0, CERT_FIND_EXISTING, wanted.cast(), std::ptr::null());
            if !found.is_null() {
                CertDeleteCertificateFromStore(found);
            }
            CertFreeCertificateContext(wanted);
        }
    }
}

impl Drop for Store {
    fn drop(&mut self) {
        // SAFETY: opened by this Store alone.
        unsafe { CertCloseStore(self.0, 0) };
    }
}

// A root of the test's: what it is called, its DER, the stores it is put in, what it is given there, and whether it is listed.
type Root = (&'static str, &'static [u8], Vec<(u32, &'static str)>, Given, bool);

const SERVER_AUTH: &str = "1.3.6.1.5.5.7.3.1";
const CLIENT_AUTH: &str = "1.3.6.1.5.5.7.3.2";
const LOCAL_MACHINE: u32 = CERT_SYSTEM_STORE_LOCAL_MACHINE_ID;
const CURRENT_USER: u32 = CERT_SYSTEM_STORE_CURRENT_USER_ID;
const MACHINE_POLICY: u32 = CERT_SYSTEM_STORE_LOCAL_MACHINE_GROUP_POLICY_ID;
const USER_POLICY: u32 = CERT_SYSTEM_STORE_CURRENT_USER_GROUP_POLICY_ID;
const ENTERPRISE: u32 = CERT_SYSTEM_STORE_LOCAL_MACHINE_ENTERPRISE_ID;

#[test]
#[ignore = "adds test roots to this computer's stores: run elevated, on a CI runner or in a test account"]
fn lists_the_roots_an_administrator_or_a_policy_gave_that_windows_trusts_for_tls_and_no_other() {
    let roots: Vec<Root> = vec![
        // Taken: the computer's own Root store, Group Policy's for the computer and for the user, the enterprise's.
        ("the computer's own", include_bytes!("fixtures/machine.der"), vec![(LOCAL_MACHINE, "Root")], Given::AsItIs, true),
        ("Group Policy's for the computer", include_bytes!("fixtures/machine-policy.der"), vec![(MACHINE_POLICY, "Root")], Given::AsItIs, true),
        ("the enterprise's", include_bytes!("fixtures/enterprise.der"), vec![(ENTERPRISE, "Root")], Given::AsItIs, true),
        ("Group Policy's for the user", include_bytes!("fixtures/user-policy.der"), vec![(USER_POLICY, "Root")], Given::AsItIs, true),
        ("one whose purposes include server authentication", include_bytes!("fixtures/server-too.der"), vec![(LOCAL_MACHINE, "Root")], Given::Purposes(&[CLIENT_AUTH, SERVER_AUTH]), true),
        // Never read: Microsoft's program, which any user's chain fills, and the smart cards' roots.
        ("Microsoft's program's", include_bytes!("fixtures/authroot.der"), vec![(LOCAL_MACHINE, "AuthRoot")], Given::AsItIs, false),
        ("a smart card's", include_bytes!("fixtures/smartcard.der"), vec![(LOCAL_MACHINE, "SmartCardRoot")], Given::AsItIs, false),
        // Not for TLS: client authentication alone, every purpose turned off, a distrust date.
        ("one for client authentication alone", include_bytes!("fixtures/client-only.der"), vec![(LOCAL_MACHINE, "Root")], Given::Purposes(&[CLIENT_AUTH]), false),
        ("one with every purpose turned off", include_bytes!("fixtures/no-purposes.der"), vec![(LOCAL_MACHINE, "Root")], Given::Purposes(&[]), false),
        ("one with a distrust date", include_bytes!("fixtures/distrust-after.der"), vec![(LOCAL_MACHINE, "Root")], Given::DistrustedFrom2020, false),
        // Distrusted, though a Root store holds it: in each Disallowed store.
        ("one the computer distrusts", include_bytes!("fixtures/distrusted-machine.der"), vec![(LOCAL_MACHINE, "Root"), (LOCAL_MACHINE, "Disallowed")], Given::AsItIs, false),
        ("one Group Policy distrusts for the computer", include_bytes!("fixtures/distrusted-policy.der"), vec![(MACHINE_POLICY, "Root"), (MACHINE_POLICY, "Disallowed")], Given::AsItIs, false),
        ("one the user distrusts", include_bytes!("fixtures/distrusted-user.der"), vec![(ENTERPRISE, "Root"), (CURRENT_USER, "Disallowed")], Given::AsItIs, false),
        ("one Group Policy distrusts for the user", include_bytes!("fixtures/distrusted-user-policy.der"), vec![(USER_POLICY, "Root"), (USER_POLICY, "Disallowed")], Given::AsItIs, false),
    ];
    let own: &[u8] = include_bytes!("fixtures/user.der");
    for (_, der, stores, given, _) in &roots {
        for (location, name) in stores {
            Store::system(*location, name).add(der, if *name == "Disallowed" { Given::AsItIs } else { *given });
        }
    }
    // The user's own root, in the registry key of the user's Root store. Windows lists from that store only the roots
    // the user agreed to at its question (its protected roots), which no test can answer: so a root a program put
    // there is read back by the registry's own provider alone, and whatever Windows would list, the helper lists none.
    Store::user_registry().add(own, Given::AsItIs);
    let held = Store::user_registry().holds(own);
    let listed = std::panic::catch_unwind(|| said(&["trust"]));
    for (_, der, stores, _, _) in &roots {
        for (location, name) in stores {
            Store::system(*location, name).remove(der);
        }
    }
    Store::user_registry().remove(own);
    assert!(held, "the user's own root is in its store's registry key");
    let listed = listed.expect("listed");
    let said: Vec<(&str, bool)> = roots.iter().map(|(named, der, _, _, _)| (*named, listed.contains(&certificate(der)))).collect();
    let wanted: Vec<(&str, bool)> = roots.iter().map(|(named, _, _, _, taken)| (*named, *taken)).collect();
    assert_eq!(said, wanted);
    assert!(!listed.contains(&certificate(own)), "the user's own root is never listed");
    // PEM alone, each certificate whole.
    assert_eq!(listed.matches("-----BEGIN CERTIFICATE-----\n").count(), listed.matches("-----END CERTIFICATE-----\n").count());
    assert!(listed.lines().all(|line| line.starts_with("-----") || line.bytes().all(|b| b.is_ascii_alphanumeric() || b"+/=".contains(&b))));
}

#[test]
fn lists_this_user_s_processes_with_their_programs_and_arguments_and_no_one_else_s() {
    let marker = format!("surogate-helper-test-{}", std::process::id());
    let mut child = Command::new("C:\\Windows\\System32\\cmd.exe");
    std::os::windows::process::CommandExt::raw_arg(&mut child, format!("/d /c \"ping -n 30 127.0.0.1 >nul & rem {marker} with spaces\""));
    let mut child = child.spawn().unwrap();
    let listed = said(&["procs"]);
    child.kill().ok();
    child.wait().ok();
    let lines: Vec<&str> = listed.lines().collect();
    let line = lines.iter().find(|line| line.starts_with(&format!("{{\"pid\":{},", child.id())))
        .unwrap_or_else(|| panic!("the child {} is listed among {} lines: {:?}", child.id(), lines.len(), &lines[..lines.len().min(5)]));
    assert!(line.to_ascii_lowercase().contains("\"path\":\"c:\\\\windows\\\\system32\\\\cmd.exe\""), "{line}");
    assert!(line.ends_with(&format!(",\"/d\",\"/c\",\"ping -n 30 127.0.0.1 >nul & rem {marker} with spaces\"]}}")), "{line}");
    // The helper itself, as this user's.
    assert!(listed.lines().any(|line| line.contains("surogate-helper")));
    // System's own, pid 4, is no user's.
    assert!(!listed.lines().any(|line| line.starts_with("{\"pid\":4,")));
    assert!(listed.lines().all(|line| line.starts_with("{\"pid\":") && line.ends_with("]}")));
}

#[test]
fn says_whom_a_program_is_signed_by_and_that_a_changed_or_unsigned_one_does_not_check_out() {
    assert_eq!(said(&["verify", EDGE]), "{\"trusted\":true,\"signer\":\"Microsoft Corporation\",\"status\":\"0x00000000\",\"remote\":false}\n");
    // One byte of its code changed, its signature's digest no longer its own: TRUST_E_BAD_DIGEST.
    let changed = copy(EDGE, "msedge.exe");
    let mut bytes = std::fs::read(&changed).unwrap();
    let at = bytes.len() / 2;
    bytes[at] ^= 0xff;
    std::fs::write(&changed, bytes).unwrap();
    let said_changed = said(&["verify", changed.to_str().unwrap()]);
    assert!(said_changed.starts_with("{\"trusted\":false,\"signer\":null,\"status\":\"0x8"), "{said_changed}");
    // This test's own build, which no one signed: TRUST_E_NOSIGNATURE.
    let unsigned = copy(HELPER, "unsigned.exe");
    assert_eq!(said(&["verify", unsigned.to_str().unwrap()]), "{\"trusted\":false,\"signer\":null,\"status\":\"0x800b0100\",\"remote\":false}\n");
    // Nothing there.
    assert!(said(&["verify", "C:\\nowhere\\none.exe"]).starts_with("{\"trusted\":false,\"signer\":null,"));
    for file in [changed, unsigned] {
        std::fs::remove_dir_all(file.parent().unwrap()).ok();
    }
}

#[test]
#[ignore = "reads the runner's own Chrome"]
fn says_chrome_is_signed_by_google() {
    assert_eq!(said(&["verify", CHROME]), "{\"trusted\":true,\"signer\":\"Google LLC\",\"status\":\"0x00000000\",\"remote\":false}\n");
}

#[test]
fn says_where_the_app_paths_key_puts_a_program_and_refuses_a_name_that_is_not_one() {
    let found = said(&["app-path", "msedge.exe"]);
    let first = found.lines().next().expect("Edge's App Paths key");
    assert!(Path::new(first).is_file(), "{first}");
    assert!(first.to_ascii_lowercase().ends_with("\\msedge.exe"), "{first}");
    assert_eq!(said(&["app-path", "no-such-program-surogate.exe"]), "");
    for name in ["..\\msedge.exe", "msedge", "C:\\x.exe"] {
        let done = helper(&[OsStr::new("app-path"), OsStr::new(name)]);
        assert_eq!(done.status.code(), Some(1), "{name}");
    }
}

#[test]
fn looks_at_a_folder_through_its_links_and_says_nothing() {
    let folder = std::env::temp_dir().join(format!("surogate-helper-look-{}", std::process::id()));
    std::fs::create_dir_all(&folder).unwrap();
    // A junction to the folder, and one to a folder that is not there, as `mklink /J` makes them.
    for (link, to) in [("link", folder.clone()), ("dangling", folder.join("gone"))] {
        let made = Command::new("C:\\Windows\\System32\\cmd.exe").args(["/d", "/c", "mklink", "/J"]).arg(folder.join(link)).arg(&to).output().unwrap();
        assert!(made.status.success(), "{}", String::from_utf8_lossy(&made.stderr));
    }
    let looked: Vec<Option<i32>> = [folder.clone(), folder.join("link"), folder.join("gone"), folder.join("dangling")]
        .iter()
        .map(|path| {
            let done = helper(&[OsStr::new("look"), path.as_os_str()]);
            assert!(done.stdout.is_empty() && done.stderr.is_empty(), "{path:?} says nothing");
            done.status.code()
        })
        .collect();
    std::fs::remove_dir_all(&folder).ok();
    assert_eq!(looked, vec![Some(0), Some(0), Some(1), Some(1)]);
}

#[test]
fn says_how_it_is_run_when_it_is_not() {
    for args in [&[][..], &["nothing"], &["trust", "extra"], &["verify"]] {
        let done = helper(&args.iter().map(OsStr::new).collect::<Vec<_>>());
        assert_eq!(done.status.code(), Some(2), "{args:?}");
        assert!(String::from_utf8_lossy(&done.stderr).starts_with("usage: surogate-helper"), "{args:?}");
        assert!(done.stdout.is_empty());
    }
}
