//! The release keys the installed app trusts: the install's `resources\release-keys`, which the build writes in the list
//! form release/install.sh has (scripts/release-keys.mjs), read as the app reads one (listedKeys, src/shell/updates.ts):
//! line by line and by its one form. `RELEASE_KEYS=(` on a line of its own, then each key as OpenSSL writes an Ed25519
//! public key, its first line behind a quote, its one line of letters, its last before a quote, then `)` on a line of
//! its own. Any other spelling is no list, and no key of it is read.

const BEGIN_KEY: &str = "-----BEGIN PUBLIC KEY-----";
const END_KEY: &str = "-----END PUBLIC KEY-----";
// What an Ed25519 public key's DER begins with, in base64: its 12 bytes that say it is one, before the key's 32.
const ED25519_PREFIX: &str = "MCowBQYDK2VwAyEA";

// The bytes of base64's *text*, with its padding, or None for any other letter.
fn base64(text: &str) -> Option<Vec<u8>> {
    let value = |letter: u8| match letter {
        b'A'..=b'Z' => Some(letter - b'A'),
        b'a'..=b'z' => Some(letter - b'a' + 26),
        b'0'..=b'9' => Some(letter - b'0' + 52),
        b'+' => Some(62),
        b'/' => Some(63),
        _ => None,
    };
    let body = text.trim_end_matches('=');
    let mut bits = 0u32;
    let mut held = 0;
    let mut out = Vec::new();
    for letter in body.bytes() {
        bits = (bits << 6) | u32::from(value(letter)?);
        held += 6;
        if held >= 8 {
            held -= 8;
            out.push((bits >> held) as u8);
        }
    }
    Some(out)
}

// The key a line of letters holds: the Ed25519 prefix, the key's 42 letters, and a last letter that holds four bits of
// the key's last byte and none of its own, as the app's ED25519_LINE has it.
fn key_line(line: &str) -> Option<[u8; 32]> {
    let rest = line.strip_prefix(ED25519_PREFIX)?.strip_suffix('=')?;
    let (body, last) = rest.split_at_checked(42)?;
    let ok = body.bytes().all(|byte| byte.is_ascii_alphanumeric() || byte == b'+' || byte == b'/') && last.len() == 1 && "AEIMQUYcgkosw048".contains(last);
    if !ok {
        return None;
    }
    let der = base64(line)?;
    der.get(12..44)?.try_into().ok()
}

fn list_opens(line: &str) -> bool {
    line.trim_start_matches([' ', '\t']).strip_prefix("RELEASE_KEYS").is_some_and(|rest| rest.starts_with('=') || rest.starts_with("+="))
}

/// The keys *script* lists, each Ed25519 public key's 32 bytes, as an install would trust them; none for any other form.
pub fn listed(script: &str) -> Vec<[u8; 32]> {
    #[derive(PartialEq)]
    enum At {
        Before,
        Open,
        Key,
        Closed,
        Wrong,
    }
    let mut at = At::Before;
    let mut keys = Vec::new();
    let mut key: Option<[u8; 32]> = None;
    let mut lines: Vec<&str> = script.split('\n').collect();
    // As the script reads: a last line with no newline is a line, and the end behind a newline is none.
    if lines.last() == Some(&"") {
        lines.pop();
    }
    for line in lines {
        if list_opens(line) {
            if at == At::Before && line.trim_start_matches([' ', '\t']) == "RELEASE_KEYS=(" {
                at = At::Open;
                continue;
            }
            at = At::Wrong;
        }
        match at {
            At::Open => {
                if line.trim_start_matches([' ', '\t']) == format!("'{BEGIN_KEY}") {
                    key = None;
                    at = At::Key;
                } else {
                    at = if line.trim_start_matches([' ', '\t']) == ")" { At::Closed } else { At::Wrong };
                }
            }
            At::Key => match (key, key_line(line)) {
                (None, Some(found)) => key = Some(found),
                (Some(found), _) if line == format!("{END_KEY}'") => {
                    keys.push(found);
                    at = At::Open;
                }
                _ => at = At::Wrong,
            },
            _ => {}
        }
    }
    if at == At::Closed { keys } else { Vec::new() }
}

#[cfg(test)]
mod tests {
    use super::*;

    // An Ed25519 public key as OpenSSL writes one: the RFC 8032 test key 1's, d75a9801…
    const KEY: &str = "MCowBQYDK2VwAyEA11qYAYKxCrfVS/7TyWQHOg7hcvPapiMlrwIaaPcHURo=";
    const KEY_BYTES: [u8; 32] = [
        0xd7, 0x5a, 0x98, 0x01, 0x82, 0xb1, 0x0a, 0xb7, 0xd5, 0x4b, 0xfe, 0xd3, 0xc9, 0x64, 0x07, 0x3a, 0x0e, 0xe1, 0x72, 0xf3, 0xda, 0xa6, 0x23, 0x25, 0xaf,
        0x02, 0x1a, 0x68, 0xf7, 0x07, 0x51, 0x1a,
    ];

    fn list(keys: &[&str]) -> String {
        let entries: Vec<String> = keys.iter().map(|key| format!("  '{BEGIN_KEY}\n{key}\n{END_KEY}'")).collect();
        format!("RELEASE_KEYS=(\n{}\n)\n", entries.join("\n"))
    }

    #[test]
    fn reads_each_key_of_the_list_in_its_one_form() {
        assert_eq!(listed(&list(&[KEY])), vec![KEY_BYTES]);
        assert_eq!(listed(&list(&[KEY, KEY])), vec![KEY_BYTES, KEY_BYTES]);
        assert_eq!(listed(&format!("CHANNEL=stable\n{}tail\n", list(&[KEY]))), vec![KEY_BYTES], "around the list, what a script holds");
    }

    #[test]
    fn reads_no_key_of_a_list_in_any_other_form() {
        for script in [
            String::new(),
            list(&[]).replace("RELEASE_KEYS=(\n", "RELEASE_KEYS=( \n"),
            format!("{}RELEASE_KEYS+=(x)\n", list(&[KEY])),
            format!("{}{}", list(&[KEY]), list(&[KEY])),
            list(&[KEY]).replace(&format!("{END_KEY}'"), &format!("  {END_KEY}'")),
            list(&[&KEY.replace("11qY", "11q#")]),
            list(&[&KEY.replace("Ro=", "Rp=")]),
            list(&[&KEY[..30], &KEY[30..]]),
            list(&[KEY]).replace("\n)\n", "\n"),
            list(&[KEY]).replace("  '", "  # '"),
        ] {
            assert!(listed(&script).is_empty(), "{script}");
        }
    }
}
