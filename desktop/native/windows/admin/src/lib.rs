//! surogate-admin's checks (src/main.rs runs them): what it takes as its arguments, the release keys the installed app
//! trusts, and the release a manifest names, the same on every OS and tested on each; and Windows's own calls that copy,
//! check and install an update.

// Built elsewhere for its tests alone, what only Windows's calls use is used there alone.
#![cfg_attr(not(windows), allow(dead_code))]

pub mod args;
pub mod keys;
pub mod release;
#[cfg(windows)]
pub mod windows;

/// How a run ends: installed; a failure of its own, said on stderr; how it is run; a release that is not this app's
/// (its signature, its hash, its signer, its channel or its version), nothing changed; Windows Installer's failure,
/// its code said, and nothing changed, as the MSI rolls back; and the app not ended within its time, nothing changed.
pub mod exit {
    pub const INSTALLED: i32 = 0;
    pub const FAILED: i32 = 1;
    pub const USAGE: i32 = 2;
    pub const NOT_THIS_APPS: i32 = 3;
    pub const INSTALLER_FAILED: i32 = 4;
    pub const DID_NOT_QUIT: i32 = 5;
}

/// How long the app has to end once the update is approved.
pub const QUIT_SECONDS: u32 = 60;
