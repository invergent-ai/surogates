// What this computer's Host Compute Service says of itself: its answer to HcsGetServiceProperties, printed as it
// is, or why there was none. computecore.dll is loaded at run time, so a computer without it says so rather than
// failing to start.
//
// Microsoft Learn, "HcsGetServiceProperties": HRESULT WINAPI HcsGetServiceProperties(PCWSTR propertyQuery,
// PWSTR* result); the query is a JSON ServicePropertyQuery, and the result a JSON document the caller frees with
// LocalFree. The query's "Basic" property type, which names the schema versions the service supports, is the one
// hcsshim and WSL ask for. Unsure: which other property types a Windows Server runner answers.

use std::ffi::c_void;
use std::process::exit;

#[link(name = "kernel32")]
extern "system" {
    // Microsoft Learn, "LoadLibraryW", "GetProcAddress", "LocalFree" and "GetLastError".
    fn LoadLibraryW(name: *const u16) -> *mut c_void;
    fn GetProcAddress(module: *mut c_void, name: *const u8) -> *mut c_void;
    fn LocalFree(memory: *mut c_void) -> *mut c_void;
    fn GetLastError() -> u32;
}

type GetServiceProperties = unsafe extern "system" fn(query: *const u16, result: *mut *mut u16) -> i32;

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(Some(0)).collect()
}

fn main() {
    unsafe {
        let module = LoadLibraryW(wide("computecore.dll").as_ptr());
        if module.is_null() {
            println!("computecore.dll could not be loaded: error {}", GetLastError());
            exit(2);
        }
        let found = GetProcAddress(module, b"HcsGetServiceProperties\0".as_ptr());
        if found.is_null() {
            println!("computecore.dll has no HcsGetServiceProperties: error {}", GetLastError());
            exit(3);
        }
        let call: GetServiceProperties = std::mem::transmute(found);
        let query = wide(r#"{"PropertyTypes":["Basic"]}"#);
        let mut result: *mut u16 = std::ptr::null_mut();
        let code = call(query.as_ptr(), &mut result);
        let mut answer = String::new();
        if !result.is_null() {
            let mut length = 0;
            while *result.add(length) != 0 {
                length += 1;
            }
            answer = String::from_utf16_lossy(std::slice::from_raw_parts(result, length));
            LocalFree(result.cast());
        }
        println!("HcsGetServiceProperties: HRESULT 0x{:08x}", code as u32);
        println!("{answer}");
        if code < 0 {
            exit(1);
        }
    }
}
