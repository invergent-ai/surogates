//! An update installed, or the app removed, through UAC (update.rs): surogate-admin.exe, beside the helper, started
//! elevated by ShellExecuteExW's "runas", which shows Windows's own prompt for an administrator; ERROR_CANCELLED where
//! none approved. Once one has, the app is told on stdout and quits, and the admin program is waited for. An update's
//! end is kept in the staged folder, and the installed app started again as this user; a removal's, once it ended well,
//! removes the folders of this user's it was told to forget. The helper itself is never elevated.

use std::ffi::OsString;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::ptr::null_mut;

use windows_sys::Win32::Foundation::{CloseHandle, GetLastError};
use windows_sys::Win32::System::Com::{CoInitializeEx, COINIT_APARTMENTTHREADED, COINIT_DISABLE_OLE1DDE};
use windows_sys::Win32::System::Threading::{GetExitCodeProcess, WaitForSingleObject, INFINITE};
use windows_sys::Win32::UI::Shell::{ShellExecuteExW, SEE_MASK_FLAG_NO_UI, SEE_MASK_NOASYNC, SEE_MASK_NOCLOSEPROCESS, SHELLEXECUTEINFOW};

use super::wide;
use crate::spawn::arguments;
use crate::update::{admin_args, remove_args, result, Remove, Update, APPROVED, DISMISSED, RESULT};

const ERROR_CANCELLED: u32 = 1223;

// The helper's own folder, <install>\bin, which holds surogate-admin.exe; the app is <install>\Surogate.exe.
fn programs() -> Result<(PathBuf, PathBuf), String> {
    let bin = std::env::current_exe().ok().and_then(|exe| exe.parent().map(Path::to_path_buf)).ok_or("the helper's own folder could not be read")?;
    let app = bin.parent().map(|install| install.join("Surogate.exe")).ok_or("the helper is in no install")?;
    Ok((bin.join("surogate-admin.exe"), app))
}

fn said(words: &str) {
    let _ = writeln!(std::io::stderr().lock(), "{words}");
}

/// *admin* run with *args* as an administrator, once Windows's prompt has one's approval, and "approved" said to the
/// app; waited for, its exit code. Before any approval, which leaves the app running: Err, DISMISSED where none
/// approved, 1 where it could not be asked.
fn elevated(admin: &Path, args: &[OsString]) -> Result<i32, i32> {
    let args: Vec<Vec<u16>> = args.iter().map(|arg| wide(arg).split_last().map(|(_, units)| units.to_vec()).unwrap_or_default()).collect();
    let mut parameters = arguments(&args);
    parameters.push(0);
    let verb = wide("runas");
    let file = wide(admin);
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
            said("No administrator approved it");
            return Err(DISMISSED);
        }
        said(&format!("{} could not be started as an administrator: Windows error {error}", admin.display()));
        return Err(1);
    }
    if info.hProcess.is_null() {
        said(&format!("{} gave no process to wait for", admin.display()));
        return Err(1);
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
    Ok(code as i32)
}

/// The update run: its exit code, the apply's, or DISMISSED, or 1 where it could not be asked; its words on stderr.
pub fn run(update: &Update<'_>) -> i32 {
    let (admin, app) = match programs() {
        Ok(found) => found,
        Err(why) => {
            said(&why);
            return 1;
        }
    };
    // Not approved, the app runs on, and is told how it ended now.
    let code = match elevated(&admin, &admin_args(update)) {
        Ok(code) => code,
        Err(code) => return code,
    };
    // How it ended, for the app's next start: by a rename, so that no half of it is ever read.
    let staged = Path::new(update.staged);
    let partial = staged.join(format!("{RESULT}.partial"));
    let _ = std::fs::write(&partial, result(update.version, code)).and_then(|()| std::fs::rename(&partial, staged.join(RESULT)));
    // The installed app again, as this user, whichever version is installed now.
    let _ = Command::new(OsString::from(app.as_os_str())).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn();
    code
}

/// The removal run: its exit code, the remove's, or DISMISSED, or 1; and, once the uninstall ended well, each folder it
/// was told to forget removed, as this user, through no link.
pub fn remove(removal: &Remove<'_>) -> i32 {
    let (admin, _) = match programs() {
        Ok(found) => found,
        Err(why) => {
            said(&why);
            return 1;
        }
    };
    let code = elevated(&admin, &remove_args(removal)).unwrap_or_else(|code| code);
    if code == 0 {
        for folder in &removal.forget {
            if let Err(error) = std::fs::remove_dir_all(folder) {
                if error.kind() != std::io::ErrorKind::NotFound {
                    said(&format!("{} could not be removed: {error}", Path::new(folder).display()));
                }
            }
        }
    }
    code
}
