"""Tests for the fetched-channel-image vision-block injector (spec-required suite).

Covers the cases listed in the task spec:
- vision + fetch_channel_file image result + working read_image -> one user message with text+image_url
- supports_vision=False -> []
- read_image returns None -> [] (no raise)
- kind=attachment -> []
- tool call name is NOT fetch_channel_file -> []
- malformed content (not JSON) -> [] (no raise)
"""
import base64
import json

from surogates.harness.loop_vision_inject import maybe_build_fetched_image_messages

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20  # minimal PNG-ish bytes


def _tc(name: str, call_id: str) -> dict:
    """Build a raw tool-call dict."""
    return {"id": call_id, "function": {"name": name, "arguments": "{}"}}


def _tr(call_id: str, content) -> dict:
    """Build a tool-result dict; content is JSON-encoded if it's a dict."""
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps(content) if isinstance(content, dict) else content,
    }


async def _read_ok(path: str):
    return PNG_BYTES


async def _read_none(path: str):
    return None


# ---------------------------------------------------------------------------
# 1. Vision model + fetch_channel_file image result -> user message with image_url
# ---------------------------------------------------------------------------
async def test_vision_model_image_result_injects_user_message():
    call_id = "cid_1"
    tool_calls = [_tc("fetch_channel_file", call_id)]
    tool_results = [
        _tr(
            call_id,
            {
                "success": True,
                "kind": "image",
                "path": "uploads/slack/fetch/x.png",
                "filename": "x.png",
                "mime_type": "image/png",
            },
        )
    ]

    msgs = await maybe_build_fetched_image_messages(
        tool_results,
        tool_calls,
        supports_vision=True,
        read_image=_read_ok,
    )

    assert len(msgs) == 1
    msg = msgs[0]
    assert msg["role"] == "user"
    blocks = msg["content"]
    assert isinstance(blocks, list)
    types = [b["type"] for b in blocks]
    assert "text" in types
    assert "image_url" in types
    img_block = next(b for b in blocks if b["type"] == "image_url")
    url = img_block["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    # Verify the embedded bytes decode back to PNG_BYTES.
    b64_part = url.split(",", 1)[1]
    assert base64.b64decode(b64_part) == PNG_BYTES


# ---------------------------------------------------------------------------
# 2. supports_vision=False -> []
# ---------------------------------------------------------------------------
async def test_non_vision_model_returns_empty():
    call_id = "cid_2"
    tool_calls = [_tc("fetch_channel_file", call_id)]
    tool_results = [
        _tr(call_id, {"kind": "image", "path": "x.png", "mime_type": "image/png"})
    ]

    msgs = await maybe_build_fetched_image_messages(
        tool_results,
        tool_calls,
        supports_vision=False,
        read_image=_read_ok,
    )
    assert msgs == []


# ---------------------------------------------------------------------------
# 3. read_image returns None -> [] (no raise)
# ---------------------------------------------------------------------------
async def test_read_image_returns_none_no_raise():
    call_id = "cid_3"
    tool_calls = [_tc("fetch_channel_file", call_id)]
    tool_results = [
        _tr(call_id, {"kind": "image", "path": "x.png", "mime_type": "image/png"})
    ]

    msgs = await maybe_build_fetched_image_messages(
        tool_results,
        tool_calls,
        supports_vision=True,
        read_image=_read_none,
    )
    assert msgs == []


# ---------------------------------------------------------------------------
# 4. kind=attachment -> []
# ---------------------------------------------------------------------------
async def test_non_image_kind_returns_empty():
    call_id = "cid_4"
    tool_calls = [_tc("fetch_channel_file", call_id)]
    tool_results = [
        _tr(
            call_id,
            {
                "kind": "attachment",
                "path": "uploads/slack/fetch/doc.pdf",
                "mime_type": "application/pdf",
                "filename": "doc.pdf",
            },
        )
    ]

    msgs = await maybe_build_fetched_image_messages(
        tool_results,
        tool_calls,
        supports_vision=True,
        read_image=_read_ok,
    )
    assert msgs == []


# ---------------------------------------------------------------------------
# 5. Tool name is NOT fetch_channel_file -> []
# ---------------------------------------------------------------------------
async def test_other_tool_name_returns_empty():
    call_id = "cid_5"
    tool_calls = [_tc("generate_image", call_id)]  # wrong tool name
    tool_results = [
        _tr(call_id, {"kind": "image", "path": "gen/img.png", "mime_type": "image/png"})
    ]

    msgs = await maybe_build_fetched_image_messages(
        tool_results,
        tool_calls,
        supports_vision=True,
        read_image=_read_ok,
    )
    assert msgs == []


# ---------------------------------------------------------------------------
# 6. Malformed content (not JSON) -> [] (no raise)
# ---------------------------------------------------------------------------
async def test_malformed_content_no_raise():
    call_id = "cid_6"
    tool_calls = [_tc("fetch_channel_file", call_id)]
    # Raw string that is not valid JSON
    bad_result = {
        "role": "tool",
        "tool_call_id": call_id,
        "content": "<<< NOT JSON >>>",
    }

    msgs = await maybe_build_fetched_image_messages(
        [bad_result],
        tool_calls,
        supports_vision=True,
        read_image=_read_ok,
    )
    assert msgs == []
