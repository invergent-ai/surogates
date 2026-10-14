//! `surogate-admin apply --wait <pid> --manifest <path> --signature <path> --msi <path> --version <x.y.z>`, each in this
//! order, as surogate-helper's `update` gives them: the app's process, which is waited for; the release's manifest,
//! its signature and its MSI, as the app downloaded them into its user's cache; and the version they are of. And
//! `surogate-admin remove --wait <pid>`, as its `remove` gives it: the app's process, waited for before the uninstall.

use std::ffi::OsString;

use crate::release::version_ok;

/// An apply's arguments.
#[derive(Debug, PartialEq, Eq)]
pub struct Apply {
    pub wait: u32,
    pub manifest: OsString,
    pub signature: OsString,
    pub msi: OsString,
    pub version: String,
}

// A file by its whole path on a drive (X:\…), with no quote and no NUL in it: what the app names, and nothing that a
// command line or a relative path would read as another.
fn whole_path(path: &OsString) -> bool {
    path.to_str().is_some_and(|text| {
        let bytes = text.as_bytes();
        bytes.len() > 3 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':' && bytes[2] == b'\\' && !text.contains(['"', '\0'])
    })
}

/// What a run is asked: an update applied, or the app removed.
#[derive(Debug, PartialEq, Eq)]
pub enum Asked {
    Apply(Apply),
    Remove { wait: u32 },
}

// A process's id in decimal, as the app gives its own.
fn pid(text: &OsString) -> Option<u32> {
    let text = text.to_str().filter(|text| !text.is_empty() && text.len() <= 10 && text.bytes().all(|byte| byte.is_ascii_digit()))?;
    text.parse().ok().filter(|&pid| pid > 0)
}

/// *args*, after the program's name, as a run takes them, or None.
pub fn asked(args: &[OsString]) -> Option<Asked> {
    if let [command, wait_flag, wait] = args {
        let flags = [command, wait_flag].map(|flag| flag.to_str());
        return (flags == [Some("remove"), Some("--wait")]).then(|| pid(wait)).flatten().map(|wait| Asked::Remove { wait });
    }
    parsed(args).map(Asked::Apply)
}

/// *args*, after the program's name, as an apply takes them, or None.
pub fn parsed(args: &[OsString]) -> Option<Apply> {
    let [command, wait_flag, wait, manifest_flag, manifest, signature_flag, signature, msi_flag, msi, version_flag, version] = args else { return None };
    let flags = [command, wait_flag, manifest_flag, signature_flag, msi_flag, version_flag].map(|flag| flag.to_str());
    if flags != [Some("apply"), Some("--wait"), Some("--manifest"), Some("--signature"), Some("--msi"), Some("--version")] {
        return None;
    }
    let wait = pid(wait)?;
    let version = version.to_str().filter(|text| version_ok(text))?.to_string();
    [manifest, signature, msi].into_iter().all(whole_path).then(|| Apply { wait, manifest: manifest.clone(), signature: signature.clone(), msi: msi.clone(), version })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(list: &[&str]) -> Vec<OsString> {
        list.iter().map(OsString::from).collect()
    }

    const GOOD: [&str; 11] = [
        "apply", "--wait", "4242", "--manifest", "C:\\Users\\me\\AppData\\Local\\Surogate\\updates\\1.2.3\\manifest.json", "--signature",
        "C:\\Users\\me\\AppData\\Local\\Surogate\\updates\\1.2.3\\manifest.json.sig", "--msi", "C:\\Users\\me\\AppData\\Local\\Surogate\\updates\\1.2.3\\release.msi",
        "--version", "1.2.3",
    ];

    #[test]
    fn takes_an_apply_s_arguments_in_their_order() {
        let apply = parsed(&args(&GOOD)).expect("an apply");
        assert_eq!(apply.wait, 4242);
        assert_eq!(apply.version, "1.2.3");
        assert_eq!(apply.msi, OsString::from(GOOD[8]));
    }

    #[test]
    fn takes_a_remove_of_the_app_s_process() {
        assert_eq!(asked(&args(&["remove", "--wait", "77"])), Some(Asked::Remove { wait: 77 }));
        assert!(matches!(asked(&args(&GOOD)), Some(Asked::Apply(_))));
        for bad in [&["remove", "--wait"][..], &["remove", "--wait", "0"], &["remove", "--pid", "77"], &["Remove", "--wait", "77"], &["remove", "--wait", "77", "x"]] {
            assert_eq!(asked(&args(bad)), None, "{bad:?}");
        }
    }

    #[test]
    fn refuses_any_other_form() {
        assert_eq!(parsed(&args(&GOOD[..10])), None, "one short");
        let mut swapped = GOOD;
        swapped.swap(3, 5);
        assert_eq!(parsed(&args(&swapped)), None, "flags out of order");
        for (at, bad) in [
            (0, "Apply"), (2, "0"), (2, "-1"), (2, "12345678901"), (2, "4 2"), (4, "manifest.json"), (4, "\\\\server\\share\\m.json"), (4, "C:\\a\"b"),
            (8, "C:"), (10, "1.2"), (10, "1.02.3"), (10, "v1.2.3"),
        ] {
            let mut list = GOOD;
            list[at] = bad;
            assert_eq!(parsed(&args(&list)), None, "{bad} at {at}");
        }
    }
}
