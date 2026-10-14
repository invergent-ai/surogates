//! An update installed through UAC (update.rs): surogate-admin.exe, beside the helper, started elevated by
//! ShellExecuteExW's "runas", which shows Windows's own prompt for an administrator; ERROR_CANCELLED where none approved.
//! Once one has, the app is told on stdout and quits, the apply is waited for, how it ended is kept in the staged folder,
//! and the installed app is started again as this user, by the helper, which is never elevated.

use std::ffi::OsString;
use std::io::Write;
use std::path::Path;
use std::process::{Command, Stdio};
use std::ptr::null_mut;

use windows_sys::Win32::Foundation::{CloseHandle, GetLastError};
use windows_sys::Win32::System::Com::{CoInitializeEx, COINIT_APARTMENTTHREADED, COINIT_DISABLE_OLE1DDE};
use windows_sys::Win32::System::Threading::{GetExitCodeProcess, WaitForSingleObject, INFINITE};
use windows_sys::Win32::UI::Shell::{ShellExecuteExW, SEE_MASK_FLAG_NO_UI, SEE_MASK_NOASYNC, SEE_MASK_NOCLOSEPROCESS, SHELLEXECUTEINFOW};

use super::wide;
use crate::spawn::arguments;
use crate::update::{admin_args, result, Update, APPROVED, DISMISSED, RESULT};

const ERROR_CANCELLED: u32 = 1223;

/// The update run: its exit code, the apply's, or DISMISSED, or 1 where it could not be asked; its words on stderr.
pub fn run(update: &Update<'_>) -> i32 {
    let fail = |words: String| {
        let _ = writeln!(std::io::stderr().lock(), "{words}");
        1
    };
    // The helper's own folder, <install>\bin, holds surogate-admin.exe; the app is <install>\Surogate.exe.
    let Some(bin) = std::env::current_exe().ok().and_then(|exe| exe.parent().map(Path::to_path_buf)) else {
        return fail("the helper's own folder could not be read".to_string());
    };
    let admin = bin.join("surogate-admin.exe");
    let Some(app) = bin.parent().map(|install| install.join("Surogate.exe")) else {
        return fail("the helper is in no install".to_string());
    };
    let args: Vec<Vec<u16>> = admin_args(update).iter().map(|arg| wide(arg).split_last().map(|(_, units)| units.to_vec()).unwrap_or_default()).collect();
    let mut parameters = arguments(&args);
    parameters.push(0);
    let verb = wide("runas");
    let file = wide(&admin);
    // As ShellExecuteExW asks of its caller, for whatever handles the verb.
    unsafe { CoInitializeEx(null_mut(), (COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE) as u32) };
    let mut info = SHELLEXECUTEINFOW {
        cbSize: size_of::<SHELLEXECUTEINFOW>() as u32,
        fMask: SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC | SEE_MASK_FLAG_NO_UI,
        lpVerb: verb.as_ptr(),
        lpFile: file.as_ptr(),
        lpParameters: parameters.as_ptr(),
        nShow: 0,
        ..unsafe { std::mem::zeroed() }
    };
    // SAFETY: every string the structure names outlives the call.
    if unsafe { ShellExecuteExW(&mut info) } == 0 {
        let error = unsafe { GetLastError() };
        if error == ERROR_CANCELLED {
            let _ = writeln!(std::io::stderr().lock(), "No administrator approved the update");
            return DISMISSED;
        }
        return fail(format!("{} could not be started as an administrator: Windows error {error}", admin.display()));
    }
    if info.hProcess.is_null() {
        return fail(format!("{} gave no process to wait for", admin.display()));
    }
    // The app quits on this line; nothing is written to it after.
    let mut out = std::io::stdout().lock();
    let _ = writeln!(out, "{APPROVED}").and_then(|()| out.flush());
    drop(out);
    let mut code = 1u32;
    // SAFETY: the process handle ShellExecuteExW gave, closed here.
    unsafe {
        WaitForSingleObject(info.hProcess, INFINITE);
        GetExitCodeProcess(info.hProcess, &mut code);
        CloseHandle(info.hProcess);
    }
    let code = code as i32;
    // How it ended, for the app's next start: by a rename, so that no half of it is ever read.
    let staged = Path::new(update.staged);
    let partial = staged.join(format!("{RESULT}.partial"));
    let _ = std::fs::write(&partial, result(update.version, code)).and_then(|()| std::fs::rename(&partial, staged.join(RESULT)));
    // The installed app again, as this user, whichever version is installed now.
    let _ = Command::new(OsString::from(app.as_os_str())).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn();
    code
}
