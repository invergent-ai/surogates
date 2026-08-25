#!/usr/bin/env python3
"""Drive a live agent through the OpenAI-compatible surface, with the real SDK.

Not a unit test: the point is that an ordinary OpenAI client, the one an
integrator will actually use, works against a running agent end to end. Every
check reports RED / YELLOW / GREEN with the evidence it saw, so a failure names
what broke rather than just failing.

    python scripts/openai_conformance.py \\
        --base-url https://<agent-host>/v1/api \\
        --api-key surg_sk_...

Optional:
    --other-agent-key   a key minted for a DIFFERENT agent, to prove the
                        cross-agent refusal (skipped when absent)
    --revoked-key       a key that has been revoked, to prove it stops working
    --skip-slow         omit the long-turn check
    --image PATH        a PNG/JPEG to use for the vision check
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

try:
    from openai import APIStatusError, OpenAI
except ImportError:  # pragma: no cover - operator-facing
    print("This script needs the OpenAI SDK:  pip install openai")
    raise SystemExit(2) from None


GREEN, YELLOW, RED, SKIP = "GREEN", "YELLOW", "RED", "SKIP"

# A 1x1 red PNG. Enough to prove the multimodal path carries an image all the
# way to the model without shipping a fixture file.
_TINY_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    "IQAAAABJRU5ErkJggg=="
)


@dataclass
class Result:
    name: str
    light: str
    detail: str


@dataclass
class Runner:
    client: OpenAI
    model: str
    results: list[Result] = field(default_factory=list)

    def check(self, name: str, fn: Callable[[], tuple[str, str]]) -> None:
        started = time.monotonic()
        try:
            light, detail = fn()
        except AssertionError as exc:
            light, detail = RED, f"assertion failed: {exc}"
        except APIStatusError as exc:
            body = ""
            try:
                body = json.dumps(exc.response.json())[:200]
            except Exception:
                body = (exc.response.text or "")[:200]
            light, detail = RED, f"HTTP {exc.status_code}: {body}"
        except Exception as exc:
            light, detail = RED, f"{type(exc).__name__}: {exc}"
        elapsed = time.monotonic() - started
        self.results.append(Result(name, light, f"{detail}  [{elapsed:.1f}s]"))
        print(f"[{light:<6}] {name}\n         {self.results[-1].detail}\n", flush=True)


def _text_of(response: Any) -> str:
    return (response.choices[0].message.content or "").strip()


def build_checks(
    runner: Runner,
    *,
    base_url: str,
    api_key: str,
    other_agent_key: str | None,
    revoked_key: str | None,
    skip_slow: bool,
    image_path: str | None,
) -> None:
    client, model = runner.client, runner.model
    # Carried between checks so multi-turn continuity is a real continuation.
    state: dict[str, Any] = {}

    def models_list() -> tuple[str, str]:
        listing = client.models.list()
        ids = [m.id for m in listing.data]
        assert ids, "the endpoint advertised no model"
        assert model in ids, f"{model!r} not among {ids}"
        return GREEN, f"advertises {ids}"

    def single_turn() -> tuple[str, str]:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with exactly: PONG"}],
        )
        text = _text_of(response)
        assert text, "empty content on a 200"
        usage = response.usage
        assert usage is not None, "no usage block"
        light = GREEN if usage.prompt_tokens > 0 else YELLOW
        note = "" if usage.prompt_tokens else "  (usage reported zero)"
        state["turn1"] = text
        return light, (
            f"answered {text[:60]!r}; finish_reason="
            f"{response.choices[0].finish_reason}; "
            f"usage={usage.prompt_tokens}/{usage.completion_tokens}{note}"
        )

    def multi_turn_memory() -> tuple[str, str]:
        """The agent has to remember something from turn one."""
        first = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": "Remember this word: ARTICHOKE. Reply with just: OK",
            }],
        )
        first_text = _text_of(first)
        second = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "user",
                 "content": "Remember this word: ARTICHOKE. Reply with just: OK"},
                {"role": "assistant", "content": first_text},
                {"role": "user",
                 "content": "What word did I ask you to remember? One word."},
            ],
        )
        text = _text_of(second)
        action = second.headers.get("x-surogate-conversation-action") if hasattr(
            second, "headers",
        ) else None
        remembered = "ARTICHOKE" in text.upper()
        state["session_continued"] = remembered
        if not remembered:
            return RED, f"did not recall the word; said {text[:80]!r}"
        return GREEN, (
            f"recalled it ({text[:50]!r})"
            + (f"; conversation-action={action}" if action else "")
        )

    def streaming() -> tuple[str, str]:
        stream = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Count from 1 to 5."}],
            stream=True,
        )
        chunks, text, roles, finishes = 0, "", 0, []
        for chunk in stream:
            chunks += 1
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if getattr(delta, "role", None):
                roles += 1
            if delta.content:
                text += delta.content
            if chunk.choices[0].finish_reason:
                finishes.append(chunk.choices[0].finish_reason)
        assert chunks >= 2, f"only {chunks} chunk(s)"
        assert text.strip(), "stream produced no content"
        assert finishes, "no finish_reason frame — client reads that as truncated"
        return GREEN, (
            f"{chunks} chunks, {roles} role frame(s), "
            f"finish={finishes[-1]}, text={text.strip()[:60]!r}"
        )

    def streaming_usage() -> tuple[str, str]:
        stream = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Say: DONE"}],
            stream=True,
            stream_options={"include_usage": True},
        )
        usage = None
        for chunk in stream:
            if getattr(chunk, "usage", None):
                usage = chunk.usage
        if usage is None:
            return RED, "include_usage requested but no usage frame arrived"
        return GREEN, (
            f"usage frame: {usage.prompt_tokens}/{usage.completion_tokens}"
        )

    def reasoning() -> tuple[str, str]:
        stream = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": "Think it through, then answer: what is 17 * 23?",
            }],
            stream=True,
        )
        reasoning_text, content = "", ""
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            reasoning_text += getattr(delta, "reasoning_content", None) or ""
            content += delta.content or ""
        if reasoning_text:
            return GREEN, f"{len(reasoning_text)} chars of reasoning_content"
        return YELLOW, (
            "no reasoning_content — expected unless this agent's model emits "
            f"reasoning; answer was {content.strip()[:60]!r}"
        )

    def vision() -> tuple[str, str]:
        if image_path:
            with open(image_path, "rb") as handle:
                payload = base64.b64encode(handle.read()).decode("ascii")
            mime = mimetypes.guess_type(image_path)[0] or "image/png"
        else:
            payload, mime = _TINY_PNG, "image/png"
        response = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text",
                     "text": "Describe this image in a few words."},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{mime};base64,{payload}"}},
                ],
            }],
        )
        text = _text_of(response)
        assert text, "empty answer for an image prompt"
        light = GREEN if image_path else YELLOW
        note = "" if image_path else "  (1x1 pixel; pass --image for a real one)"
        return light, f"answered {text[:70]!r}{note}"

    def client_tools_refused() -> tuple[str, str]:
        """Must be a clean refusal, never a hang."""
        try:
            client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "hi"}],
                tools=[{
                    "type": "function",
                    "function": {"name": "get_weather", "parameters": {}},
                }],
            )
        except APIStatusError as exc:
            if exc.status_code != 400:
                return RED, f"refused with {exc.status_code}, expected 400"
            body = exc.response.json()
            code = (body.get("error") or {}).get("code")
            message = (body.get("error") or {}).get("message") or ""
            assert message, "error envelope carried no message — SDKs show nothing"
            return GREEN, f"400 {code!r}: {message[:80]!r}"
        return RED, "accepted client-declared tools; a client would hang"

    def error_envelope() -> tuple[str, str]:
        try:
            client.chat.completions.create(model=model, messages=[])
        except APIStatusError as exc:
            body = exc.response.json()
            assert "error" in body, f"not an OpenAI error envelope: {body}"
            assert body["error"].get("message"), "empty message"
            return GREEN, f"{exc.status_code} {body['error']['message'][:70]!r}"
        return RED, "empty messages array was accepted"

    def wrong_agent_key() -> tuple[str, str]:
        if not other_agent_key:
            return SKIP, "pass --other-agent-key to check the cross-agent refusal"
        stranger = OpenAI(
            base_url=base_url, api_key=other_agent_key,
            default_query=client._custom_query or None,
        )
        try:
            stranger.models.list()
        except APIStatusError as exc:
            if exc.status_code == 403:
                return GREEN, "403 — a key bound elsewhere cannot drive this agent"
            return RED, f"refused with {exc.status_code}, expected 403"
        return RED, "another agent's key was accepted"

    def revoked() -> tuple[str, str]:
        if not revoked_key:
            return SKIP, "pass --revoked-key to check revocation"
        dead = OpenAI(
            base_url=base_url, api_key=revoked_key,
            default_query=client._custom_query or None,
        )
        try:
            dead.models.list()
        except APIStatusError as exc:
            if exc.status_code in (401, 403):
                return GREEN, f"{exc.status_code} — revoked key rejected"
            return RED, f"got {exc.status_code}, expected 401/403"
        return RED, "a revoked key still works"

    def explicit_conversation() -> tuple[str, str]:
        """The header must pin a conversation regardless of message content."""
        scoped = client.with_options(
            default_headers={"X-Surogate-Conversation": f"conformance-{int(time.time())}"},
        )
        scoped.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": "Remember this number: 4242. Reply with just: OK",
            }],
        )
        second = scoped.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": "What number did I give you? Digits only.",
            }],
        )
        text = _text_of(second)
        if "4242" in text:
            return GREEN, f"server-held history worked ({text[:40]!r})"
        return RED, (
            "the pinned conversation did not carry history; "
            f"answered {text[:60]!r}"
        )

    def long_turn() -> tuple[str, str]:
        if skip_slow:
            return SKIP, "--skip-slow"
        started = time.monotonic()
        stream = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": (
                    "Take your time and think carefully, then write three "
                    "detailed paragraphs about the history of Bucharest."
                ),
            }],
            stream=True,
        )
        text, finished = "", False
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                text += chunk.choices[0].delta.content
            if chunk.choices and chunk.choices[0].finish_reason:
                finished = True
        elapsed = time.monotonic() - started
        assert finished, "stream ended without a finish_reason"
        assert text.strip(), "long turn produced nothing"
        return GREEN, f"{len(text)} chars over {elapsed:.0f}s, one response"

    for name, fn in [
        ("1  models list", models_list),
        ("2  single-turn text", single_turn),
        ("3  multi-turn memory", multi_turn_memory),
        ("4  streaming", streaming),
        ("5  streaming + include_usage", streaming_usage),
        ("6  reasoning", reasoning),
        ("7  image input", vision),
        ("8  client tools refused", client_tools_refused),
        ("9  OpenAI error envelope", error_envelope),
        ("10 wrong-agent key refused", wrong_agent_key),
        ("11 revoked key refused", revoked),
        ("12 pinned conversation", explicit_conversation),
        ("13 long turn stays one response", long_turn),
    ]:
        runner.check(name, fn)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="…/v1/api")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--model", help="defaults to the advertised model")
    parser.add_argument("--other-agent-key")
    parser.add_argument("--revoked-key")
    parser.add_argument("--image")
    parser.add_argument("--skip-slow", action="store_true")
    parser.add_argument(
        "--agent-id",
        help=(
            "send ?agent_id=... on every request. Only needed against a local "
            "or shared runtime reached by a host that carries no agent slug; "
            "a deployed agent resolves from its own hostname."
        ),
    )
    parser.add_argument(
        "--timeout", type=float, default=600.0,
        help="per-request timeout; agent turns are slower than a raw model",
    )
    args = parser.parse_args()

    client = OpenAI(
        base_url=args.base_url, api_key=args.api_key, timeout=args.timeout,
        max_retries=0,
        default_query={"agent_id": args.agent_id} if args.agent_id else None,
    )

    model = args.model
    if not model:
        try:
            model = client.models.list().data[0].id
        except Exception as exc:
            print(f"Could not reach {args.base_url}: {type(exc).__name__}: {exc}")
            return 2

    print("=" * 78)
    print(f"OpenAI conformance — {args.base_url}  (model: {model})")
    print("=" * 78 + "\n")

    runner = Runner(client=client, model=model)
    build_checks(
        runner,
        base_url=args.base_url,
        api_key=args.api_key,
        other_agent_key=args.other_agent_key,
        revoked_key=args.revoked_key,
        skip_slow=args.skip_slow,
        image_path=args.image,
    )

    counts: dict[str, int] = {}
    for result in runner.results:
        counts[result.light] = counts.get(result.light, 0) + 1
    print("=" * 78)
    print("SUMMARY: " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    failed = [r.name for r in runner.results if r.light == RED]
    if failed:
        print("FAILED: " + ", ".join(failed))
    print("=" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
