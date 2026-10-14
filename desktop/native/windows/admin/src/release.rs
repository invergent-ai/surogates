//! The release a manifest names, as the app took it (signedRelease, src/shell/updates.ts), checked again: one of the
//! release keys the install trusts signed its exact bytes, and it is a release of the install's channel for Windows on
//! x64, at its one place, newer than the version installed. The app checks first; what it took and this refused would
//! be offered and then refused, so the two take the same.

use serde::Deserialize;

/// A manifest is one short line, and its signature Ed25519's 64 bytes.
pub const MANIFEST_MAX: usize = 4096;
pub const SIGNATURE_SIZE: usize = 64;

/// What the release is, as its manifest names it.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Manifest {
    version: String,
    channel: String,
    platform: String,
    arch: String,
    url: String,
    sha256: String,
    size: u64,
    state_schema: u64,
}

/// The MSI the release is: its hash and size, which its copy must have.
#[derive(Debug, PartialEq, Eq)]
pub struct Release {
    pub sha256: String,
    pub size: u64,
}

/// Whether *text* is a version, x.y.z, with no part that has a zero before it.
pub fn version_ok(text: &str) -> bool {
    let parts: Vec<&str> = text.split('.').collect();
    parts.len() == 3 && parts.iter().all(|part| !part.is_empty() && part.len() <= 9 && part.bytes().all(|byte| byte.is_ascii_digit()) && (part.len() == 1 || !part.starts_with('0')))
}

/// Whether version *a* comes after *b*, each x.y.z, compared part by part as numbers.
pub fn newer(a: &str, b: &str) -> bool {
    if !version_ok(a) || !version_ok(b) {
        return false;
    }
    let numbers = |text: &str| text.split('.').map(|part| part.parse::<u64>().unwrap_or(0)).collect::<Vec<_>>();
    numbers(a) > numbers(b)
}

/// The release *manifest* names, once one of *keys* signed it in *signature*: of *version*, which is the one asked
/// for, and newer than *installed*; of *channel*; for Windows on x64, at its one place. Why not, where it is not.
pub fn checked(manifest: &[u8], signature: &[u8], keys: &[[u8; 32]], version: &str, channel: &str, installed: &str) -> Result<Release, String> {
    let signed = <[u8; SIGNATURE_SIZE]>::try_from(signature).ok().is_some_and(|signature| {
        let signature = ed25519_compact::Signature::new(signature);
        keys.iter().any(|key| ed25519_compact::PublicKey::new(*key).verify(manifest, &signature).is_ok())
    });
    if !signed {
        return Err("the release's manifest is not signed by a release key this install trusts".to_string());
    }
    // One line, whose newline is its last byte, of one JSON object.
    let line = manifest.strip_suffix(b"\n").filter(|line| manifest.len() <= MANIFEST_MAX && !line.contains(&b'\n'));
    let release: Manifest = line.and_then(|line| serde_json::from_slice(line).ok()).ok_or("the release's manifest is not one")?;
    let place = format!("releases/{version}/win32-x64/surogate-desktop-{version}-win32-x64.msi");
    let counted = |value: u64| (1..1_000_000_000_000_000).contains(&value);
    let hash = release.sha256.len() == 64 && release.sha256.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte));
    if release.version != version || release.platform != "win32" || release.arch != "x64" || release.url != place || !hash || !counted(release.size) || !counted(release.state_schema) {
        return Err(format!("the release's manifest names no release of Surogate {version} for Windows on x64"));
    }
    if release.channel != channel {
        return Err(format!("the release is of the channel {}, and this install takes {channel}", release.channel));
    }
    if !newer(version, installed) {
        return Err(format!("Surogate {version} is not newer than the version installed, {installed}"));
    }
    Ok(Release { sha256: release.sha256, size: release.size })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn keypair() -> ed25519_compact::KeyPair {
        ed25519_compact::KeyPair::from_seed(ed25519_compact::Seed::new([7; 32]))
    }

    fn manifest(version: &str, channel: &str) -> Vec<u8> {
        format!(
            "{{\"version\":\"{version}\",\"channel\":\"{channel}\",\"platform\":\"win32\",\"arch\":\"x64\",\"url\":\"releases/{version}/win32-x64/surogate-desktop-{version}-win32-x64.msi\",\"sha256\":\"{}\",\"size\":1234,\"stateSchema\":1}}\n",
            "ab".repeat(32)
        )
        .into_bytes()
    }

    fn signed(bytes: &[u8]) -> Vec<u8> {
        keypair().sk.sign(bytes, None).to_vec()
    }

    #[test]
    fn takes_a_signed_release_of_this_install_that_is_newer() {
        let keys = [*keypair().pk];
        let bytes = manifest("1.3.0", "stable");
        assert_eq!(checked(&bytes, &signed(&bytes), &keys, "1.3.0", "stable", "1.2.9"), Ok(Release { sha256: "ab".repeat(32), size: 1234 }));
    }

    #[test]
    fn refuses_a_manifest_no_trusted_key_signed_its_exact_bytes() {
        let keys = [*keypair().pk];
        let bytes = manifest("1.3.0", "stable");
        let other = ed25519_compact::KeyPair::from_seed(ed25519_compact::Seed::new([8; 32]));
        assert!(checked(&bytes, other.sk.sign(&bytes, None).as_ref(), &keys, "1.3.0", "stable", "1.2.9").unwrap_err().contains("not signed"));
        let mut changed = bytes.clone();
        changed[12] ^= 1;
        assert!(checked(&changed, &signed(&bytes), &keys, "1.3.0", "stable", "1.2.9").unwrap_err().contains("not signed"));
        assert!(checked(&bytes, &signed(&bytes)[..63], &keys, "1.3.0", "stable", "1.2.9").unwrap_err().contains("not signed"));
        assert!(checked(&bytes, &signed(&bytes), &[], "1.3.0", "stable", "1.2.9").unwrap_err().contains("not signed"));
    }

    #[test]
    fn refuses_a_release_of_another_version_channel_place_or_kind_and_one_not_newer() {
        let keys = [*keypair().pk];
        let check = |bytes: &[u8], version: &str, installed: &str| checked(bytes, &signed(bytes), &keys, version, "stable", installed);
        assert!(check(&manifest("1.3.0", "stable"), "1.3.1", "1.2.9").is_err(), "another version than asked");
        assert!(check(&manifest("1.3.0", "beta"), "1.3.0", "1.2.9").unwrap_err().contains("channel beta"));
        assert!(check(&manifest("1.3.0", "stable"), "1.3.0", "1.3.0").unwrap_err().contains("not newer"), "the same version");
        assert!(check(&manifest("1.3.0", "stable"), "1.3.0", "1.10.0").unwrap_err().contains("not newer"), "an older one");
        for (from, to) in [("win32", "linux"), ("x64", "arm64"), ("win32-x64.msi", "linux-x64.tar.gz"), ("\"size\":1234", "\"size\":0"), ("\"stateSchema\":1", "\"stateSchema\":0")] {
            let bytes = String::from_utf8(manifest("1.3.0", "stable")).unwrap().replacen(from, to, 1).into_bytes();
            assert!(check(&bytes, "1.3.0", "1.2.9").is_err(), "{from} → {to}");
        }
    }

    #[test]
    fn refuses_a_manifest_that_is_not_one_line_of_one_object() {
        let keys = [*keypair().pk];
        let one = manifest("1.3.0", "stable");
        let line = &one[..one.len() - 1];
        let twice = String::from_utf8(one.clone()).unwrap().replacen("\"arch\":\"x64\"", "\"arch\":\"x64\",\"arch\":\"x64\"", 1);
        for bytes in [line.to_vec(), [line, b"\n\n"].concat(), [b"[", line, b"]\n"].concat(), twice.into_bytes(), [&one[..], &vec![b' '; MANIFEST_MAX]].concat()] {
            assert!(checked(&bytes, &signed(&bytes), &keys, "1.3.0", "stable", "1.2.9").is_err(), "{}", String::from_utf8_lossy(&bytes));
        }
    }

    #[test]
    fn reads_versions_as_x_y_z_numbers() {
        assert!(newer("1.10.0", "1.9.9"));
        assert!(newer("2.0.0", "1.99.99"));
        assert!(!newer("1.2.3", "1.2.3"));
        assert!(!newer("1.02.3", "1.0.0"), "a part with a zero before it is no version");
        for bad in ["1.2", "1.2.3.4", "", "a.b.c", "1..2", "01.2.3", "1.2.3 "] {
            assert!(!version_ok(bad), "{bad}");
        }
        assert!(version_ok("0.0.0"));
    }
}
