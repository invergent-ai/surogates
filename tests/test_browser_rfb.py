"""Tests for RFB ClientMessage gating."""

from __future__ import annotations

from surogates.browser.rfb import RFB_INPUT_TYPES, is_input_frame


class TestIsInputFrame:
    def test_key_event_is_input(self) -> None:
        assert is_input_frame(bytes([4]) + bytes(7)) is True

    def test_pointer_event_is_input(self) -> None:
        assert is_input_frame(bytes([5]) + bytes(5)) is True

    def test_client_cut_text_is_input(self) -> None:
        assert is_input_frame(bytes([6]) + bytes(7)) is True

    def test_set_pixel_format_is_not_input(self) -> None:
        assert is_input_frame(bytes([0]) + bytes(19)) is False

    def test_set_encodings_is_not_input(self) -> None:
        assert is_input_frame(bytes([2]) + bytes(7)) is False

    def test_framebuffer_update_request_is_not_input(self) -> None:
        assert is_input_frame(bytes([3]) + bytes(9)) is False

    def test_empty_frame_is_not_input(self) -> None:
        assert is_input_frame(b"") is False

    def test_input_types_set(self) -> None:
        assert RFB_INPUT_TYPES == frozenset({4, 5, 6})
