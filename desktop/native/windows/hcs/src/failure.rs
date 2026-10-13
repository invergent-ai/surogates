// What a failed call of the Host Compute Service said: its HRESULT, and the words of the ResultError document its
// operation may give (SchemaReference, ResultError (2.1): Error, ErrorMessage; ErrorEvents (2.4), each an
// ErrorEvent's Message). Kept as HCS says it, so a person, and the measurements, read HCS's own words.

use serde::Deserialize;

/// HCS_E_HYPERV_NOT_INSTALLED: "The virtual machine could not be started because a required feature is not
/// installed" (Microsoft Learn, HCS's HRESULT values): no hardware virtualization, or a VM without nested.
pub const HYPERV_NOT_INSTALLED: i32 = 0x8037_0102_u32 as i32;
/// HCS_E_ACCESS_DENIED: "Only administrators or users that are members of the Hyper-V Administrators user group
/// are permitted to access virtual machines or containers."
pub const ACCESS_DENIED: i32 = 0x8037_011B_u32 as i32;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Failure {
    pub code: i32,
    pub words: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "PascalCase")]
struct ResultError {
    error_message: Option<String>,
    #[serde(default)]
    error_events: Vec<ErrorEvent>,
}

#[derive(Deserialize)]
#[serde(rename_all = "PascalCase")]
struct ErrorEvent {
    message: Option<String>,
}

// The most of HCS's words kept: a document is HCS's, but its events can be long.
const MOST: usize = 2000;

impl Failure {
    /// What the call *what* failed with: *code*, and the words of *document*, the operation's result, where it has any.
    pub fn of(what: &str, code: i32, document: Option<&str>) -> Failure {
        let mut said: Vec<String> = Vec::new();
        if let Some(error) = document.and_then(|text| serde_json::from_str::<ResultError>(text).ok()) {
            said.extend(error.error_message.filter(|message| !message.trim().is_empty()).map(|message| message.trim().to_owned()));
            for event in error.error_events {
                if let Some(message) = event.message.map(|message| message.trim().to_owned()).filter(|message| !message.is_empty() && !said.contains(message)) {
                    said.push(message);
                }
            }
        }
        let mut words = format!("{what} failed (HRESULT 0x{:08X})", code as u32);
        if !said.is_empty() {
            words.push_str(": ");
            words.push_str(&said.join(" "));
        }
        if words.len() > MOST {
            let mut end = MOST;
            while !words.is_char_boundary(end) {
                end -= 1;
            }
            words.truncate(end);
        }
        Failure { code, words }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keeps_hcs_s_own_words_with_its_code() {
        let document = r#"{"Error":-2143878910,"ErrorMessage":"The virtual machine could not be started because a required feature is not installed.","ErrorEvents":[{"Message":"Failed to start the virtual machine 'x' because one of the Hyper-V components is not running."},{"Message":"The virtual machine could not be started because a required feature is not installed."}]}"#;
        let failure = Failure::of("HcsStartComputeSystem", HYPERV_NOT_INSTALLED, Some(document));
        assert_eq!(failure.code, HYPERV_NOT_INSTALLED);
        assert_eq!(
            failure.words,
            "HcsStartComputeSystem failed (HRESULT 0x80370102): The virtual machine could not be started because a required feature is not installed. Failed to start the virtual machine 'x' because one of the Hyper-V components is not running."
        );
    }

    #[test]
    fn says_the_code_alone_without_a_document_or_with_one_that_is_not_hcs_s() {
        assert_eq!(Failure::of("HcsCreateComputeSystem", ACCESS_DENIED, None).words, "HcsCreateComputeSystem failed (HRESULT 0x8037011B)");
        assert_eq!(Failure::of("HcsCreateComputeSystem", -1, Some("not json")).words, "HcsCreateComputeSystem failed (HRESULT 0xFFFFFFFF)");
    }

    #[test]
    fn bounds_what_it_keeps() {
        let document = format!(r#"{{"ErrorMessage":"{}"}}"#, "é".repeat(5000));
        assert!(Failure::of("x", -1, Some(&document)).words.len() <= 2000);
    }
}
