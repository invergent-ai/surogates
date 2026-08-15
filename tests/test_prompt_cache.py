"""Tests for surogates.harness.prompt_cache -- Anthropic prompt caching and system prompt cache."""

from __future__ import annotations

import copy
from uuid import UUID, uuid4

from surogates.harness.prompt_cache import (
    SystemPromptCache,
    _CACHE_BREAKPOINT_COUNT,
    _CACHE_MARKER,
    apply_cache_control,
    build_cache_extra_body,
    is_cacheable_model,
)


# ---------------------------------------------------------------------------
# is_cacheable_model
# ---------------------------------------------------------------------------


class TestIsCacheableModel:
    def test_claude_in_model_id(self) -> None:
        assert is_cacheable_model("claude-sonnet-4-20250514") is True

    def test_claude_case_insensitive(self) -> None:
        assert is_cacheable_model("Claude-Opus-4") is True

    def test_non_claude_model(self) -> None:
        assert is_cacheable_model("gpt-4o") is False

    def test_anthropic_in_base_url(self) -> None:
        assert is_cacheable_model("some-model", base_url="https://api.anthropic.com/v1") is True

    def test_base_url_case_insensitive(self) -> None:
        assert is_cacheable_model("some-model", base_url="https://API.Anthropic.COM") is True

    def test_no_base_url_non_claude(self) -> None:
        assert is_cacheable_model("gpt-4o", base_url=None) is False

    def test_openrouter_claude(self) -> None:
        assert is_cacheable_model("anthropic/claude-sonnet-4", base_url="https://openrouter.ai/api") is True

    def test_non_anthropic_base_url(self) -> None:
        assert is_cacheable_model("some-model", base_url="https://api.openai.com/v1") is False


# ---------------------------------------------------------------------------
# apply_cache_control
# ---------------------------------------------------------------------------


class TestApplyCacheControl:
    def test_marks_last_n_user_assistant_messages(self) -> None:
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "how are you?"},
            {"role": "assistant", "content": "good"},
            {"role": "user", "content": "tell me more"},
        ]
        result_msgs, result_prompt = apply_cache_control(messages, "system prompt")
        # Last 3 user/assistant messages should be marked.
        marked = [m for m in result_msgs if "cache_control" in m]
        assert len(marked) == _CACHE_BREAKPOINT_COUNT

    def test_does_not_mutate_original(self) -> None:
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        original = copy.deepcopy(messages)
        apply_cache_control(messages, "prompt")
        assert messages == original  # originals untouched

    def test_fewer_messages_than_breakpoints(self) -> None:
        messages = [
            {"role": "user", "content": "hello"},
        ]
        result_msgs, _ = apply_cache_control(messages, "prompt")
        marked = [m for m in result_msgs if "cache_control" in m]
        assert len(marked) == 1

    def test_skips_tool_messages(self) -> None:
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "tool", "tool_call_id": "t1", "content": "result"},
            {"role": "user", "content": "next"},
        ]
        result_msgs, _ = apply_cache_control(messages, "prompt")
        # Tool messages should not be marked.
        for m in result_msgs:
            if m.get("role") == "tool":
                assert "cache_control" not in m

    def test_empty_messages(self) -> None:
        result_msgs, result_prompt = apply_cache_control([], "prompt")
        assert result_msgs == []
        assert result_prompt == "prompt"

    def test_cache_marker_value(self) -> None:
        messages = [
            {"role": "user", "content": "hello"},
        ]
        result_msgs, _ = apply_cache_control(messages, "prompt")
        assert result_msgs[0]["cache_control"] == _CACHE_MARKER


# ---------------------------------------------------------------------------
# build_cache_extra_body
# ---------------------------------------------------------------------------


class TestBuildCacheExtraBody:
    def test_returns_dict_for_claude(self) -> None:
        result = build_cache_extra_body("claude-sonnet-4-20250514")
        assert result is not None
        assert "extra_headers" in result
        assert "anthropic-beta" in result["extra_headers"]

    def test_returns_none_for_non_claude(self) -> None:
        result = build_cache_extra_body("gpt-4o")
        assert result is None

    def test_returns_none_for_deepseek(self) -> None:
        result = build_cache_extra_body("deepseek-chat")
        assert result is None

    def test_header_value(self) -> None:
        result = build_cache_extra_body("claude-opus-4-20250514")
        assert result is not None
        assert result["extra_headers"]["anthropic-beta"] == "prompt-caching-2024-07-31"


# ---------------------------------------------------------------------------
# SystemPromptCache
# ---------------------------------------------------------------------------


class TestSystemPromptCache:
    def test_get_returns_none_when_empty(self) -> None:
        cache = SystemPromptCache()
        assert cache.get(uuid4()) is None

    def test_set_and_get(self) -> None:
        cache = SystemPromptCache()
        sid = uuid4()
        cache.set(sid, "You are a test agent.")
        assert cache.get(sid) == "You are a test agent."

    def test_invalidate(self) -> None:
        cache = SystemPromptCache()
        sid = uuid4()
        cache.set(sid, "prompt")
        cache.invalidate(sid)
        assert cache.get(sid) is None

    def test_invalidate_nonexistent_is_noop(self) -> None:
        cache = SystemPromptCache()
        cache.invalidate(uuid4())  # should not raise

    def test_len(self) -> None:
        cache = SystemPromptCache()
        assert len(cache) == 0
        sid1 = uuid4()
        sid2 = uuid4()
        cache.set(sid1, "a")
        assert len(cache) == 1
        cache.set(sid2, "b")
        assert len(cache) == 2

    def test_contains(self) -> None:
        cache = SystemPromptCache()
        sid = uuid4()
        assert sid not in cache
        cache.set(sid, "prompt")
        assert sid in cache

    def test_overwrite(self) -> None:
        cache = SystemPromptCache()
        sid = uuid4()
        cache.set(sid, "old")
        cache.set(sid, "new")
        assert cache.get(sid) == "new"

    def test_multiple_sessions_independent(self) -> None:
        cache = SystemPromptCache()
        sid1 = uuid4()
        sid2 = uuid4()
        cache.set(sid1, "prompt-1")
        cache.set(sid2, "prompt-2")
        assert cache.get(sid1) == "prompt-1"
        assert cache.get(sid2) == "prompt-2"
        cache.invalidate(sid1)
        assert cache.get(sid1) is None
        assert cache.get(sid2) == "prompt-2"


# ---------------------------------------------------------------------------
# Prefix caching on the outgoing payload
# ---------------------------------------------------------------------------


class TestMarkPrefixCacheable:
    """The static prefix (tools + system) is ~85% of every request.

    Measured: 51.6k chars of tool schemas + 33k of system prompt, re-sent
    every turn with cache_read_tokens = 0. A breakpoint on the system block
    caches tools+system, since Anthropic caches in tools -> system ->
    messages order.
    """

    def _payload(self):
        return {
            "model": "surogate",
            "messages": [
                {"role": "system", "content": "S" * 100},
                {"role": "user", "content": "hello"},
            ],
            "tools": [{"type": "function", "function": {"name": "t"}}],
        }

    def test_system_block_gets_a_cache_breakpoint(self):
        from surogates.harness.prompt_cache import mark_prefix_cacheable

        p = self._payload()
        mark_prefix_cacheable(p, "surogate")
        sys_msg = p["messages"][0]
        assert isinstance(sys_msg["content"], list)
        assert sys_msg["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert sys_msg["content"][0]["text"] == "S" * 100

    def test_tier_sentinels_are_cacheable(self):
        # The sentinel is what the harness sends; it resolves to Claude.
        # Gating on "claude" in the model id left caching off for every
        # platform session -- the same sentinel bug as cost attribution.
        from surogates.harness.prompt_cache import is_cacheable_model

        assert is_cacheable_model("surogate")
        assert is_cacheable_model("surogate-pro")
        assert is_cacheable_model("claude-sonnet-5")

    def test_non_anthropic_model_is_left_alone(self):
        from surogates.harness.prompt_cache import mark_prefix_cacheable

        p = self._payload()
        p["model"] = "gpt-5.5"
        mark_prefix_cacheable(p, "gpt-5.5")
        assert p["messages"][0]["content"] == "S" * 100  # untouched str

    def test_is_idempotent(self):
        from surogates.harness.prompt_cache import mark_prefix_cacheable

        p = self._payload()
        mark_prefix_cacheable(p, "surogate")
        mark_prefix_cacheable(p, "surogate")
        assert len(p["messages"][0]["content"]) == 1

    def test_no_system_message_is_a_noop(self):
        from surogates.harness.prompt_cache import mark_prefix_cacheable

        p = {"model": "surogate", "messages": [{"role": "user", "content": "x"}]}
        mark_prefix_cacheable(p, "surogate")  # must not raise
        assert p["messages"][0]["content"] == "x"
