//! A certificate as PEM (RFC 7468, section 5): its DER in base64 (RFC 4648, section 4), 64 characters a line, between
//! the CERTIFICATE label's lines, each line ending in a line feed.

const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// *bytes* in base64, padded.
pub fn base64(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let [a, b, c] = [chunk[0], *chunk.get(1).unwrap_or(&0), *chunk.get(2).unwrap_or(&0)];
        let n = (u32::from(a) << 16) | (u32::from(b) << 8) | u32::from(c);
        for (index, shift) in [18, 12, 6, 0].into_iter().enumerate() {
            if index <= chunk.len() {
                out.push(char::from(ALPHABET[((n >> shift) & 63) as usize]));
            } else {
                out.push('=');
            }
        }
    }
    out
}

/// The certificate whose DER is *der*, as PEM.
pub fn certificate(der: &[u8]) -> String {
    let encoded = base64(der);
    let mut out = String::from("-----BEGIN CERTIFICATE-----\n");
    for line in encoded.as_bytes().chunks(64) {
        out.push_str(std::str::from_utf8(line).unwrap_or_default());
        out.push('\n');
    }
    out.push_str("-----END CERTIFICATE-----\n");
    out
}

#[cfg(test)]
mod tests {
    use super::{base64, certificate};

    #[test]
    fn encodes_rfc_4648_s_test_vectors() {
        for (plain, encoded) in [("", ""), ("f", "Zg=="), ("fo", "Zm8="), ("foo", "Zm9v"), ("foob", "Zm9vYg=="), ("fooba", "Zm9vYmE="), ("foobar", "Zm9vYmFy")] {
            assert_eq!(base64(plain.as_bytes()), encoded, "{plain}");
        }
        assert_eq!(base64(&[0xfb, 0xff, 0xbf]), "+/+/");
    }

    #[test]
    fn writes_a_certificate_64_characters_a_line_between_its_label_s_lines() {
        let der: Vec<u8> = (0..=255u8).collect();
        let pem = certificate(&der);
        let lines: Vec<&str> = pem.lines().collect();
        assert_eq!(lines.first(), Some(&"-----BEGIN CERTIFICATE-----"));
        assert_eq!(lines.last(), Some(&"-----END CERTIFICATE-----"));
        assert!(lines[1..lines.len() - 1].iter().all(|line| line.len() <= 64));
        assert_eq!(lines[1..lines.len() - 2].iter().map(|line| line.len()).collect::<Vec<_>>(), vec![64; 5]);
        assert_eq!(lines[1..lines.len() - 1].concat(), base64(&der));
        assert!(pem.ends_with("-----END CERTIFICATE-----\n"));
        assert!(!pem.contains('\r'));
    }
}
