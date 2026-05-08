"""Tests for LLM message serialization helpers."""

from __future__ import annotations

from types import SimpleNamespace

from surogates.harness.message_utils import (
    message_to_dict,
    reconstruct_message_from_deltas,
)


class TestToolCallArgumentJson:
    def test_reconstruct_replaces_malformed_tool_call_arguments(self) -> None:
        message = reconstruct_message_from_deltas(
            role="assistant",
            content_parts=[],
            tool_calls_acc={
                0: {
                    "id": "tc-bad",
                    "type": "function",
                    "function": {
                        "name": "terminal",
                        "arguments": '{"cmd": ',
                    },
                },
            },
        )

        assert message["tool_calls"][0]["function"]["arguments"] == "{}"

    def test_message_to_dict_replaces_malformed_tool_call_arguments(self) -> None:
        message = SimpleNamespace(
            role="assistant",
            content=None,
            tool_calls=[
                {
                    "id": "tc-bad",
                    "type": "function",
                    "function": {
                        "name": "terminal",
                        "arguments": "{bad json}",
                    },
                },
            ],
        )

        result = message_to_dict(message)

        assert result["tool_calls"][0]["function"]["arguments"] == "{}"

    def test_valid_tool_call_arguments_are_preserved(self) -> None:
        message = reconstruct_message_from_deltas(
            role="assistant",
            content_parts=[],
            tool_calls_acc={
                0: {
                    "id": "tc-good",
                    "type": "function",
                    "function": {
                        "name": "terminal",
                        "arguments": '{"cmd": "ls"}',
                    },
                },
            },
        )

        assert message["tool_calls"][0]["function"]["arguments"] == '{"cmd": "ls"}'
