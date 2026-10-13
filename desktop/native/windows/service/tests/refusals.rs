// The service's refusals on Windows itself, against its own code: a caller the service does not serve, an oversized
// message, and a file the caller may not reach. The protocol's refusals of a wrong-shaped request, an eighth share and
// the rest are the core's, tested on every OS (service::core); these are the three that need Windows.

#![cfg(windows)]

use std::sync::atomic::{AtomicU32, Ordering};
use std::thread;
use std::time::{Duration, Instant};

use pipe::win::{Handle, Pipe, Way};
use pipe::{Lines, Read};
use pipe::Answer;
use service::core::Kind;
use service::files::reached;
use service::server::{serve_one, PIPE_SDDL};
use windows_sys::Win32::Foundation::HANDLE;
use windows_sys::Win32::Security::{
    CreateRestrictedToken, CreateWellKnownSid, DuplicateTokenEx, RevertToSelf, SecurityImpersonation, SID_AND_ATTRIBUTES, TokenImpersonation, TOKEN_ALL_ACCESS,
    TOKEN_DUPLICATE, WinInteractiveSid, SECURITY_MAX_SID_SIZE,
};
use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken, SetThreadToken};

// A pipe name of the test's own, each unique.
fn name() -> String {
    static NEXT: AtomicU32 = AtomicU32::new(0);
    format!(r"\\.\pipe\surogate-vm-test-{}-{}", std::process::id(), NEXT.fetch_add(1, Ordering::SeqCst))
}

// A scratch folder of the test's own, under the runner's temp folder, by its canonical long path (the temp folder may
// carry a short name, which the service's walk refuses as another name): canonicalize, and drop the \\?\ prefix so
// it is a drive-absolute path again.
fn scratch(leaf: &str) -> String {
    let base = std::env::temp_dir().join(format!("surogate-refusals-{}-{leaf}", std::process::id()));
    let _ = std::fs::remove_dir_all(&base);
    std::fs::create_dir_all(&base).unwrap();
    let real = std::fs::canonicalize(&base).unwrap();
    let text = real.to_string_lossy().into_owned();
    text.strip_prefix(r"\\?\").map(str::to_owned).unwrap_or(text)
}

// The service serving a client that connects to a pipe of *sddl*, on a thread; the client's end.
fn served(sddl: &str) -> (Pipe, thread::JoinHandle<()>) {
    let name = name();
    let server = Pipe::server(&name, Some(sddl), Way::Duplex, true, true).unwrap();
    let accepting = thread::spawn(move || {
        server.accept().unwrap();
        serve_one(server, None);
    });
    let mut client = None;
    for _ in 0..200 {
        match Pipe::open(&name) {
            Ok(pipe) => { client = Some(pipe); break; }
            Err(_) => thread::sleep(Duration::from_millis(10)),
        }
    }
    (client.expect("the client opened the pipe"), accepting)
}

// The one line the service answers with, by a deadline.
fn answer(client: &Pipe) -> Answer {
    let mut lines = Lines::new();
    let mut buffer = vec![0u8; 8192];
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Some(Read::Line(line)) = lines.next_line() {
            return serde_json::from_str(&line).expect("an answer");
        }
        assert!(Instant::now() < deadline, "the service said nothing");
        let read = client.read(&mut buffer).unwrap();
        assert!(read > 0, "the service closed its pipe without answering");
        lines.push(&buffer[..read]);
    }
}

#[test]
fn refuses_a_caller_the_service_does_not_serve() {
    // A copy of this process's token with INTERACTIVE made deny-only, so the service's "an interactive user" check
    // fails though the pipe let the caller in (CreateRestrictedToken, a deny-only SID).
    let restricted = unsafe {
        let mut token: HANDLE = std::ptr::null_mut();
        assert!(OpenProcessToken(GetCurrentProcess(), TOKEN_DUPLICATE | TOKEN_ALL_ACCESS, &mut token) != 0);
        let token = Handle(token);
        let mut primary: HANDLE = std::ptr::null_mut();
        assert!(DuplicateTokenEx(token.0, TOKEN_ALL_ACCESS, std::ptr::null(), SecurityImpersonation, TokenImpersonation, &mut primary) != 0);
        let primary = Handle(primary);
        let mut sid = [0u8; SECURITY_MAX_SID_SIZE as usize];
        let mut size = SECURITY_MAX_SID_SIZE;
        assert!(CreateWellKnownSid(WinInteractiveSid, std::ptr::null_mut(), sid.as_mut_ptr().cast(), &mut size) != 0);
        let disable = [SID_AND_ATTRIBUTES { Sid: sid.as_mut_ptr().cast(), Attributes: 0 }];
        let mut made: HANDLE = std::ptr::null_mut();
        assert!(CreateRestrictedToken(primary.0, 0, 1, disable.as_ptr(), 0, std::ptr::null(), 0, std::ptr::null(), &mut made) != 0);
        Handle(made)
    };
    let name = name();
    let server = Pipe::server(&name, Some("D:P(A;;GA;;;SY)(A;;GA;;;WD)"), Way::Duplex, true, true).unwrap();
    let serving = thread::spawn(move || { server.accept().unwrap(); serve_one(server, None); });
    // The client opens the pipe as the restricted user, so that is the token the service reads.
    let client = {
        assert!(unsafe { SetThreadToken(std::ptr::null(), restricted.0) } != 0);
        let opened = (0..200).find_map(|_| Pipe::open(&name).ok().or_else(|| { thread::sleep(Duration::from_millis(10)); None })).expect("opened");
        unsafe { RevertToSelf() };
        opened
    };
    client.write_all(b"{\"type\":\"kill\",\"id\":1}\n").unwrap();
    assert!(matches!(answer(&client), Answer::Refused { why, .. } if why.contains("interactive")), "a wrong caller is refused");
    drop(client);
    let _ = serving.join();
}

#[test]
fn refuses_an_oversized_message() {
    let (client, serving) = served(PIPE_SDDL);
    let mut line = vec![b'x'; 70 * 1024];
    line.push(b'\n');
    client.write_all(&line).unwrap();
    assert!(matches!(answer(&client), Answer::Refused { why, .. } if why.contains("64 KiB")));
    drop(client);
    let _ = serving.join();
}

#[test]
fn the_service_pipe_grants_a_client_no_right_to_make_an_instance() {
    let name = name();
    // The pipe with the service's descriptor, unlimited instances, first instance ours.
    let server = Pipe::server(&name, Some(PIPE_SDDL), Way::Duplex, true, false).unwrap();
    let accepting = thread::spawn(move || {
        let _ = server.accept();
        thread::sleep(Duration::from_millis(300));
        drop(server);
    });
    // A client opens with exactly the rights a client needs (pipe::win), so the open succeeds under the narrowed DACL.
    let mut client = None;
    for _ in 0..200 {
        if let Ok(pipe) = Pipe::open(&name) {
            client = Some(pipe);
            break;
        }
        thread::sleep(Duration::from_millis(5));
    }
    assert!(client.is_some(), "a client opens the service pipe with a client's rights");
    // A second instance, as this interactive (non-SYSTEM) user, is refused: the descriptor grants interactive users no
    // FILE_CREATE_PIPE_INSTANCE. (Only SYSTEM, which the GitHub runner's admin user is not, may make another.)
    let second = Pipe::server(&name, None, Way::Duplex, false, false);
    assert!(second.is_err(), "an interactive user cannot make another instance of the service pipe");
    drop(client);
    let _ = accepting.join();
}

#[test]
fn reaches_a_file_as_the_caller_and_refuses_what_it_is_not() {
    let root = scratch("files");
    // A plain file and a folder, each reached from the root the caller owns; the wrong kind and a missing name refused.
    let file = format!(r"{root}\image\rootfs.vhd");
    std::fs::create_dir_all(format!(r"{root}\image")).unwrap();
    std::fs::write(&file, b"disk").unwrap();
    assert!(reached(&root, &file, Kind::File).is_ok());
    assert!(reached(&root, &format!(r"{root}\image"), Kind::Folder).is_ok());
    assert!(reached(&root, &file, Kind::Folder).is_err());
    assert!(reached(&root, &format!(r"{root}\image"), Kind::File).is_err());
    assert!(reached(&root, &format!(r"{root}\image\missing"), Kind::File).is_err());
}

#[test]
fn refuses_a_reparse_point_on_the_way() {
    let root = scratch("reparse");
    let outside = scratch("reparse-outside");
    std::fs::write(format!(r"{outside}\secret.vhd"), b"outside").unwrap();
    // A leaf symbolic link to a file the caller can read is refused as a reparse point (the leaf is not followed).
    let file = format!(r"{root}\rootfs.vhd");
    std::fs::write(&file, b"disk").unwrap();
    let link = format!(r"{root}\link.vhd");
    if std::os::windows::fs::symlink_file(&file, &link).is_ok() {
        assert!(matches!(reached(&root, &link, Kind::File), Err(why) if why.contains("reparse point")), "a leaf symbolic link is refused");
    }
    // A directory junction as an ancestor, leading outside the root: a file reached through it is refused, the
    // junction never followed, though the caller could read the target.
    let junction = format!(r"{root}\j");
    if std::process::Command::new("cmd").args(["/c", "mklink", "/J", &junction, &outside]).output().map(|out| out.status.success()).unwrap_or(false) {
        assert!(matches!(reached(&root, &format!(r"{junction}\secret.vhd"), Kind::File), Err(why) if why.contains("reparse point")), "an ancestor junction is refused");
    }
    // A directory symbolic link as an ancestor: refused the same way.
    let dirlink = format!(r"{root}\d");
    if std::os::windows::fs::symlink_dir(&outside, &dirlink).is_ok() {
        assert!(matches!(reached(&root, &format!(r"{dirlink}\secret.vhd"), Kind::File), Err(why) if why.contains("reparse point")), "an ancestor directory symlink is refused");
    }
}

#[test]
fn holds_the_chain_so_a_swap_after_the_check_fails() {
    let root = scratch("swap");
    std::fs::create_dir_all(format!(r"{root}\image")).unwrap();
    let file = format!(r"{root}\image\rootfs.vhd");
    std::fs::write(&file, b"disk").unwrap();
    // While the chain's handles are held, no component can be renamed or removed: a swap of the folder on the way, or
    // of the file itself, fails. HCS reopens by string, so the held handles are what stop a swap mid-flight.
    let held = reached(&root, &file, Kind::File).expect("the file is reached");
    assert!(std::fs::rename(format!(r"{root}\image"), format!(r"{root}\image-swapped")).is_err(), "the held folder cannot be renamed");
    assert!(std::fs::remove_file(&file).is_err(), "the held file cannot be removed");
    drop(held);
    // Once the handles are let go, the same operations succeed.
    assert!(std::fs::rename(format!(r"{root}\image"), format!(r"{root}\image-swapped")).is_ok(), "once let go, the folder can be renamed");
}
