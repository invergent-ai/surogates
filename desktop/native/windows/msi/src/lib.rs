//! The MSI's own custom actions (windows/msi/Surogate.wxs), in the MSI's Binary table and never installed, each deferred
//! and run as LocalSystem:
//!
//! - `TakeBackDataFolders`, before the MSI makes its folders of `%ProgramData%`: a `Surogate` or `apply` folder that is
//!   there already, which any user may have made, as `%ProgramData%` lets its users make folders, is the install's again
//!   (folders.rs).
//! - `EnableVirtualMachinePlatform`, once the files are in place. It turns on Windows's Virtual Machine Platform, which
//!   the Host Compute Service that the Surogate VM service calls needs, with DISM, as WSL's installer turns it on (WSL's
//!   `WslInstall.cpp`): `dism.exe /Online /Enable-Feature /FeatureName:VirtualMachinePlatform /NoRestart`, and `/Quiet`,
//!   as no one reads its words. DISM's 3010 (ERROR_SUCCESS_REBOOT_REQUIRED) says the feature is on once Windows
//!   restarts: the action marks the install as needing that restart as the WiX Toolset's own deferred actions mark one
//!   (`WcaDeferredActionRequiresReboot`, a global atom that its `CheckRebootRequired` action reads at the install's end
//!   and turns into `MsiSetMode(MSIRUNMODE_REBOOTATEND)`), so Windows asks for it then. Any other answer leaves the
//!   feature as DISM left it and the install goes on, said in its log: the app says what the computer lacks of its
//!   sandbox, and how to turn it on (src/vm/win32.ts). Where an administrator turns the feature on by policy, the MSI's
//!   SUROGATE_VMP=0 runs none of it, at this install and at every later one, which remembers it.

pub mod folders;
#[cfg(windows)]
mod take_back;

/// The DISM line that turns the feature on, after its program's path.
pub const DISM: [&str; 5] = ["/Online", "/Enable-Feature", "/FeatureName:VirtualMachinePlatform", "/NoRestart", "/Quiet"];

/// The global atom that the WiX Toolset's `CheckRebootRequired` action reads, by its name in WiX's `wcawrap.cpp`.
pub const RESTART_ATOM: &str = "WcaDeferredActionRequiresReboot";

/// What DISM's exit code says of the feature.
#[derive(Debug, PartialEq, Eq)]
pub enum Turned {
    /// It is on, as it was already or with no restart.
    On,
    /// It is on once Windows restarts.
    AtRestart,
    /// DISM did not turn it on: its exit code, or none where it could not be started or was ended.
    Not(Option<i32>),
}

/// The feature as DISM's exit *code* leaves it.
pub fn turned(code: Option<i32>) -> Turned {
    match code {
        Some(0) => Turned::On,
        Some(3010) => Turned::AtRestart,
        other => Turned::Not(other),
    }
}

/// What the install's log says of *turned*.
pub fn said(turned: &Turned) -> String {
    match turned {
        Turned::On => "Surogate: Windows's Virtual Machine Platform is on".to_owned(),
        Turned::AtRestart => "Surogate: Windows's Virtual Machine Platform is on once Windows restarts".to_owned(),
        Turned::Not(Some(code)) => format!("Surogate: DISM did not turn on Windows's Virtual Machine Platform: it exited {code}"),
        Turned::Not(None) => "Surogate: DISM did not turn on Windows's Virtual Machine Platform: it could not run".to_owned(),
    }
}

#[cfg(windows)]
pub(crate) mod action {
    use std::os::windows::process::CommandExt;
    use std::path::PathBuf;
    use std::process::{Command, Stdio};

    use windows_sys::Win32::System::ApplicationInstallationAndServicing::{MsiCloseHandle, MsiCreateRecord, MsiProcessMessage, MsiRecordSetStringW, INSTALLMESSAGE_INFO, MSIHANDLE};
    use windows_sys::Win32::System::DataExchange::GlobalAddAtomW;
    use windows_sys::Win32::System::SystemInformation::GetSystemDirectoryW;
    use windows_sys::Win32::System::Threading::CREATE_NO_WINDOW;

    use super::{said, turned, Turned, DISM, RESTART_ATOM};

    pub(crate) fn wide(text: &str) -> Vec<u16> {
        text.encode_utf16().chain(Some(0)).collect()
    }

    /// Windows's own System32, where its DISM is: never one a PATH names.
    fn system32() -> Option<PathBuf> {
        let mut buffer = [0u16; 512];
        let length = unsafe { GetSystemDirectoryW(buffer.as_mut_ptr(), buffer.len() as u32) } as usize;
        (length > 0 && length < buffer.len()).then(|| PathBuf::from(String::from_utf16_lossy(&buffer[..length])))
    }

    /// *words* in the install's log.
    pub(crate) fn log(install: MSIHANDLE, words: &str) {
        unsafe {
            let record = MsiCreateRecord(1);
            if record == 0 {
                return;
            }
            MsiRecordSetStringW(record, 0, wide(words).as_ptr());
            MsiProcessMessage(install, INSTALLMESSAGE_INFO, record);
            MsiCloseHandle(record);
        }
    }

    /// The custom action: Windows's Virtual Machine Platform turned on, its restart asked for where it needs one. It
    /// never fails the install.
    #[no_mangle]
    pub extern "system" fn EnableVirtualMachinePlatform(install: MSIHANDLE) -> u32 {
        // With no window: a deferred action runs where no one sees one.
        let code = system32()
            .and_then(|folder| {
                Command::new(folder.join("dism.exe"))
                    .args(DISM)
                    .stdin(Stdio::null())
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .creation_flags(CREATE_NO_WINDOW)
                    .status()
                    .ok()
            })
            .and_then(|status| status.code());
        let turned = turned(code);
        if turned == Turned::AtRestart {
            unsafe { GlobalAddAtomW(wide(RESTART_ATOM).as_ptr()) };
        }
        log(install, &said(&turned));
        0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_dism_s_exit_codes_as_wsl_s_installer_reads_them() {
        assert_eq!(turned(Some(0)), Turned::On);
        assert_eq!(turned(Some(3010)), Turned::AtRestart);
        for code in [Some(1), Some(87), Some(740), Some(-2146498530), None] {
            assert_eq!(turned(code), Turned::Not(code));
        }
    }

    #[test]
    fn says_each_answer_in_the_install_s_log() {
        assert_eq!(said(&Turned::On), "Surogate: Windows's Virtual Machine Platform is on");
        assert_eq!(said(&Turned::AtRestart), "Surogate: Windows's Virtual Machine Platform is on once Windows restarts");
        assert_eq!(said(&Turned::Not(Some(87))), "Surogate: DISM did not turn on Windows's Virtual Machine Platform: it exited 87");
        assert_eq!(said(&Turned::Not(None)), "Surogate: DISM did not turn on Windows's Virtual Machine Platform: it could not run");
    }

    #[test]
    fn turns_the_feature_on_as_wsl_s_installer_does_and_waits_for_no_one() {
        assert_eq!(DISM, ["/Online", "/Enable-Feature", "/FeatureName:VirtualMachinePlatform", "/NoRestart", "/Quiet"]);
        assert_eq!(RESTART_ATOM, "WcaDeferredActionRequiresReboot");
    }
}
