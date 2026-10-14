//! `surogate-helper update --app-pid <pid> --staged <folder> --version <x.y.z>`: an update the app downloaded into
//! *folder* installed through UAC. What is read and written for it is here, the same on every OS and tested on each;
//! windows/update.rs asks Windows for an administrator to run surogate-admin.exe's apply, says so to the app, which then
//! quits, waits for the apply, keeps how it ended in the folder for the app's next start, and starts the app again.
//! And `surogate-helper remove --app-pid <pid> [--forget <folder>]…`: the app uninstalled the same way, by
//! surogate-admin.exe's remove, and then each folder of this user's that the app named removed, once the uninstall ended
//! well.

use std::ffi::{OsStr, OsString};

/// The file of the staged folder that keeps how the apply ended, for the app to read at its next start.
pub const RESULT: &str = "result.json";
/// What the helper says, a line on stdout, once an administrator approved the update: the app quits on it.
pub const APPROVED: &str = "approved";
/// The exit code Windows gives where the administrator's approval was not given (ERROR_CANCELLED).
pub const DISMISSED: i32 = 1223;

/// An update's arguments.
#[derive(Debug, PartialEq, Eq)]
pub struct Update<'a> {
    pub app_pid: u32,
    pub staged: &'a OsStr,
    pub version: &'a str,
}

// x.y.z, no part with a zero before it, as the app's VERSION and surogate-admin's have it.
fn version_ok(text: &str) -> bool {
    let parts: Vec<&str> = text.split('.').collect();
    parts.len() == 3 && parts.iter().all(|part| !part.is_empty() && part.len() <= 9 && part.bytes().all(|byte| byte.is_ascii_digit()) && (part.len() == 1 || !part.starts_with('0')))
}

/// *args* after `update`, as it takes them, or None: the app's pid in decimal, the staged folder by its whole path on a
/// drive (X:\…) with no quote in it, and the version.
pub fn parsed(args: &[OsString]) -> Option<Update<'_>> {
    let [pid_flag, pid, staged_flag, staged, version_flag, version] = args else { return None };
    if [pid_flag, staged_flag, version_flag].map(|flag| flag.to_str()) != [Some("--app-pid"), Some("--staged"), Some("--version")] {
        return None;
    }
    let pid = pid.to_str().filter(|text| !text.is_empty() && text.len() <= 10 && text.bytes().all(|byte| byte.is_ascii_digit()))?;
    let app_pid: u32 = pid.parse().ok().filter(|&pid| pid > 0)?;
    let path = staged.to_str()?;
    let bytes = path.as_bytes();
    let drive = bytes.len() > 3 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':' && bytes[2] == b'\\' && !path.contains(['"', '\0']);
    let version = version.to_str().filter(|text| version_ok(text))?;
    drive.then_some(Update { app_pid, staged, version })
}

/// surogate-admin.exe's arguments for *update*: its apply of the staged release's three files, waiting for the app.
pub fn admin_args(update: &Update<'_>) -> Vec<OsString> {
    let staged = update.staged.to_string_lossy();
    let file = |name: &str| OsString::from(format!("{}\\{name}", staged.trim_end_matches('\\')));
    vec![
        "apply".into(),
        "--wait".into(),
        update.app_pid.to_string().into(),
        "--manifest".into(),
        file("manifest.json"),
        "--signature".into(),
        file("manifest.json.sig"),
        "--msi".into(),
        file("release.msi"),
        "--version".into(),
        update.version.into(),
    ]
}

/// A removal's arguments: the app's process, and the folders of the user's own to remove after it.
#[derive(Debug, PartialEq, Eq)]
pub struct Remove<'a> {
    pub app_pid: u32,
    pub forget: Vec<&'a OsStr>,
}

// A folder by its whole path on a drive, with no quote in it.
fn on_a_drive(path: &OsStr) -> bool {
    path.to_str().is_some_and(|path| {
        let bytes = path.as_bytes();
        bytes.len() > 3 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':' && bytes[2] == b'\\' && !path.contains(['"', '\0'])
    })
}

/// *args* after `remove`, as it takes them, or None: the app's pid, then `--forget` and a folder on a drive, each as often
/// as the app names one.
pub fn removal(args: &[OsString]) -> Option<Remove<'_>> {
    let (head, rest) = args.split_at_checked(2)?;
    let [pid_flag, pid] = head else { return None };
    if pid_flag.to_str() != Some("--app-pid") {
        return None;
    }
    let pid = pid.to_str().filter(|text| !text.is_empty() && text.len() <= 10 && text.bytes().all(|byte| byte.is_ascii_digit()))?;
    let app_pid: u32 = pid.parse().ok().filter(|&pid| pid > 0)?;
    let (pairs, odd) = rest.as_chunks::<2>();
    if !odd.is_empty() {
        return None;
    }
    let forget = pairs.iter().map(|[flag, folder]| (flag.to_str() == Some("--forget") && on_a_drive(folder)).then_some(folder.as_os_str())).collect::<Option<Vec<_>>>()?;
    Some(Remove { app_pid, forget })
}

/// surogate-admin.exe's arguments for *removal*: its remove, waiting for the app.
pub fn remove_args(removal: &Remove<'_>) -> Vec<OsString> {
    vec!["remove".into(), "--wait".into(), removal.app_pid.to_string().into()]
}

/// How an apply of *version* ended, as its result file holds it: one line of JSON.
pub fn result(version: &str, code: i32) -> String {
    format!("{{\"version\":\"{version}\",\"code\":{code}}}\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(list: &[&str]) -> Vec<OsString> {
        list.iter().map(OsString::from).collect()
    }

    const GOOD: [&str; 6] = ["--app-pid", "777", "--staged", "C:\\Users\\me\\AppData\\Local\\Surogate\\Cache\\updates\\1.4.0", "--version", "1.4.0"];

    #[test]
    fn takes_an_update_s_arguments_and_gives_surogate_admin_its_own() {
        let given = args(&GOOD);
        let update = parsed(&given).expect("an update");
        assert_eq!(update, Update { app_pid: 777, staged: OsStr::new(GOOD[3]), version: "1.4.0" });
        let folder = GOOD[3];
        assert_eq!(
            admin_args(&update),
            args(&[
                "apply", "--wait", "777", "--manifest", &format!("{folder}\\manifest.json"), "--signature", &format!("{folder}\\manifest.json.sig"), "--msi",
                &format!("{folder}\\release.msi"), "--version", "1.4.0",
            ])
        );
    }

    #[test]
    fn refuses_any_other_form() {
        assert_eq!(parsed(&args(&GOOD[..4])), None);
        for (at, bad) in [(0, "--pid"), (1, "0"), (1, "x"), (3, "updates\\1.4.0"), (3, "C:\\a\"b"), (5, "1.4"), (5, "01.4.0")] {
            let mut list = GOOD;
            list[at] = bad;
            assert_eq!(parsed(&args(&list)), None, "{bad} at {at}");
        }
    }

    #[test]
    fn takes_a_removal_and_the_folders_to_forget_after_it() {
        let given = args(&["--app-pid", "9", "--forget", "C:\\Users\\me\\AppData\\Roaming\\Surogate", "--forget", "C:\\Users\\me\\AppData\\Local\\Surogate"]);
        let removal = removal(&given).expect("a removal");
        assert_eq!(removal.app_pid, 9);
        assert_eq!(removal.forget, [OsStr::new("C:\\Users\\me\\AppData\\Roaming\\Surogate"), OsStr::new("C:\\Users\\me\\AppData\\Local\\Surogate")]);
        assert_eq!(remove_args(&removal), args(&["remove", "--wait", "9"]));
        assert_eq!(super::removal(&args(&["--app-pid", "9"])).map(|removal| removal.forget.len()), Some(0), "nothing to forget");
        for bad in [&["--app-pid"][..], &["--app-pid", "0"], &["--app-pid", "9", "--forget"], &["--app-pid", "9", "--forget", "Surogate"], &["--app-pid", "9", "--keep", "C:\\x"]] {
            assert_eq!(super::removal(&args(bad)), None, "{bad:?}");
        }
    }

    #[test]
    fn keeps_how_an_apply_ended_as_one_line() {
        assert_eq!(result("1.4.0", 0), "{\"version\":\"1.4.0\",\"code\":0}\n");
        assert_eq!(result("1.4.0", 4), "{\"version\":\"1.4.0\",\"code\":4}\n");
        assert_eq!(DISMISSED, 1223);
    }
}
