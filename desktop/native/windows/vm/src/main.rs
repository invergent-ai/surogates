// surogate-vm.exe: the user's side of one VM (lib.rs). `run` drives one VM for the app's VM manager; `check` says
// what this computer has of what the sandbox runs on. Windows alone: the user's VM is a Hyper-V VM.

#[cfg(windows)]
fn main() {
    use vm::{command, Command};
    let arguments: Vec<String> = std::env::args().skip(1).collect();
    match command(&arguments) {
        Ok(Command::Run(run)) => std::process::exit(vm::run::run(run)),
        Ok(Command::Check) => {
            println!("{}", vm::check::check());
        }
        Err(usage) => {
            eprintln!("{usage}");
            std::process::exit(2);
        }
    }
}

#[cfg(not(windows))]
fn main() {
    eprintln!("surogate-vm runs on Windows alone");
    std::process::exit(2);
}
