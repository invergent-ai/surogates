// The Surogate VM service (surogate-service.exe): LocalSystem, started on demand, the only program of the app's that
// calls the Host Compute Service, for the users who ask over its pipe. What it decides is core.rs's, on any OS; who
// asks, what they may reach and HCS itself are Windows's (caller.rs, files.rs, hyper.rs), and so is the service's
// life: its pipe, its connections, and its end once no VM has run for five minutes (server.rs).

pub mod core;

#[cfg(windows)]
pub mod caller;
#[cfg(windows)]
pub mod files;
#[cfg(windows)]
pub mod hyper;
#[cfg(windows)]
pub mod server;
