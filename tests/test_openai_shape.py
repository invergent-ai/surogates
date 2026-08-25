"""The OpenAI wire translation, pinned as pure functions."""

from __future__ import annotations

import json

import pytest

from surogates.channels.openai_shape import (
    CHUNK_OBJECT,
    COMPLETION_OBJECT,
    DONE_SENTINEL,
    OpenAIRequestError,
    Turn,
    build_chat_response,
    build_chunk,
    build_error_body,
    build_final_chunk,
    build_models_response,
    build_role_chunk,
    build_usage_chunk,
    normalise_text,
    parse_chat_request,
    seed_text_for,
    sse_data,
    usage_from_cost_summary,
)


def _user(text):
    return {"role": "user", "content": text}


# ---------------------------------------------------------------------------
# parsing — the basics
# ---------------------------------------------------------------------------

def test_a_single_user_message_becomes_the_prompt():
    parsed = parse_chat_request({"messages": [_user("hello")]})
    assert parsed.prompt == "hello"
    assert parsed.prior_turns == []
    assert parsed.stream is False


def test_history_is_split_from_the_turn_to_run():
    parsed = parse_chat_request({"messages": [
        _user("q1"),
        {"role": "assistant", "content": "a1"},
        _user("q2"),
    ]})
    assert parsed.prompt == "q2"
    assert parsed.prior_turns == [Turn("user", "q1"), Turn("assistant", "a1")]


def test_the_last_message_must_be_from_the_user():
    with pytest.raises(OpenAIRequestError) as exc:
        parse_chat_request({"messages": [
            _user("q"), {"role": "assistant", "content": "a"},
        ]})
    assert "last message must be a user message" in exc.value.message


def test_empty_or_malformed_messages_are_refused():
    for body in ({"messages": []}, {"messages": "nope"}, {}):
        with pytest.raises(OpenAIRequestError):
            parse_chat_request(body)


def test_a_system_message_is_folded_onto_the_first_user_turn():
    """The agent's own prompt is its identity; a caller prefixes, never replaces."""
    parsed = parse_chat_request({"messages": [
        {"role": "system", "content": "Be terse."},
        _user("q1"),
        {"role": "assistant", "content": "a1"},
        _user("q2"),
    ]})
    assert parsed.prior_turns[0] == Turn("user", "Be terse.\n\nq1")
    assert parsed.prompt == "q2", "the instruction must not land on the live turn"


def test_a_system_message_with_a_single_turn_prefixes_the_prompt():
    parsed = parse_chat_request({"messages": [
        {"role": "system", "content": "Be terse."},
        _user("q"),
    ]})
    assert parsed.prompt == "Be terse.\n\nq"


def test_tool_role_messages_are_dropped_whole():
    """The endpoint never emits tool calls, so it can never legitimately
    receive a tool result — and reading its content first would let an
    unsupported part refuse a request that was going to discard it."""
    parsed = parse_chat_request({"messages": [
        _user("q"),
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": [{"type": "weird"}], "tool_call_id": "1"},
        _user("q2"),
    ]})
    assert parsed.prompt == "q2"
    assert [t.role for t in parsed.prior_turns] == ["user", "assistant"]
    assert parsed.prior_turns[1].content == ""


def test_null_content_is_refused_for_user_and_system():
    with pytest.raises(OpenAIRequestError):
        parse_chat_request({"messages": [{"role": "user", "content": None}]})


# ---------------------------------------------------------------------------
# parsing — client-declared tools
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["tools", "functions"])
def test_client_declared_tools_are_refused_not_ignored(key):
    """Ignoring them would look like a hang: the client waits forever for a
    tool call the agent is never going to make."""
    with pytest.raises(OpenAIRequestError) as exc:
        parse_chat_request({"messages": [_user("q")], key: [{"type": "function"}]})
    assert exc.value.code == "tools_not_supported"
    assert exc.value.status == 400


def test_an_empty_tools_array_is_not_a_refusal():
    parse_chat_request({"messages": [_user("q")], "tools": []})


# ---------------------------------------------------------------------------
# parsing — multimodal
# ---------------------------------------------------------------------------

def test_a_data_url_image_is_carried_inline():
    parsed = parse_chat_request({"messages": [{
        "role": "user",
        "content": [
            {"type": "text", "text": "what is this?"},
            {"type": "image_url",
             "image_url": {"url": "data:image/jpeg;base64,QUJD"}},
        ],
    }]})
    assert parsed.prompt == "what is this?"
    assert len(parsed.images) == 1
    assert parsed.images[0].mime_type == "image/jpeg"
    assert parsed.images[0].data == "QUJD"
    assert parsed.images[0].url is None


def test_a_remote_image_is_left_for_the_caller_to_fetch():
    parsed = parse_chat_request({"messages": [{
        "role": "user",
        "content": [{"type": "image_url",
                     "image_url": {"url": "https://example.test/a.png"}}],
    }]})
    assert parsed.images[0].url == "https://example.test/a.png"
    assert parsed.images[0].data is None


def test_an_unsupported_image_type_is_refused():
    with pytest.raises(OpenAIRequestError) as exc:
        parse_chat_request({"messages": [{
            "role": "user",
            "content": [{"type": "image_url",
                         "image_url": {"url": "data:image/tiff;base64,QUJD"}}],
        }]})
    assert "Unsupported image type" in exc.value.message


def test_a_non_http_image_url_is_refused():
    for url in ("ftp://x/y.png", "file:///etc/passwd", "//evil/x.png"):
        with pytest.raises(OpenAIRequestError):
            parse_chat_request({"messages": [{
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": url}}],
            }]})


def test_an_unsupported_content_part_is_refused():
    with pytest.raises(OpenAIRequestError) as exc:
        parse_chat_request({"messages": [{
            "role": "user",
            "content": [{"type": "input_audio", "input_audio": {}}],
        }]})
    assert "input_audio" in exc.value.message


def test_images_are_taken_from_the_turn_being_run_only():
    parsed = parse_chat_request({"messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "old"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
        ]},
        {"role": "assistant", "content": "ok"},
        _user("new"),
    ]})
    assert parsed.images == []
    assert parsed.prompt == "new"


def test_seed_text_marks_images_it_cannot_replay():
    turn = Turn("user", "look at this")
    assert seed_text_for(turn) == "look at this"

    marked = seed_text_for(turn, 1)
    assert "look at this" in marked
    assert "omitted from replayed history: 1" in marked
    assert "omitted from replayed history: 2" in seed_text_for(turn, 2)

    # Machine-strippable, so the reconciler can compare a seeded turn against
    # the caller's own unmarked history instead of re-forking every request.
    from surogates.channels.openai_shape import strip_image_marker

    assert strip_image_marker(marked) == "look at this"
    assert strip_image_marker("no marker here") == "no marker here"


def test_history_image_counts_are_reported_per_prior_turn():
    """Without this the count is unreachable and history images vanish with
    no marker — the exact silent drop seed_text_for exists to prevent."""
    parsed = parse_chat_request({"messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,BB"}},
        ]},
        {"role": "assistant", "content": "ok"},
        _user("and now?"),
    ]})
    assert parsed.prior_image_counts == [2, 0]
    assert len(parsed.prior_image_counts) == len(parsed.prior_turns)
    assert seed_text_for(parsed.prior_turns[0], parsed.prior_image_counts[0]) != (
        parsed.prior_turns[0].content
    )


# ---------------------------------------------------------------------------
# parsing — passthrough flags
# ---------------------------------------------------------------------------

def test_stream_and_include_usage_are_read():
    parsed = parse_chat_request({
        "messages": [_user("q")],
        "stream": True,
        "stream_options": {"include_usage": True},
        "model": "my-agent",
        "user": "customer-42",
    })
    assert parsed.stream is True
    assert parsed.include_usage is True
    assert parsed.model == "my-agent"
    assert parsed.user == "customer-42"


def test_sampling_parameters_are_accepted_and_ignored():
    """An agent resolves its own model and settings; honouring temperature
    would serve something other than the configured agent."""
    parsed = parse_chat_request({
        "messages": [_user("q")],
        "temperature": 1.9, "top_p": 0.1, "max_tokens": 7, "seed": 3,
    })
    assert parsed.prompt == "q"


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------

def test_usage_maps_from_a_cost_summary():
    usage = usage_from_cost_summary({
        "total_input_tokens": 2700,
        "total_output_tokens": 520,
        "total_cache_read_tokens": 1900,
        "total_reasoning_tokens": 240,
        "total_cost_usd": 0.009,
        "call_count": 2,
    })
    assert usage["prompt_tokens"] == 2700
    assert usage["completion_tokens"] == 520
    assert usage["total_tokens"] == 3220
    assert usage["prompt_tokens_details"]["cached_tokens"] == 1900
    assert usage["completion_tokens_details"]["reasoning_tokens"] == 240


def test_usage_is_zero_rather_than_fabricated_when_nothing_was_reported():
    for summary in (None, {}, "nonsense"):
        assert usage_from_cost_summary(summary) == {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        }


def test_usage_omits_detail_blocks_that_would_be_all_zero():
    usage = usage_from_cost_summary({
        "total_input_tokens": 10, "total_output_tokens": 5,
    })
    assert "prompt_tokens_details" not in usage
    assert "completion_tokens_details" not in usage


# ---------------------------------------------------------------------------
# responses
# ---------------------------------------------------------------------------

def test_a_chat_response_has_the_shape_an_sdk_parses():
    payload = build_chat_response(
        completion_id="chatcmpl-x", model="m", content="hi", created=1,
        usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    )
    assert payload["object"] == COMPLETION_OBJECT
    assert payload["choices"][0]["message"] == {"role": "assistant", "content": "hi"}
    assert payload["choices"][0]["finish_reason"] == "stop"
    assert payload["usage"]["total_tokens"] == 3


def test_reasoning_rides_along_when_present():
    payload = build_chat_response(
        completion_id="c", model="m", content="answer", created=1,
        reasoning="because",
    )
    assert payload["choices"][0]["message"]["reasoning_content"] == "because"


def test_models_response_advertises_one_model():
    payload = build_models_response(model="my-agent", created=7)
    assert payload["object"] == "list"
    assert payload["data"][0]["id"] == "my-agent"
    assert payload["data"][0]["object"] == "model"


def test_the_error_envelope_is_the_one_sdks_read():
    """FastAPI's {"detail": ...} reaches the developer as an empty string."""
    body = build_error_body(OpenAIRequestError(
        "nope", code="tools_not_supported", param="tools",
    ))
    assert body["error"]["message"] == "nope"
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["code"] == "tools_not_supported"
    assert body["error"]["param"] == "tools"


# ---------------------------------------------------------------------------
# streaming frames
# ---------------------------------------------------------------------------

def test_the_streaming_frames_form_a_valid_sequence():
    kw = {"completion_id": "chatcmpl-x", "model": "m", "created": 1}
    frames = [
        build_role_chunk(**kw),
        build_chunk(**kw, reasoning="thinking"),
        build_chunk(**kw, content="Hel"),
        build_chunk(**kw, content="lo"),
        build_final_chunk(**kw),
    ]
    assert all(f["object"] == CHUNK_OBJECT for f in frames)
    assert frames[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert frames[1]["choices"][0]["delta"] == {"reasoning_content": "thinking"}
    assert "".join(
        f["choices"][0]["delta"].get("content", "") for f in frames
    ) == "Hello"
    assert frames[-1]["choices"][0]["finish_reason"] == "stop"
    assert all(f["choices"][0]["finish_reason"] is None for f in frames[:-1])


def test_content_and_reasoning_never_share_a_frame():
    """The harness emits them as separate events; merging them would make a
    client that renders reasoning separately interleave the two."""
    kw = {"completion_id": "c", "model": "m", "created": 1}
    assert build_chunk(**kw, content="a")["choices"][0]["delta"] == {"content": "a"}
    assert build_chunk(**kw, reasoning="r")["choices"][0]["delta"] == {
        "reasoning_content": "r",
    }


def test_the_usage_frame_carries_no_choices():
    frame = build_usage_chunk(
        completion_id="c", model="m", created=1,
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    )
    assert frame["choices"] == []
    assert frame["usage"]["total_tokens"] == 2


def test_finish_reason_is_selectable():
    frame = build_final_chunk(
        completion_id="c", model="m", created=1, finish_reason="length",
    )
    assert frame["choices"][0]["finish_reason"] == "length"


def test_sse_lines_are_well_formed():
    line = sse_data({"a": 1})
    assert line.startswith("data: ")
    assert line.endswith("\n\n")
    assert json.loads(line[6:].strip()) == {"a": 1}
    assert sse_data(DONE_SENTINEL) == "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

def test_normalise_text_collapses_what_clients_re_render():
    for variant in ("Bucharest.", "Bucharest. ", "Bucharest.\n", "Bucharest.\r\n"):
        assert normalise_text(variant) == "Bucharest."
    assert normalise_text("a   b\n\tc") == "a b c"
    assert normalise_text(None) == ""


def test_the_image_mime_allowlist_matches_the_message_route():
    """Two allowlists that drift means an image this module accepts is
    rejected one layer down, as a 422 the caller cannot act on."""
    from surogates.api.routes.sessions import _ALLOWED_IMAGE_MIMES as route_mimes
    from surogates.channels.openai_shape import _ALLOWED_IMAGE_MIMES as ours

    assert set(ours) == set(route_mimes)


def test_the_key_turns_are_free_of_the_folded_system_prompt():
    """Clients inject a system prompt carrying the current date and time.

    Folding that into the first user turn and then keying on the result
    changes the key every request, so the conversation never resolves and
    each turn starts a fresh session.
    """
    def parse(system: str):
        return parse_chat_request({"messages": [
            {"role": "system", "content": system},
            _user("q1"),
            {"role": "assistant", "content": "a1"},
            _user("q2"),
        ]})

    monday = parse("You are helpful. Today is Monday 09:00.")
    tuesday = parse("You are helpful. Today is Tuesday 17:42.")

    assert monday.key_turns == ["q1", "q2"]
    assert monday.key_turns == tuesday.key_turns, (
        "a changing system prompt must not reach the conversation key"
    )
    # ...while the agent still receives the instruction.
    assert monday.prior_turns[0].content.startswith("You are helpful.")


def test_key_turns_track_the_user_turns_in_order():
    parsed = parse_chat_request({"messages": [
        _user("one"),
        {"role": "assistant", "content": "x"},
        _user("two"),
        {"role": "assistant", "content": "y"},
        _user("three"),
    ]})
    assert parsed.key_turns == ["one", "two", "three"]
    assert parsed.key_turns[-1] == parsed.prompt


def test_an_image_only_turn_keys_on_its_images_not_on_empty_text():
    """Two unrelated image-only conversations must not share a key.

    An image-only message flattens to empty text, so a key derived from it
    would be identical for every such conversation in a scope and the second
    one's follow-up would resolve into the first one's session.
    """
    def only(url):
        return parse_chat_request({"messages": [{
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": url}}],
        }]})

    a = only("data:image/png;base64,AAAA")
    b = only("data:image/png;base64,BBBB")
    again = only("data:image/png;base64,AAAA")

    assert a.key_turns != [""], "an image-only turn keyed on empty text"
    assert a.key_turns != b.key_turns, "different images must key apart"
    assert a.key_turns == again.key_turns, "the same image must key the same"


def test_a_remote_image_only_turn_keys_on_its_url():
    def only(url):
        return parse_chat_request({"messages": [{
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": url}}],
        }]})

    assert only("https://a.test/x.png").key_turns != only(
        "https://b.test/y.png",
    ).key_turns


def test_text_turns_are_unaffected_by_the_image_fingerprint():
    parsed = parse_chat_request({"messages": [
        _user("one"), {"role": "assistant", "content": "x"}, _user("two"),
    ]})
    assert parsed.key_turns == ["one", "two"]
