// What the Host Compute Service answers, read: the service's supported schema versions, a system's runtime ID, and
// how a system exited.

use serde_json::Value;

/// Whether the service's properties (HcsGetServiceProperties of {"PropertyTypes":["Basic"]}) name *version* among
/// its SupportedSchemaVersions (BasicInformation, 2.1: "Array element A.X implies all versions with major version A
/// and minor version from 0 to X are also supported"), wherever in the answer the list is.
pub fn supports(answer: &str, version: (u32, u32)) -> bool {
    fn find(value: &Value, version: (u32, u32)) -> bool {
        match value {
            Value::Object(fields) => fields.iter().any(|(key, field)| {
                (key == "SupportedSchemaVersions"
                    && field.as_array().is_some_and(|versions| {
                        versions.iter().any(|each| {
                            each["Major"].as_u64() == Some(u64::from(version.0)) && each["Minor"].as_u64().is_some_and(|minor| minor >= u64::from(version.1))
                        })
                    }))
                    || find(field, version)
            }),
            Value::Array(items) => items.iter().any(|item| find(item, version)),
            _ => false,
        }
    }
    serde_json::from_str::<Value>(answer).is_ok_and(|value| find(&value, version))
}

/// A system's runtime ID from its properties (Properties, RuntimeId, 2.1): the VM's ID that Hyper-V sockets address.
pub fn runtime_id(properties: &str) -> Option<String> {
    let value: Value = serde_json::from_str(properties).ok()?;
    let id = value["RuntimeId"].as_str()?.trim_matches(|c| c == '{' || c == '}').to_ascii_lowercase();
    is_guid(&id).then_some(id)
}

/// Whether *text* is a GUID's string form, 8-4-4-4-12 hex digits.
pub fn is_guid(text: &str) -> bool {
    let parts: Vec<&str> = text.split('-').collect();
    parts.len() == 5 && parts.iter().zip([8, 4, 4, 4, 12]).all(|(part, length)| part.len() == length && part.bytes().all(|byte| byte.is_ascii_hexdigit()))
}

/// How a system exited, from the event's SystemExitStatus (2.1): its Status, an HRESULT, 0 when the guest powered off.
pub fn exit_status(document: Option<&str>) -> i32 {
    document
        .and_then(|text| serde_json::from_str::<Value>(text).ok())
        .and_then(|value| value["Status"].as_i64())
        .map_or(0, |status| status as i32)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_the_schema_version_in_the_service_s_answer() {
        let answer = r#"{"Properties":[{"SupportedSchemaVersions":[{"Major":1,"Minor":0},{"Major":2,"Minor":11}]}]}"#;
        assert!(supports(answer, (2, 6)));
        assert!(supports(answer, (2, 11)));
        assert!(!supports(answer, (2, 12)));
        assert!(!supports(r#"{"SupportedSchemaVersions":[{"Major":2,"Minor":5}]}"#, (2, 6)));
        assert!(!supports("not json", (2, 6)));
    }

    #[test]
    fn reads_a_runtime_id_and_nothing_that_is_not_one() {
        assert_eq!(runtime_id(r#"{"Id":"x","RuntimeId":"6B3C1D3E-6F0A-4C1E-9A52-6A1D2C3B4E5F"}"#).as_deref(), Some("6b3c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f"));
        assert_eq!(runtime_id(r#"{"RuntimeId":"{6b3c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f}"}"#).as_deref(), Some("6b3c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f"));
        assert_eq!(runtime_id(r#"{"RuntimeId":"6b3c1d3e-6f0a-4c1e-9a52"}"#), None);
        assert_eq!(runtime_id("{}"), None);
    }

    #[test]
    fn reads_how_a_system_exited() {
        assert_eq!(exit_status(Some(r#"{"Status":0,"ExitType":"GracefulExit"}"#)), 0);
        assert_eq!(exit_status(Some(r#"{"Status":-2143878906,"ExitType":"UnexpectedExit"}"#)), 0x8037_0106_u32 as i32);
        assert_eq!(exit_status(None), 0);
    }
}
