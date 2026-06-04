"""Tool-call validation and recovery helpers for the harness loop."""

from __future__ import annotations

import json

def _is_valid_json_args(tc: dict) -> bool:
    """Check if a tool call's arguments are valid JSON."""
    import json as _json

    fn = tc.get("function", {})
    args_raw = fn.get("arguments", "")
    if not args_raw or not isinstance(args_raw, str):
        return True  # empty or already parsed — not invalid JSON
    args_raw = args_raw.strip()
    if not args_raw or args_raw == "{}":
        return True
    try:
        parsed = _json.loads(args_raw)
        return isinstance(parsed, dict)
    except (ValueError, TypeError):
        return False


def build_partial_tool_call_recovery_results(tool_calls: list[dict]) -> list[dict]:
    """Build model-visible tool results for truncated tool-call arguments."""
    results: list[dict] = []
    for tc in tool_calls:
        fn = tc.get("function", {})
        tool_name = fn.get("name", "")
        results.append(
            {
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": json.dumps(
                    {
                        "error": (
                            "Partial tool call arguments detected. The provider "
                            "ended the response before the JSON arguments were "
                            "complete. Retry this tool call with complete JSON."
                        ),
                        "tool": tool_name,
                    },
                    ensure_ascii=False,
                ),
            }
        )
    return results
