import json

from surogates.tools.builtin import channel_files as tool_mod
from surogates.tools.router import TOOL_LOCATIONS, ToolLocation


async def test_tool_delegates_to_api_client():
    seen = {}

    class _Api:
        async def fetch_channel_file(self, file_id):
            seen["file_id"] = file_id
            return json.dumps({"success": True, "path": "p"})

    out = json.loads(
        await tool_mod._fetch_channel_file_handler(
            {"file_id": "F1"}, api_client=_Api()),
    )
    assert seen["file_id"] == "F1"
    assert out["success"] is True


async def test_tool_empty_id_errors_without_api_client():
    class _Api:
        async def fetch_channel_file(self, file_id):
            raise AssertionError("must not be called for empty id")

    out = json.loads(
        await tool_mod._fetch_channel_file_handler(
            {"file_id": ""}, api_client=_Api()),
    )
    assert out["success"] is False
    assert "file_id" in out["error"].lower()


async def test_tool_requires_api_client():
    out = json.loads(
        await tool_mod._fetch_channel_file_handler({"file_id": "F1"}),
    )
    assert out["success"] is False


def test_tool_is_harness_routed():
    assert TOOL_LOCATIONS["fetch_channel_file"] == ToolLocation.HARNESS
