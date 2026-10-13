//! The JSON the helper prints: a string as RFC 8259 writes one, every control character escaped, and nothing else
//! that the app's JSON.parse would read otherwise.

/// *text* as a JSON string, quotes and all.
pub fn string(text: &str) -> String {
    let mut out = String::with_capacity(text.len() + 2);
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

#[cfg(test)]
mod tests {
    use super::string;

    #[test]
    fn writes_a_string_with_its_quotes_backslashes_and_control_characters_escaped() {
        assert_eq!(string(""), "\"\"");
        assert_eq!(string("C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"), "\"C:\\\\Program Files\\\\Google\\\\Chrome\\\\Application\\\\chrome.exe\"");
        assert_eq!(string("say \"hi\""), "\"say \\\"hi\\\"\"");
        assert_eq!(string("a\nb\rc\td\u{0}e\u{1f}f"), "\"a\\nb\\rc\\td\\u0000e\\u001ff\"");
        assert_eq!(string("Ünïcødé ✓ \u{7f}"), "\"Ünïcødé ✓ \u{7f}\"");
    }
}
