// surogate-service.exe: run by the service control manager as the Surogate VM service (server.rs).

#[cfg(windows)]
fn main() {
    std::process::exit(service::server::main());
}

#[cfg(not(windows))]
fn main() {
    eprintln!("surogate-service runs on Windows alone");
    std::process::exit(2);
}
