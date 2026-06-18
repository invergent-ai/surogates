"""Tests for RFB ClientMessage gating."""

from __future__ import annotations

from surogates.browser.rfb import RFBClientMessageGate


class TestRFBClientMessageGate:
    def test_gate_forwards_split_pointer_event_only_when_complete(self) -> None:
        gate = RFBClientMessageGate()
        # x11vnc no-auth client-side handshake: ProtocolVersion(12) + selected
        # security type(1) + ClientInit(1). Not RFB ClientMessages — pass through.
        assert gate.filter_client_bytes(
            b"RFB 003.008\n\x01\x01", input_allowed=True
        ) == [b"RFB 003.008\n\x01\x01"]

        # A PointerEvent split across two WS frames: nothing until it is complete.
        assert gate.filter_client_bytes(b"\x05\x00", input_allowed=True) == []
        assert gate.filter_client_bytes(b"\x00\x0a\x00\x0b", input_allowed=True) == [
            b"\x05\x00\x00\x0a\x00\x0b",
        ]

    def test_gate_drops_input_after_control_expires_but_keeps_framebuffer_requests(
        self,
    ) -> None:
        gate = RFBClientMessageGate()
        assert gate.filter_client_bytes(b"RFB 003.008\n\x01\x01", input_allowed=True)

        update_request = b"\x03\x00\x00\x00\x00\x00\x10\x00\x10\x00"
        pointer_event = b"\x05\x01\x00\x0a\x00\x0b"
        assert gate.filter_client_bytes(
            update_request + pointer_event, input_allowed=False
        ) == [update_request]

    def test_gate_handles_coalesced_key_and_cut_text_input(self) -> None:
        gate = RFBClientMessageGate()
        assert gate.filter_client_bytes(b"RFB 003.008\n\x01\x01", input_allowed=True)

        key_event = b"\x04\x01\x00\x00\x00\x00\xff\r"
        cut_text = b"\x06\x00\x00\x00\x00\x00\x00\x05hello"
        assert gate.filter_client_bytes(
            key_event + cut_text, input_allowed=False
        ) == []
