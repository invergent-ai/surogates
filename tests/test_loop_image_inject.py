"""Tests for the fetched-channel-image vision-block injector."""
import base64
import json
from types import SimpleNamespace

from surogates.harness.loop_vision_inject import maybe_build_fetched_image_messages

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20  # minimal PNG-ish bytes

def _tc(name: str, call_id: str) -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": "{}"}}

def _tr(call_id: str, content: dict) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": json.dumps(content)}

def _read_ok(path: str):
    """Synchronous stub returning (bytes, mime). maybe_build_... calls it with await."""
    # Return None: the helper must handle None gracefully
    return None

async def test_vision_capable_image_result_injects_user_message():
    """Vision model + fetch_channel_file image result -> one trailing user message with image_url block."""
    call_id = "call_img_1"
    tool_calls = [_tc("fetch_channel_file", call_id)]
    tool_results = [_tr(call_id, {"kind": "image", "path": "uploads/slack/fetch/F1-photo.png", "mime_type": "image/png", "filename": "photo.png"})]

    read_count = []
    async def _read(path):
        read_count.append(path)
        return (PNG_BYTES, "image/png")

    msgs = await maybe_build_fetched_image_messages(
        tool_results, tool_calls, supports_vision=True, read_image=_read,
    )
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg["role"] == "user"
    blocks = msg["content"]
    assert isinstance(blocks, list)
    # must have text block and image_url block
    types = [b["type"] for b in blocks]
    assert "text" in types
    assert "image_url" in types
    img_block = next(b for b in blocks if b["type"] == "image_url")
    url = img_block["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    # storage read was called once
    assert read_count == ["uploads/slack/fetch/F1-photo.png"]


async def test_non_vision_model_returns_empty():
    """Text-only model: no injection, returns []."""
    call_id = "call_img_2"
    tool_calls = [_tc("fetch_channel_file", call_id)]
    tool_results = [_tr(call_id, {"kind": "image", "path": "uploads/slack/fetch/F2-x.png", "mime_type": "image/png", "filename": "x.png"})]

    async def _read(path):
        return (PNG_BYTES, "image/png")

    msgs = await maybe_build_fetched_image_messages(
        tool_results, tool_calls, supports_vision=False, read_image=_read,
    )
    assert msgs == []


async def test_storage_failure_returns_empty_no_raise():
    """Storage read raises: no injection, no exception."""
    call_id = "call_img_3"
    tool_calls = [_tc("fetch_channel_file", call_id)]
    tool_results = [_tr(call_id, {"kind": "image", "path": "uploads/slack/fetch/F3-y.png", "mime_type": "image/png", "filename": "y.png"})]

    async def _read_fail(path):
        raise OSError("storage gone")

    msgs = await maybe_build_fetched_image_messages(
        tool_results, tool_calls, supports_vision=True, read_image=_read_fail,
    )
    assert msgs == []


async def test_non_image_attachment_result_returns_empty():
    """kind=attachment (non-image): no injection."""
    call_id = "call_att_1"
    tool_calls = [_tc("fetch_channel_file", call_id)]
    tool_results = [_tr(call_id, {"kind": "attachment", "path": "uploads/slack/fetch/F4-doc.pdf", "mime_type": "application/pdf", "filename": "doc.pdf"})]

    async def _read(path):
        return (b"PDF", "application/pdf")

    msgs = await maybe_build_fetched_image_messages(
        tool_results, tool_calls, supports_vision=True, read_image=_read,
    )
    assert msgs == []


async def test_other_tool_image_result_ignored():
    """Only fetch_channel_file results are considered; other tools returning image-ish content are ignored."""
    call_id = "call_other_1"
    tool_calls = [_tc("generate_image", call_id)]  # not fetch_channel_file
    tool_results = [_tr(call_id, {"kind": "image", "path": "uploads/gen/img.png", "mime_type": "image/png"})]

    async def _read(path):
        return (PNG_BYTES, "image/png")

    msgs = await maybe_build_fetched_image_messages(
        tool_results, tool_calls, supports_vision=True, read_image=_read,
    )
    assert msgs == []


async def test_read_image_returns_none_returns_empty():
    """read_image returns None (soft failure): no injection."""
    call_id = "call_img_4"
    tool_calls = [_tc("fetch_channel_file", call_id)]
    tool_results = [_tr(call_id, {"kind": "image", "path": "uploads/slack/fetch/F5-z.png", "mime_type": "image/png", "filename": "z.png"})]

    async def _read_none(path):
        return None

    msgs = await maybe_build_fetched_image_messages(
        tool_results, tool_calls, supports_vision=True, read_image=_read_none,
    )
    assert msgs == []
