// Lines out of a byte stream, each at most MAX_LINE bytes: a line that grows past it is refused before it is whole,
// so no caller makes the service hold more than MAX_LINE of its bytes, and the connection ends there.

/// The longest line either end takes, its newline not counted.
pub const MAX_LINE: usize = 64 * 1024;

/// What the next line is.
#[derive(Debug, PartialEq, Eq)]
pub enum Read {
    /// A whole line, its newline (and a carriage return before it) taken off.
    Line(String),
    /// A line past MAX_LINE bytes, or one that is not UTF-8: nothing more is read of this stream.
    Refused(&'static str),
}

/// The lines of a stream, as its bytes come.
#[derive(Default)]
pub struct Lines {
    held: Vec<u8>,
    refused: bool,
}

impl Lines {
    pub fn new() -> Lines {
        Lines::default()
    }

    /// *bytes*, the stream's next.
    pub fn push(&mut self, bytes: &[u8]) {
        if !self.refused {
            self.held.extend_from_slice(bytes);
        }
    }

    /// The next whole line, or why the stream is refused, or None until more bytes come.
    pub fn next_line(&mut self) -> Option<Read> {
        if self.refused {
            return None;
        }
        match self.held.iter().position(|&byte| byte == b'\n') {
            Some(end) if end <= MAX_LINE + 1 => {
                let mut line: Vec<u8> = self.held.drain(..=end).collect();
                line.pop();
                if line.last() == Some(&b'\r') {
                    line.pop();
                }
                if line.len() > MAX_LINE {
                    return Some(self.refuse("a line over 64 KiB"));
                }
                match String::from_utf8(line) {
                    Ok(line) => Some(Read::Line(line)),
                    Err(_) => Some(self.refuse("a line that is not UTF-8")),
                }
            }
            Some(_) => Some(self.refuse("a line over 64 KiB")),
            None if self.held.len() > MAX_LINE + 1 => Some(self.refuse("a line over 64 KiB")),
            None => None,
        }
    }

    fn refuse(&mut self, why: &'static str) -> Read {
        self.refused = true;
        self.held = Vec::new();
        Read::Refused(why)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gives_each_whole_line_as_it_comes() {
        let mut lines = Lines::new();
        lines.push(b"{\"a\":1}\n{\"b\"");
        assert_eq!(lines.next_line(), Some(Read::Line("{\"a\":1}".into())));
        assert_eq!(lines.next_line(), None);
        lines.push(b":2}\r\n");
        assert_eq!(lines.next_line(), Some(Read::Line("{\"b\":2}".into())));
        assert_eq!(lines.next_line(), None);
    }

    #[test]
    fn takes_a_line_of_64_kib_and_refuses_one_byte_more_before_its_end_comes() {
        let mut lines = Lines::new();
        lines.push(&vec![b'x'; MAX_LINE]);
        lines.push(b"\n");
        assert!(matches!(lines.next_line(), Some(Read::Line(line)) if line.len() == MAX_LINE));
        let mut long = Lines::new();
        long.push(&vec![b'x'; MAX_LINE + 2]);
        assert_eq!(long.next_line(), Some(Read::Refused("a line over 64 KiB")));
        // Nothing more is read or held once refused.
        long.push(b"\n{}\n");
        assert_eq!(long.next_line(), None);
        let mut ended = Lines::new();
        ended.push(&vec![b'x'; MAX_LINE + 1]);
        ended.push(b"\n");
        assert_eq!(ended.next_line(), Some(Read::Refused("a line over 64 KiB")));
    }

    #[test]
    fn refuses_a_line_that_is_not_utf8() {
        let mut lines = Lines::new();
        lines.push(b"\xff\xfe\n");
        assert_eq!(lines.next_line(), Some(Read::Refused("a line that is not UTF-8")));
    }
}
