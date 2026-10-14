//! surogate-helper <command>: one answer a run, on stdout, and its words on stderr where it has none. It runs as the
//! user who starts it, never elevated, reads no environment and no file of the caller's but the program `verify` or `version` is
//! given, and starts no program but the one `spawn` is given, and, for `update`, surogate-admin.exe and the app. The app's TypeScript reads each answer (src/shell/ca-source-win32.ts,
//! src/browser/processes-win32.ts, src/browser/known-win32.ts), and starts every program that is not its own through
//! `spawn` (src/clean-child.ts).
//!
//!   trust             the company's root certificates, as PEM: those an administrator or a policy put in the
//!                     computer's or Group Policy's Root stores that Windows would trust for TLS, none it distrusts
//!                     (windows/trust.rs); nothing where there are none
//!   procs             this user's processes, a JSON object a line: {"pid":…,"path":…,"args":[…]}
//!   verify <path>     the program's Authenticode signature, one JSON object: {"trusted":…,"signer":…,"status":…,"remote":…}
//!   app-path <name>   where the App Paths key says the program <name> is: HKLM's, then HKCU's, a line each
//!   look <path>       nothing: whether <path> is there, opened through its links, by the run's end alone (0 or 1)
//!   version <path>    the program's product version, from its version resource, a line
//!   spawn <keep> <program> <args>…
//!                     the program, by its whole path, holding the helper's first <keep> + 1 descriptors and nothing
//!                     else of its caller's, in the helper's place: its output is the program's, and the helper ends as
//!                     it ends, taking it along if killed first (spawn.rs, windows/spawn.rs)
//!   update --app-pid <pid> --staged <folder> --version <x.y.z>
//!                     the update staged in <folder> installed by surogate-admin.exe, once Windows's prompt has an
//!                     administrator's approval: "approved", a line, as the app is to quit; its exit code the apply's,
//!                     1223 where none approved; then the app started again (update.rs, windows/update.rs)
//!   remove --app-pid <pid> [--forget <folder>]…
//!                     the app uninstalled by surogate-admin.exe, the same way; then each <folder>, this user's,
//!                     removed once the uninstall ended well
//!
//! A Windows-subsystem program: started by an app that has no console, as the installed app started from the Start
//! menu has none, it gives Windows no reason to make one (Microsoft, "Creation of a Console": a console process gets a
//! new console where its parent has none). Its standard handles are the pipes its caller gives it.
#![windows_subsystem = "windows"]

use std::ffi::OsString;
use std::io::Write;

const USAGE: &str = "usage: surogate-helper trust | procs | verify <path> | app-path <name> | look <path> | version <path> | spawn <keep> <program> <args>… | update --app-pid <pid> --staged <folder> --version <x.y.z> | remove --app-pid <pid> [--forget <folder>]…";

fn main() {
    let args: Vec<OsString> = std::env::args_os().skip(1).collect();
    // A program in the helper's place says nothing of the helper's: its output is the program's own.
    if args.first().and_then(|first| first.to_str()) == Some("spawn") {
        spawn(&args[1..]);
    }
    // An update and a removal say their one line midway, and end as the admin program ends.
    if args.first().and_then(|first| first.to_str()) == Some("update") {
        std::process::exit(update(&args[1..]));
    }
    if args.first().and_then(|first| first.to_str()) == Some("remove") {
        std::process::exit(remove(&args[1..]));
    }
    let (code, out, words) = match run(&args) {
        Answer::Said(text) => (0, text, None),
        Answer::Failed(words) => (1, String::new(), Some(words)),
        Answer::Usage => (2, String::new(), Some(USAGE.to_string())),
        Answer::Not => (1, String::new(), None),
    };
    // A reader that has gone, or a full pipe that fails, ends the run as a failure: nothing half said is an answer.
    if std::io::stdout().lock().write_all(out.as_bytes()).and_then(|()| std::io::stdout().lock().flush()).is_err() {
        std::process::exit(1);
    }
    if let Some(words) = words {
        let _ = writeln!(std::io::stderr().lock(), "{words}");
    }
    std::process::exit(code);
}

/// What a run says: its answer, its words where it has none, or how it is run.
#[cfg_attr(not(windows), allow(dead_code))]
enum Answer {
    Said(String),
    Failed(String),
    Usage,
    // No, said by the run's end alone.
    Not,
}

impl From<Result<String, String>> for Answer {
    fn from(result: Result<String, String>) -> Self {
        result.map_or_else(Answer::Failed, Answer::Said)
    }
}

#[cfg(windows)]
fn run(args: &[OsString]) -> Answer {
    use surogate_helper::{pem, windows};

    let command = args.first().and_then(|first| first.to_str());
    match (command, args.len()) {
        (Some("trust"), 1) => windows::trust::roots().map(|roots| roots.iter().map(|der| pem::certificate(der)).collect()).into(),
        (Some("procs"), 1) => windows::procs::listed().map(|listed| listed.iter().map(|one| one.json() + "\n").collect()).into(),
        (Some("verify"), 2) => windows::verify::verdict(&args[1]).map(|verdict| verdict.json() + "\n").into(),
        (Some("version"), 2) => match windows::version::of(&args[1]) {
            Some(version) => Answer::Said(format!("{version}\n")),
            None => Answer::Failed(format!("{} has no version", args[1].to_string_lossy())),
        },
        (Some("look"), 2) => if windows::look::there(&args[1]) { Answer::Said(String::new()) } else { Answer::Not },
        (Some("app-path"), 2) => match args[1].to_str() {
            Some(name) => windows::app_path::paths(name).map(|paths| paths.iter().map(|path| format!("{path}\n")).collect()).into(),
            None => Answer::Usage,
        },
        _ => Answer::Usage,
    }
}

// Elsewhere it is built for its tests alone: what it answers is Windows's.
#[cfg(not(windows))]
fn run(_args: &[OsString]) -> Answer {
    Answer::Failed("surogate-helper answers on Windows alone".to_string())
}

fn update(args: &[OsString]) -> i32 {
    let Some(update) = surogate_helper::update::parsed(args) else {
        let _ = writeln!(std::io::stderr().lock(), "{USAGE}");
        return 2;
    };
    #[cfg(windows)]
    return surogate_helper::windows::update::run(&update);
    #[cfg(not(windows))]
    {
        let _ = update;
        let _ = writeln!(std::io::stderr().lock(), "surogate-helper updates on Windows alone");
        1
    }
}

fn remove(args: &[OsString]) -> i32 {
    let Some(removal) = surogate_helper::update::removal(args) else {
        let _ = writeln!(std::io::stderr().lock(), "{USAGE}");
        return 2;
    };
    #[cfg(windows)]
    return surogate_helper::windows::update::remove(&removal);
    #[cfg(not(windows))]
    {
        let _ = removal;
        let _ = writeln!(std::io::stderr().lock(), "surogate-helper removes on Windows alone");
        1
    }
}

fn spawn(args: &[OsString]) -> ! {
    let Some(spawn) = surogate_helper::spawn::parsed(args) else {
        let _ = writeln!(std::io::stderr().lock(), "{USAGE}");
        std::process::exit(2);
    };
    #[cfg(windows)]
    surogate_helper::windows::spawn::run(&spawn);
    #[cfg(not(windows))]
    {
        let _ = (spawn.keep, spawn.program, spawn.args);
        let _ = writeln!(std::io::stderr().lock(), "surogate-helper starts a program on Windows alone");
        std::process::exit(2);
    }
}
