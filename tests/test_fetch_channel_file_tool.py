import json

from surogates.tools.builtin import channel_files as tool_mod


async def test_tool_delegates_file_name_to_api_client():
    """Passing {"file": "report.html"} forwards the name to api_client."""
    seen = {}

    class _Api:
        async def fetch_channel_file(self, ref):
            seen["ref"] = ref
            return json.dumps({"success": True, "path": "p"})

    out = json.loads(
        await tool_mod._fetch_channel_file_handler(
            {"file": "report.html"}, api_client=_Api()),
    )
    assert seen["ref"] == "report.html"
    assert out["success"] is True


async def test_tool_delegates_file_id_to_api_client():
    """Passing {"file": "F0BE46MG31P"} also forwards correctly."""
    seen = {}

    class _Api:
        async def fetch_channel_file(self, ref):
            seen["ref"] = ref
            return json.dumps({"success": True, "path": "p"})

    out = json.loads(
        await tool_mod._fetch_channel_file_handler(
            {"file": "F0BE46MG31P"}, api_client=_Api()),
    )
    assert seen["ref"] == "F0BE46MG31P"
    assert out["success"] is True


async def test_tool_accepts_legacy_file_id_key():
    """Legacy {"file_id": "F123"} must still be forwarded (no breaking change)."""
    seen = {}

    class _Api:
        async def fetch_channel_file(self, ref):
            seen["ref"] = ref
            return json.dumps({"success": True, "path": "p"})

    out = json.loads(
        await tool_mod._fetch_channel_file_handler(
            {"file_id": "F123"}, api_client=_Api()),
    )
    assert seen["ref"] == "F123"
    assert out["success"] is True


async def test_tool_empty_file_errors():
    class _Api:
        async def fetch_channel_file(self, ref):
            raise AssertionError("must not be called for empty ref")

    out = json.loads(
        await tool_mod._fetch_channel_file_handler(
            {"file": ""}, api_client=_Api()),
    )
    assert out["success"] is False


async def test_tool_requires_api_client():
    out = json.loads(
        await tool_mod._fetch_channel_file_handler({"file": "report.html"}),
    )
    assert out["success"] is False
