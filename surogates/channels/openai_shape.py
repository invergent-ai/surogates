"""Translation between the OpenAI chat-completions wire format and agent turns.

Pure: no I/O, no database, no clock, no randomness.  Every rule that decides
what an agent is asked, and how its answer is reported, lives here so it can be
pinned by a test that needs nothing running.

Two things this module deliberately does **not** do:

* It never emits OpenAI ``tool_calls``.  The agent runs its own tools inside a
  turn; a client that received a tool call would try to answer it and the
  request would hang forever waiting for a ``role: "tool"`` message the agent
  is not listening for.
* It never honours the caller's sampling parameters.  An agent resolves its own
  model and generation settings, so applying the caller's ``temperature`` would
  serve something other than the configured agent.  They are accepted and
  ignored, which is what every OpenAI-compatible gateway does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

__all__ = [
    "CHUNK_OBJECT",
    "COMPLETION_OBJECT",
    "DONE_SENTINEL",
    "ImagePart",
    "OpenAIRequestError",
    "ParsedChatRequest",
    "Turn",
    "build_chat_response",
    "build_chunk",
    "build_error_body",
    "build_final_chunk",
    "build_models_response",
    "build_role_chunk",
    "build_usage_chunk",
    "normalise_text",
    "parse_chat_request",
    "sse_data",
    "usage_from_cost_summary",
]

COMPLETION_OBJECT = "chat.completion"
CHUNK_OBJECT = "chat.completion.chunk"
#: The literal an OpenAI stream ends with.  Clients treat its absence as a
#: truncated response, so every terminal path must emit it — including errors
#: raised after the first chunk has already been flushed.
DONE_SENTINEL = "[DONE]"

#: Mirrors ``surogates.api.routes.sessions._ALLOWED_IMAGE_MIMES``.  Duplicated
#: rather than imported because this module must stay free of route imports;
#: the parity is asserted by the test suite.
_ALLOWED_IMAGE_MIMES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)

#: Roles that carry conversation content.  Anything else (``tool``,
#: ``function``, ``developer``) is dropped whole — see :func:`parse_chat_request`.
_CONTENT_ROLES = frozenset({"system", "user", "assistant"})


class OpenAIRequestError(Exception):
    """A request this module refuses, carrying the OpenAI error taxonomy.

    Routes turn this into ``build_error_body`` + ``status``.  Raising a typed
    error rather than a bare ``ValueError`` keeps the ``type``/``code`` a
    client's SDK branches on out of the route layer, where it would drift.
    """

    def __init__(
        self,
        message: str,
        *,
        type: str = "invalid_request_error",
        code: str | None = None,
        status: int = 400,
        param: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.type = type
        self.code = code
        self.status = status
        self.param = param


@dataclass(frozen=True, slots=True)
class ImagePart:
    """One image attached to the turn being run.

    Exactly one of *data* / *url* is set.  ``data`` is inline base64 (or a
    ``data:`` URL) that maps straight onto the message route's ``ImageBlock``;
    ``url`` is a remote reference the caller must fetch, which this module
    cannot do without becoming impure.
    """

    mime_type: str
    data: str | None = None
    url: str | None = None


@dataclass(frozen=True, slots=True)
class Turn:
    """One already-completed exchange in the caller's history."""

    role: str
    content: str


@dataclass(frozen=True, slots=True)
class ParsedChatRequest:
    """A chat completion split into history and the turn to actually run."""

    prior_turns: list[Turn]
    prompt: str
    images: list[ImagePart] = field(default_factory=list)
    stream: bool = False
    include_usage: bool = False
    model: str | None = None
    #: The caller's own end-user identifier, when supplied.  Carried through
    #: so a downstream integration can attribute turns; never used to pick a
    #: session, because a client is free to omit or reuse it.
    user: str | None = None


def normalise_text(value: str) -> str:
    """Collapse all whitespace runs to single spaces and strip.

    Used wherever text is compared rather than displayed.  Clients re-render
    history between turns — trailing newlines appear, CRLF creeps in — and an
    exact comparison would call an unchanged conversation "rewritten".
    """
    return " ".join(str(value or "").split())


# ---------------------------------------------------------------------------
# request parsing
# ---------------------------------------------------------------------------


def _image_part(block: dict) -> ImagePart:
    """Map one ``image_url`` content part onto an :class:`ImagePart`."""
    spec = block.get("image_url")
    if isinstance(spec, str):
        url = spec
    elif isinstance(spec, dict):
        url = spec.get("url")
    else:
        url = None
    if not isinstance(url, str) or not url.strip():
        raise OpenAIRequestError(
            "image_url content parts must carry a url",
            param="messages",
        )
    url = url.strip()

    if url.startswith("data:"):
        header, _, payload = url.partition(",")
        if not payload:
            raise OpenAIRequestError(
                "data: image URLs must carry base64 payload after the comma",
                param="messages",
            )
        mime = header[5:].split(";", 1)[0].strip().lower() or "image/png"
        if mime not in _ALLOWED_IMAGE_MIMES:
            raise OpenAIRequestError(
                f"Unsupported image type: {mime}. Supported: "
                + ", ".join(sorted(_ALLOWED_IMAGE_MIMES)),
                param="messages",
            )
        return ImagePart(mime_type=mime, data=payload)

    if not url.lower().startswith(("http://", "https://")):
        raise OpenAIRequestError(
            "image_url must be a data: URL or an http(s) URL",
            param="messages",
        )
    # The mime type of a remote image is only known once fetched; the caller
    # overwrites this from the response's Content-Type.
    return ImagePart(mime_type="image/png", url=url)


def _split_content(content: Any, *, role: str) -> tuple[str, list[ImagePart]]:
    """Flatten a message's content into text plus any image parts.

    ``None`` content is legal OpenAI history on an assistant turn that carried
    only ``tool_calls``; it becomes empty text.  System and user content are
    never nullable in the OpenAI schema, so a null there is malformed input.
    """
    if content is None:
        if role == "assistant":
            return "", []
        raise OpenAIRequestError(
            f"{role} messages must carry content", param="messages",
        )
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        raise OpenAIRequestError(
            "message content must be a string or an array of content parts",
            param="messages",
        )

    parts: list[str] = []
    images: list[ImagePart] = []
    for block in content:
        if not isinstance(block, dict):
            raise OpenAIRequestError(
                "each content part must be an object", param="messages",
            )
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text") or ""))
        elif kind == "image_url":
            images.append(_image_part(block))
        else:
            raise OpenAIRequestError(
                f"Unsupported content part type: {kind!r}. This endpoint "
                "accepts 'text' and 'image_url'.",
                param="messages",
            )
    return "".join(parts), images


def parse_chat_request(body: dict) -> ParsedChatRequest:
    """Split a chat completion into prior turns and the turn to run.

    A ``system`` message is folded as a prefix onto the FIRST user turn rather
    than replacing anything: the agent's own system prompt is its identity, and
    a caller cannot overwrite it.  Folding onto the first user turn (not the
    last) keeps a few-shot prefix intact, and keeps the instruction out of a
    seeded *assistant* turn, where it would read as something the agent said.

    Images are collected from the turn being run only.  History images are
    already in the session on the normal path; on a fork the history is seeded
    as text, so :func:`seed_text_for` marks where an image was.
    """
    if not isinstance(body, dict):
        raise OpenAIRequestError("request body must be a JSON object")

    if body.get("tools") or body.get("functions"):
        # Silently ignoring these is the dangerous option: the client would
        # wait for a tool call that can never arrive, and the request would
        # look like a hang rather than a refusal.
        raise OpenAIRequestError(
            "This endpoint does not accept client-declared tools. The agent "
            "runs its own tools inside the turn and returns the final answer.",
            code="tools_not_supported",
            param="tools",
        )

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise OpenAIRequestError(
            "messages must be a non-empty array", param="messages",
        )

    system_parts: list[str] = []
    turns: list[Turn] = []
    images_by_index: dict[int, list[ImagePart]] = {}

    for message in messages:
        if not isinstance(message, dict):
            raise OpenAIRequestError(
                "each message must be an object", param="messages",
            )
        role = message.get("role")
        # The role decides before the content is read: a dropped role's
        # content is never inspected, so a tool result carrying an
        # unsupported part cannot refuse a request that was going to
        # discard that part anyway.
        if role not in _CONTENT_ROLES:
            continue
        text, images = _split_content(message.get("content"), role=str(role))
        if role == "system":
            if images:
                raise OpenAIRequestError(
                    "system messages cannot carry images", param="messages",
                )
            system_parts.append(text)
            continue
        turns.append(Turn(role=str(role), content=text))
        if images:
            images_by_index[len(turns) - 1] = images

    if not turns or turns[-1].role != "user":
        raise OpenAIRequestError(
            "the last message must be a user message", param="messages",
        )

    if system_parts:
        prefix = "\n\n".join(p for p in system_parts if p)
        if prefix:
            first_user = next(
                (i for i, t in enumerate(turns) if t.role == "user"), None,
            )
            if first_user is not None:
                turns[first_user] = Turn(
                    role="user",
                    content=f"{prefix}\n\n{turns[first_user].content}",
                )

    stream_options = body.get("stream_options")
    include_usage = bool(
        isinstance(stream_options, dict) and stream_options.get("include_usage")
    )
    user = body.get("user")

    return ParsedChatRequest(
        prior_turns=turns[:-1],
        prompt=turns[-1].content,
        images=images_by_index.get(len(turns) - 1, []),
        stream=bool(body.get("stream")),
        include_usage=include_usage,
        model=str(body["model"]) if body.get("model") else None,
        user=str(user) if isinstance(user, str) and user.strip() else None,
    )


def seed_text_for(turn: Turn, images: Iterable[ImagePart] = ()) -> str:
    """The text written into a seeded turn, marking any dropped images.

    Seeding writes into the event log, which is text; a history image cannot
    be reconstructed there.  A visible marker beats a silent drop — otherwise
    a forked conversation reads as though the user never sent the picture.
    """
    count = len(list(images))
    if not count:
        return turn.content
    marker = f"[{count} image{'s' if count != 1 else ''} omitted from replayed history]"
    return f"{turn.content}\n\n{marker}" if turn.content else marker


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------

_ZERO_USAGE: dict[str, Any] = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
}


def usage_from_cost_summary(summary: dict | None) -> dict:
    """Map a harness ``cost_summary`` onto an OpenAI ``usage`` block.

    Zeros when the runtime reported nothing, rather than a fabricated count.

    One OpenAI completion is many LLM calls: the harness iterates over tool
    results inside a single turn, and ``cost_summary`` is the sum across that
    whole turn.  ``prompt_tokens`` is therefore the tokens the AGENT spent, not
    the size of the caller's prompt — correct for billing, and the number a
    caller reconciling against their invoice needs.
    """
    if not isinstance(summary, dict):
        return dict(_ZERO_USAGE)
    prompt = int(summary.get("total_input_tokens") or 0)
    completion = int(summary.get("total_output_tokens") or 0)
    usage: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
    cached = int(summary.get("total_cache_read_tokens") or 0)
    if cached:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    reasoning = int(summary.get("total_reasoning_tokens") or 0)
    if reasoning:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return usage


# ---------------------------------------------------------------------------
# responses
# ---------------------------------------------------------------------------


def build_chat_response(
    *,
    completion_id: str,
    model: str,
    content: str,
    created: int,
    usage: dict | None = None,
    finish_reason: str = "stop",
    reasoning: str | None = None,
) -> dict:
    """Wrap an agent's answer as a chat completion."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning:
        # Not in the OpenAI schema, but the de-facto field every reasoning
        # provider (DeepSeek, Qwen, GLM) and every client that renders
        # reasoning agreed on.  Clients that do not know it ignore it.
        message["reasoning_content"] = reasoning
    return {
        "id": completion_id,
        "object": COMPLETION_OBJECT,
        "created": created,
        "model": model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": finish_reason}
        ],
        "usage": dict(usage) if usage else dict(_ZERO_USAGE),
    }


def build_models_response(
    *, model: str, created: int, owned_by: str = "surogate-agent",
) -> dict:
    """The single-entry model list an agent advertises."""
    return {
        "object": "list",
        "data": [
            {
                "id": model,
                "object": "model",
                "created": created,
                "owned_by": owned_by,
            }
        ],
    }


def build_error_body(error: OpenAIRequestError) -> dict:
    """The error envelope OpenAI SDKs parse.

    FastAPI's default ``{"detail": ...}`` is not it: the SDKs read
    ``error.message`` and surface ``None`` for anything else, so a refusal
    would reach the developer as an empty string.
    """
    body: dict[str, Any] = {
        "message": error.message,
        "type": error.type,
    }
    body["param"] = error.param
    body["code"] = error.code
    return {"error": body}


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------


def _chunk(
    *, completion_id: str, model: str, created: int, choices: list[dict],
) -> dict:
    return {
        "id": completion_id,
        "object": CHUNK_OBJECT,
        "created": created,
        "model": model,
        "choices": choices,
    }


def build_role_chunk(*, completion_id: str, model: str, created: int) -> dict:
    """The opening frame.  Clients key off ``delta.role`` to open a message."""
    return _chunk(
        completion_id=completion_id, model=model, created=created,
        choices=[{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    )


def build_chunk(
    *,
    completion_id: str,
    model: str,
    created: int,
    content: str | None = None,
    reasoning: str | None = None,
) -> dict:
    """One content or reasoning delta.

    Reasoning travels as ``reasoning_content`` so a client rendering it can
    keep it visually separate from the answer; the two never share a frame,
    which is what the harness already emits (separate ``llm.delta`` events).
    """
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    return _chunk(
        completion_id=completion_id, model=model, created=created,
        choices=[{"index": 0, "delta": delta, "finish_reason": None}],
    )


def build_final_chunk(
    *, completion_id: str, model: str, created: int, finish_reason: str = "stop",
) -> dict:
    """The frame that closes the choice.  Always precedes ``[DONE]``."""
    return _chunk(
        completion_id=completion_id, model=model, created=created,
        choices=[{"index": 0, "delta": {}, "finish_reason": finish_reason}],
    )


def build_usage_chunk(
    *, completion_id: str, model: str, created: int, usage: dict,
) -> dict:
    """The trailing usage frame, emitted only for ``include_usage``.

    Carries an empty ``choices`` array, which is what the OpenAI SDK expects
    and what tells it this frame is accounting rather than content.
    """
    payload = _chunk(
        completion_id=completion_id, model=model, created=created, choices=[],
    )
    payload["usage"] = dict(usage)
    return payload


def sse_data(payload: dict | str) -> str:
    """Render one SSE ``data:`` line, including the blank-line terminator."""
    if isinstance(payload, str):
        return f"data: {payload}\n\n"
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
