"""RFB ClientMessage parsing for live-view WebSocket proxying.

websockify relays the VNC TCP byte stream as WebSocket binary frames whose
boundaries do not align with RFB protocol messages — one WS frame may carry a
partial message, or several coalesced messages. ``RFBClientMessageGate`` buffers
the client->server byte stream, splits it into complete RFB ClientMessages, and
drops only the input messages (KeyEvent/PointerEvent/ClientCutText) once the
caller no longer holds browser control. The initial client-side handshake bytes
(ProtocolVersion + selected security type + ClientInit) are not ClientMessages
and pass through untouched.
"""

from __future__ import annotations

RFB_INPUT_TYPES: frozenset[int] = frozenset({4, 5, 6})

# Client-side handshake before any ClientMessage, for x11vnc's no-auth path:
# ProtocolVersion(12) + selected security type(1) + ClientInit(1).
_HANDSHAKE_CLIENT_BYTES = 14


def _client_message_len(buffer: bytes) -> int | None:
    """Length in bytes of the leading RFB ClientMessage, or None if more bytes
    are needed to determine it."""
    if not buffer:
        return None
    message_type = buffer[0]
    if message_type == 0:  # SetPixelFormat
        return 20
    if message_type == 2:  # SetEncodings
        if len(buffer) < 4:
            return None
        return 4 + int.from_bytes(buffer[2:4], "big") * 4
    if message_type == 3:  # FramebufferUpdateRequest
        return 10
    if message_type == 4:  # KeyEvent
        return 8
    if message_type == 5:  # PointerEvent
        return 6
    if message_type == 6:  # ClientCutText
        if len(buffer) < 8:
            return None
        return 8 + int.from_bytes(buffer[4:8], "big")
    # Unknown/extension message: forward one byte at a time so we never stall
    # the stream on a message type we do not model.
    return 1


class RFBClientMessageGate:
    """Stateful, per-connection gate over the client->server RFB byte stream."""

    def __init__(self) -> None:
        self._handshake_remaining = _HANDSHAKE_CLIENT_BYTES
        self._buffer = bytearray()

    def filter_client_bytes(
        self,
        data: bytes,
        *,
        input_allowed: bool,
    ) -> list[bytes]:
        """Return the byte chunks that may be forwarded upstream, dropping input
        ClientMessages when ``input_allowed`` is False."""
        if not data:
            return []
        out: list[bytes] = []
        view = memoryview(data)
        if self._handshake_remaining:
            n = min(self._handshake_remaining, len(view))
            out.append(bytes(view[:n]))
            self._handshake_remaining -= n
            view = view[n:]
            if not view:
                return out

        self._buffer.extend(view)
        while self._buffer:
            length = _client_message_len(bytes(self._buffer))
            if length is None or len(self._buffer) < length:
                break
            message = bytes(self._buffer[:length])
            del self._buffer[:length]
            if message[0] in RFB_INPUT_TYPES and not input_allowed:
                continue
            out.append(message)
        return out


def is_input_frame(frame: bytes) -> bool:
    """Whether a single RFB ClientMessage frame is an input message.

    Retained for the focused ``_should_forward_client_frame`` helper and its
    unit tests; the production WS proxy uses ``RFBClientMessageGate`` so it is
    correct across WebSocket frame boundaries.
    """
    return bool(frame) and frame[0] in RFB_INPUT_TYPES
