// The Host Compute Service as Surogate's VM service uses it: the documents it builds for a guest's VM, each from the
// few fields of a request, and the calls it makes with them. The documents are plain data and build on any OS; the
// calls are Windows's alone (api.rs), through computecore.dll, which is loaded from System32 at run time.
//
// Every field is cited to Microsoft's "JSON Schema Reference" of the Host Compute Service (learn.microsoft.com,
// virtualization/api/hcs/SchemaReference), with the schema version it is new in; what the reference leaves out is
// cited to the source that shows it.

pub mod document;
pub mod failure;
pub mod hvsock;
pub mod properties;
pub mod share;
pub mod sid;

#[cfg(windows)]
pub mod api;

pub use document::{document, Machine, SCHEMA};
pub use failure::Failure;
pub use hvsock::service_id;
pub use share::{add_share, remove_share, ShareFlags, PLAN9_PORT};
pub use sid::{is_sid, socket_descriptor};
