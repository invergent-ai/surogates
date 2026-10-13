//! surogate-helper's answers (src/main.rs runs them): what it prints, the same on every OS and tested on each, and
//! Windows's own calls that fill it.

// Built elsewhere for its tests alone, what only Windows's calls use is used there alone.
#![cfg_attr(not(windows), allow(dead_code))]

pub mod answers;
pub mod json;
pub mod pem;
#[cfg(windows)]
pub mod windows;
