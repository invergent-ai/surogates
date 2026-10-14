//! surogate-admin apply --wait <pid> --manifest <path> --signature <path> --msi <path> --version <x.y.z>: an update of
//! the app installed, once an administrator approved it through UAC (surogate-helper's `update` asks Windows for one,
//! and starts this). Its exit code is its answer (lib.rs, exit), its words on stderr.
//!
//! It waits for the app to end, up to a minute; copies the release's manifest, its signature and its MSI into
//! %ProgramData%\Surogate\apply\<version>\, which only administrators and SYSTEM reach, and checks the copies alone,
//! never the user's files again: the release keys the install trusts signed the manifest, which names a release of the
//! install's channel newer than the version installed, the MSI is the one the manifest names by its hash and size, and
//! its Authenticode signer is this program's own. Then Windows Installer installs it, from the copy.
#![windows_subsystem = "windows"]

use std::ffi::OsString;
use std::io::Write;

use surogate_admin::exit;

const USAGE: &str = "usage: surogate-admin apply --wait <pid> --manifest <path> --signature <path> --msi <path> --version <x.y.z>";

fn main() {
    let args: Vec<OsString> = std::env::args_os().skip(1).collect();
    let (code, words) = match surogate_admin::args::parsed(&args) {
        Some(apply) => run(&apply),
        None => (exit::USAGE, USAGE.to_string()),
    };
    if !words.is_empty() {
        let _ = writeln!(std::io::stderr().lock(), "{words}");
    }
    std::process::exit(code);
}

#[cfg(windows)]
fn run(apply: &surogate_admin::args::Apply) -> (i32, String) {
    surogate_admin::windows::apply(apply)
}

// Elsewhere it is built for its tests alone: what it does is Windows's.
#[cfg(not(windows))]
fn run(_apply: &surogate_admin::args::Apply) -> (i32, String) {
    (exit::FAILED, "surogate-admin installs on Windows alone".to_string())
}
