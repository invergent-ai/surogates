// The Host Compute Service's calls (Microsoft Learn, virtualization/api/hcs/Reference), through computecore.dll,
// loaded at run time from System32 alone (LoadLibraryExW, LOAD_LIBRARY_SEARCH_SYSTEM32), so no DLL of the same name
// beside the program is ever loaded in its place, and a computer without the Host Compute Service gets words rather
// than a program that cannot start. Each call that takes an HCS_OPERATION is waited on with HcsWaitForOperationResult
// within a bound; one that does not finish in it is left to run, and its failure said.

use std::ffi::c_void;
use std::sync::{Mutex, OnceLock};

use windows_sys::Win32::Foundation::{LocalFree, HANDLE};
use windows_sys::Win32::System::LibraryLoader::{GetProcAddress, LoadLibraryExW, LOAD_LIBRARY_SEARCH_SYSTEM32};

use crate::failure::Failure;

type Operation = *mut c_void;
type SystemHandle = *mut c_void;

// HCS_EVENT (ComputeDefs.h): Type, EventData (a JSON document), Operation.
#[repr(C)]
struct Event {
    kind: u32,
    data: *const u16,
    operation: Operation,
}
// HCS_EVENT_TYPE's HcsEventSystemExited.
const SYSTEM_EXITED: u32 = 0x1;
// HCS_EVENT_CALLBACK.
type EventCallback = unsafe extern "system" fn(event: *const Event, context: *const c_void);

// HCS_E_OPERATION_TIMEOUT: HcsWaitForOperationResult's "The operation didn't complete within the specified wait time".
const OPERATION_TIMEOUT: i32 = 0x8037_0118_u32 as i32;

// Each call's form, as computecore.h declares it.
type CreateOperation = unsafe extern "system" fn(*const c_void, *const c_void) -> Operation;
type CloseOperation = unsafe extern "system" fn(Operation);
type WaitForOperationResult = unsafe extern "system" fn(Operation, u32, *mut *mut u16) -> i32;
type CreateComputeSystem = unsafe extern "system" fn(*const u16, *const u16, Operation, *const c_void, *mut SystemHandle) -> i32;
type OperateComputeSystem = unsafe extern "system" fn(SystemHandle, Operation, *const u16) -> i32;
type CloseComputeSystem = unsafe extern "system" fn(SystemHandle);
type ModifyComputeSystem = unsafe extern "system" fn(SystemHandle, Operation, *const u16, HANDLE) -> i32;
type SetComputeSystemCallback = unsafe extern "system" fn(SystemHandle, u32, *const c_void, EventCallback) -> i32;
type VmAccess = unsafe extern "system" fn(*const u16, *const u16) -> i32;
type GetServiceProperties = unsafe extern "system" fn(*const u16, *mut *mut u16) -> i32;

struct Calls {
    // HcsCreateOperation(context, callback) -> HCS_OPERATION.
    create_operation: CreateOperation,
    // HcsCloseOperation(operation).
    close_operation: CloseOperation,
    // HcsWaitForOperationResult(operation, timeoutMs, resultDocument).
    wait_for_operation_result: WaitForOperationResult,
    // HcsCreateComputeSystem(id, configuration, operation, securityDescriptor ("Reserved for future use, must be
    // NULL"), computeSystem).
    create_compute_system: CreateComputeSystem,
    // HcsStartComputeSystem(computeSystem, operation, options), and HcsTerminateComputeSystem, the same.
    start_compute_system: OperateComputeSystem,
    terminate_compute_system: OperateComputeSystem,
    // HcsCloseComputeSystem(computeSystem).
    close_compute_system: CloseComputeSystem,
    // HcsModifyComputeSystem(computeSystem, operation, configuration, identity: "Optional handle to an access token
    // that is used when applying the settings").
    modify_compute_system: ModifyComputeSystem,
    // HcsGetComputeSystemProperties(computeSystem, operation, propertyQuery).
    get_compute_system_properties: OperateComputeSystem,
    // HcsSetComputeSystemCallback(computeSystem, callbackOptions, context, callback).
    set_compute_system_callback: SetComputeSystemCallback,
    // HcsGrantVmAccess(vmId, filePath): "Path to the file for which to update the ACL", and HcsRevokeVmAccess, its
    // undoing.
    grant_vm_access: VmAccess,
    revoke_vm_access: VmAccess,
    // HcsGetServiceProperties(propertyQuery, result).
    get_service_properties: GetServiceProperties,
}

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(Some(0)).collect()
}

// A string HCS allocated, read and freed (each call's remarks: "The caller is responsible for releasing the returned
// string using LocalFree").
unsafe fn taken(text: *mut u16) -> Option<String> {
    if text.is_null() {
        return None;
    }
    let mut length = 0;
    while *text.add(length) != 0 {
        length += 1;
    }
    let read = String::from_utf16_lossy(std::slice::from_raw_parts(text, length));
    LocalFree(text.cast());
    Some(read)
}

fn calls() -> Result<&'static Calls, Failure> {
    static CALLS: OnceLock<Result<Calls, String>> = OnceLock::new();
    CALLS
        .get_or_init(|| unsafe {
            let module = LoadLibraryExW(wide("computecore.dll").as_ptr(), std::ptr::null_mut(), LOAD_LIBRARY_SEARCH_SYSTEM32);
            if module.is_null() {
                return Err("this computer has no Host Compute Service: computecore.dll is not in System32".to_owned());
            }
            macro_rules! call {
                ($name:literal, $form:ty) => {{
                    let found = GetProcAddress(module, concat!($name, "\0").as_ptr());
                    match found {
                        Some(found) => std::mem::transmute::<unsafe extern "system" fn() -> isize, $form>(found),
                        None => return Err(format!("computecore.dll has no {}", $name)),
                    }
                }};
            }
            Ok(Calls {
                create_operation: call!("HcsCreateOperation", CreateOperation),
                close_operation: call!("HcsCloseOperation", CloseOperation),
                wait_for_operation_result: call!("HcsWaitForOperationResult", WaitForOperationResult),
                create_compute_system: call!("HcsCreateComputeSystem", CreateComputeSystem),
                start_compute_system: call!("HcsStartComputeSystem", OperateComputeSystem),
                terminate_compute_system: call!("HcsTerminateComputeSystem", OperateComputeSystem),
                close_compute_system: call!("HcsCloseComputeSystem", CloseComputeSystem),
                modify_compute_system: call!("HcsModifyComputeSystem", ModifyComputeSystem),
                get_compute_system_properties: call!("HcsGetComputeSystemProperties", OperateComputeSystem),
                set_compute_system_callback: call!("HcsSetComputeSystemCallback", SetComputeSystemCallback),
                grant_vm_access: call!("HcsGrantVmAccess", VmAccess),
                revoke_vm_access: call!("HcsRevokeVmAccess", VmAccess),
                get_service_properties: call!("HcsGetServiceProperties", GetServiceProperties),
            })
        })
        .as_ref()
        .map_err(|words| Failure { code: -1, words: words.clone() })
}

// *call*, tracked by an operation of its own, waited on for *ms*: the operation's result document, or its failure.
fn operated(what: &str, ms: u32, call: impl FnOnce(&Calls, Operation) -> i32) -> Result<Option<String>, Failure> {
    let calls = calls()?;
    unsafe {
        let operation = (calls.create_operation)(std::ptr::null(), std::ptr::null());
        if operation.is_null() {
            return Err(Failure::of(&format!("{what}: HcsCreateOperation"), -1, None));
        }
        let code = call(calls, operation);
        if code < 0 {
            (calls.close_operation)(operation);
            return Err(Failure::of(what, code, None));
        }
        let mut result: *mut u16 = std::ptr::null_mut();
        let code = (calls.wait_for_operation_result)(operation, ms, &mut result);
        let document = taken(result);
        if code == OPERATION_TIMEOUT {
            // Still running: closing it now could end a call HCS has not finished with, so it is left.
            return Err(Failure::of(&format!("{what} did not finish in {} s, and", ms / 1000), code, None));
        }
        (calls.close_operation)(operation);
        if code < 0 {
            return Err(Failure::of(what, code, document.as_deref()));
        }
        Ok(document)
    }
}

/// What the Host Compute Service says of itself: its answer to *query* (HcsGetServiceProperties).
pub fn service_properties(query: &str) -> Result<String, Failure> {
    let calls = calls()?;
    let mut result: *mut u16 = std::ptr::null_mut();
    unsafe {
        let code = (calls.get_service_properties)(wide(query).as_ptr(), &mut result);
        let answer = taken(result);
        if code < 0 {
            return Err(Failure::of("HcsGetServiceProperties", code, answer.as_deref()));
        }
        Ok(answer.unwrap_or_default())
    }
}

/// HcsGrantVmAccess: *path*'s ACL given an entry for the VM *id*. Made while the service impersonates the caller,
/// so the caller's own right to change the file's ACL is what is used (WSL grants a user's disk so, WslCoreVm.cpp).
pub fn grant_vm_access(id: &str, path: &str) -> Result<(), Failure> {
    let calls = calls()?;
    let code = unsafe { (calls.grant_vm_access)(wide(id).as_ptr(), wide(path).as_ptr()) };
    if code < 0 {
        return Err(Failure::of("HcsGrantVmAccess", code, None));
    }
    Ok(())
}

/// HcsRevokeVmAccess: the entry grant_vm_access made, taken away; by the identity that made it.
pub fn revoke_vm_access(id: &str, path: &str) -> Result<(), Failure> {
    let calls = calls()?;
    let code = unsafe { (calls.revoke_vm_access)(wide(id).as_ptr(), wide(path).as_ptr()) };
    if code < 0 {
        return Err(Failure::of("HcsRevokeVmAccess", code, None));
    }
    Ok(())
}

/// Told of a system's exit: its SystemExitStatus document, where HCS gives one.
pub type Exited = Box<dyn Fn(Option<String>) + Send + Sync>;

/// One compute system, by the one handle the service holds: closed when this goes, which ends the VM
/// (ShouldTerminateOnLastHandleClosed). Its exit is told once to what `on_exit` was given.
pub struct System {
    handle: SystemHandle,
    // What the exit callback's context points at, kept until the handle is closed.
    exited: Mutex<Option<Box<Exited>>>,
}

// HCS_SYSTEM handles are used from any thread: the service's connection's, and HCS's own callbacks.
unsafe impl Send for System {}
unsafe impl Sync for System {}

unsafe extern "system" fn told(event: *const Event, context: *const c_void) {
    if event.is_null() || context.is_null() {
        return;
    }
    let event = &*event;
    if event.kind == SYSTEM_EXITED {
        let exited = &*(context as *const Exited);
        let data = if event.data.is_null() {
            None
        } else {
            let mut length = 0;
            while *event.data.add(length) != 0 {
                length += 1;
            }
            Some(String::from_utf16_lossy(std::slice::from_raw_parts(event.data, length)))
        };
        exited(data);
    }
}

impl System {
    /// HcsCreateComputeSystem: the system *id* made from *document*, not started.
    pub fn create(id: &str, document: &str) -> Result<System, Failure> {
        let mut handle: SystemHandle = std::ptr::null_mut();
        let (id, document) = (wide(id), wide(document));
        operated("HcsCreateComputeSystem", 120_000, |calls, operation| unsafe {
            (calls.create_compute_system)(id.as_ptr(), document.as_ptr(), operation, std::ptr::null(), &mut handle)
        })?;
        if handle.is_null() {
            return Err(Failure::of("HcsCreateComputeSystem gave no system, and", -1, None));
        }
        Ok(System { handle, exited: Mutex::new(None) })
    }

    /// *exited* told once the system exits (HcsSetComputeSystemCallback, HcsEventOptionNone).
    pub fn on_exit(&self, exited: Exited) -> Result<(), Failure> {
        let calls = calls()?;
        let boxed = Box::new(exited);
        let context = &*boxed as *const Exited as *const c_void;
        let code = unsafe { (calls.set_compute_system_callback)(self.handle, 0, context, told) };
        if code < 0 {
            return Err(Failure::of("HcsSetComputeSystemCallback", code, None));
        }
        *self.exited.lock().unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(boxed);
        Ok(())
    }

    /// HcsStartComputeSystem.
    pub fn start(&self) -> Result<(), Failure> {
        operated("HcsStartComputeSystem", 120_000, |calls, operation| unsafe { (calls.start_compute_system)(self.handle, operation, std::ptr::null()) }).map(drop)
    }

    /// HcsTerminateComputeSystem: the VM ended at once.
    pub fn terminate(&self) -> Result<(), Failure> {
        operated("HcsTerminateComputeSystem", 30_000, |calls, operation| unsafe { (calls.terminate_compute_system)(self.handle, operation, std::ptr::null()) }).map(drop)
    }

    /// HcsModifyComputeSystem of *request*, a ModifySettingRequest, applied with the token *identity*.
    ///
    /// # Safety
    /// *identity* is an open access token's handle, or null.
    pub unsafe fn modify(&self, request: &str, identity: HANDLE) -> Result<(), Failure> {
        let request = wide(request);
        operated("HcsModifyComputeSystem", 60_000, |calls, operation| unsafe { (calls.modify_compute_system)(self.handle, operation, request.as_ptr(), identity) }).map(drop)
    }

    /// HcsGetComputeSystemProperties of *query*, or of none (the system's basic properties, as WSL asks for its
    /// RuntimeId, hcs.cpp, GetRuntimeId): the system's Properties document.
    pub fn properties(&self, query: Option<&str>) -> Result<String, Failure> {
        let query = query.map(wide);
        operated("HcsGetComputeSystemProperties", 30_000, |calls, operation| unsafe {
            (calls.get_compute_system_properties)(self.handle, operation, query.as_ref().map_or(std::ptr::null(), |query| query.as_ptr()))
        })
        .map(Option::unwrap_or_default)
    }
}

impl Drop for System {
    fn drop(&mut self) {
        if let Ok(calls) = calls() {
            unsafe { (calls.close_compute_system)(self.handle) };
        }
        // No callback comes once the handle is closed: what it was told through can go.
        self.exited.lock().unwrap_or_else(|poisoned| poisoned.into_inner()).take();
    }
}
