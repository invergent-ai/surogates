// The Hyper-V socket services of a Linux guest's vsock ports. A Linux guest's AF_VSOCK port P is the host's service
// whose GUID is P in its first field and the rest of the vsock template, 00000000-facb-11e6-bd58-64006a7986d3
// (Microsoft Learn, "Make your own integration services", "Register a new application", for a Linux guest: "the
// service GUID ... the port, followed by -facb-11e6-bd58-64006a7986d3"; WSL's hvsocket.cpp,
// InitializeSocketAddress, HV_GUID_VSOCK_TEMPLATE with Data1 the port).

/// The vsock template's last three fields: Data2, Data3 and Data4.
pub const TEMPLATE: (u16, u16, [u8; 8]) = (0xfacb, 0x11e6, [0xbd, 0x58, 0x64, 0x00, 0x6a, 0x79, 0x86, 0xd3]);

/// The service GUID of the guest's vsock *port*, in its string form.
pub fn service_id(port: u32) -> String {
    let (data2, data3, data4) = TEMPLATE;
    format!(
        "{port:08x}-{data2:04x}-{data3:04x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
        data4[0], data4[1], data4[2], data4[3], data4[4], data4[5], data4[6], data4[7]
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_each_port_s_service_by_the_vsock_template() {
        // The agent's control, net and inbound ports, and the Plan 9 server's (guest/protocol.ts, VSOCK_PORTS, PLAN9_PORT).
        assert_eq!(service_id(5000), "00001388-facb-11e6-bd58-64006a7986d3");
        assert_eq!(service_id(5001), "00001389-facb-11e6-bd58-64006a7986d3");
        assert_eq!(service_id(5002), "0000138a-facb-11e6-bd58-64006a7986d3");
        assert_eq!(service_id(564), "00000234-facb-11e6-bd58-64006a7986d3");
    }
}
