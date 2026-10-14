// surogate-vm check: what this computer has of what the sandbox runs on, as JSON on stdout, for the backend's
// requirements (src/vm/win32.ts, requirementsWin32). It reads only what needs no VM:
// - Virtual Machine Platform, by the Host Compute Service's own service, vmcompute, and HNS: WSL's own check reads
//   these present (WSL, src/windows/common/wslutil.cpp, IsVirtualMachinePlatformInstalled). Unsure: the optional
//   feature's own state through DISM, which this does not read; whether the feature is off though the services linger
//   is left to the first boot's HCS answer, recorded on a PC on the test day.
// - The Surogate VM service, present or not (service.rs).
// - The Host Compute Service: whether computecore.dll loads and names schema 2.6 among its supported versions
//   (hcs::api::service_properties). Whether virtualization is on at all is HCS_E_HYPERV_NOT_INSTALLED at the first
//   boot, not here.

use hcs::properties::supports;
use hcs::SCHEMA;
use pipe::win::wide;
use windows_sys::Win32::System::Services::{CloseServiceHandle, OpenSCManagerW, OpenServiceW, SC_MANAGER_CONNECT, SERVICE_QUERY_STATUS};

use crate::service::{self, State};

fn service_present(name: &str) -> bool {
    let manager = unsafe { OpenSCManagerW(std::ptr::null(), std::ptr::null(), SC_MANAGER_CONNECT) };
    if manager.is_null() {
        return false;
    }
    let service = unsafe { OpenServiceW(manager, wide(name).as_ptr(), SERVICE_QUERY_STATUS) };
    let present = !service.is_null();
    if present {
        unsafe { CloseServiceHandle(service) };
    }
    unsafe { CloseServiceHandle(manager) };
    present
}

/// What `surogate-vm check` prints: the three facts the backend turns into Section 11's words.
pub fn check() -> String {
    let vmp = service_present("vmcompute") && service_present("HNS");
    let service = matches!(service::state(), Ok(State::Running(_) | State::Stopped));
    let host_compute = match hcs::api::service_properties(r#"{"PropertyTypes":["Basic"]}"#) {
        Ok(answer) if supports(&answer, SCHEMA) => "ok",
        Ok(_) => "old",
        Err(_) => "absent",
    };
    serde_json::json!({ "virtualMachinePlatform": vmp, "service": if service { "present" } else { "missing" }, "hostCompute": host_compute }).to_string()
}
