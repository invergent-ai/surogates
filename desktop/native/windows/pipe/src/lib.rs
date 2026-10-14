// The Surogate VM service's pipe, \\.\pipe\surogate-vm, and what goes over it: one JSON object a line, each line at
// most MAX_LINE bytes, from a caller the service trusts with nothing. The lines (protocol.rs), their reading within
// the bound (lines.rs), and the form of every path a line may name (path.rs) are plain data, and build and test on any
// OS; the pipes themselves are Windows's (win.rs).

pub mod lines;
pub mod path;
pub mod protocol;

#[cfg(windows)]
pub mod win;

pub use lines::{Lines, Read, MAX_LINE};
pub use protocol::{Answer, Reason, Request};

/// The service's pipe.
pub const SERVICE_PIPE: &str = r"\\.\pipe\surogate-vm";
