"""generate_image asks each provider the way it takes image requests.

Grounded in what providers answered: llm.buzz refuses ``modalities``
(400 unsupported_parameter) and sets it itself; OpenRouter needs it, and
reads size and aspect only from ``image_config``; image-only models and
transparency are served by ``/images/generations``.
"""

from __future__ import annotations

import base64
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from surogates.tools.builtin import image_request
from surogates.tools.builtin.media_gen import MediaGenConfig, _generate_image_handler

MODEL = "google/gemini-3.1-flash-image"


def _png(width=1, height=1, mode="RGB", color=(200, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _data_url(data: bytes, mime="image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


class _Refused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Error code: {status} - {{'error': {{'message': '{message}'}}}}")
        self.status_code = status
        self.body = {"error": {"message": message, "code": "unsupported_parameter"}}


class _Provider:
    """A fake image provider: ``/models``, chat completions and the Images API."""

    def __init__(
        self, *, models=None, chat=None, images=None, base_url="https://provider.test/v1/",
    ):
        self.base_url = base_url
        self._models = models
        self._chat = chat
        self._images = images
        self.chat_calls: list[dict] = []
        self.images_calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.images = SimpleNamespace(generate=self._generate)

    async def get(self, path, *, cast_to, options):
        assert path == "models"
        if self._models is None:
            raise _Refused(404, "no model list")
        return {"data": self._models}

    async def _create(self, **kwargs):
        self.chat_calls.append(kwargs)
        return self._chat(kwargs)

    async def _generate(self, **kwargs):
        self.images_calls.append(kwargs)
        return self._images(kwargs)


def _chat_image(data: bytes, *, cost=None, text="done"):
    message = SimpleNamespace(
        images=[{"image_url": {"url": _data_url(data)}}], content=text, role="assistant",
    )
    message.model_dump = lambda **_: {"images": message.images, "content": text}
    usage = SimpleNamespace(cost=cost) if cost is not None else None
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], model=MODEL, usage=usage)


def _images_answer(data: bytes):
    return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(data).decode(), url=None)])


def _listed(outputs, inputs=("text",)):
    return [{"id": MODEL, "architecture": {
        "input_modalities": list(inputs), "output_modalities": list(outputs),
    }}]


class _Hooks:
    def __init__(self):
        self.settled: list[int] = []

    async def authorize(self, requested_cents=None):
        return {"balance_id": "b1", "reserved_cents": requested_cents}

    async def settle(self, receipt, actual_cents, external_ref):
        self.settled.append(actual_cents)


async def _run(provider, arguments, tmp_path, *, hooks=None, channel=None):
    cfg = MediaGenConfig(
        image_client=provider, image_model=MODEL,
        budget_authorize=hooks.authorize if hooks else None,
        budget_settle=hooks.settle if hooks else None,
    )
    return json.loads(await _generate_image_handler(
        {"prompt": "a price card", **arguments},
        media_gen=cfg, workspace_path=str(tmp_path), channel=channel,
    ))


@pytest.fixture(autouse=True)
def _fresh_caches():
    image_request._listing_cache.clear()
    image_request._shape_cache.clear()


@pytest.mark.asyncio
async def test_a_gateway_that_sets_modalities_itself_gets_a_plain_request_and_keeps_it(tmp_path):
    def chat(kwargs):
        if "modalities" in kwargs["extra_body"]:
            raise _Refused(400, "The parameter 'modalities' is not supported by this gateway profile")
        return _chat_image(_png())

    provider = _Provider(chat=chat)
    first = await _run(provider, {}, tmp_path)
    assert "error" not in first
    assert [call["extra_body"] for call in provider.chat_calls] == [
        {"modalities": ["image", "text"]}, {},
    ]
    await _run(provider, {}, tmp_path)
    assert provider.chat_calls[-1]["extra_body"] == {}
    assert len(provider.chat_calls) == 3


@pytest.mark.asyncio
async def test_openrouter_gets_its_modalities_and_image_config(tmp_path):
    provider = _Provider(models=_listed(["image"]), chat=lambda _: _chat_image(_png(1072, 1920)))
    result = await _run(provider, {"aspect_ratio": "9:16", "image_size": "2K"}, tmp_path)
    assert provider.chat_calls[0]["extra_body"] == {
        "modalities": ["image"], "image_config": {"aspect_ratio": "9:16", "image_size": "2K"},
    }
    assert (result["width"], result["height"]) == (1072, 1920)
    assert "not_as_asked" not in result


@pytest.mark.asyncio
async def test_an_image_only_model_falls_back_to_the_images_api(tmp_path):
    def chat(kwargs):
        raise _Refused(400, "The parameter 'modalities' is not supported by this gateway profile")

    def images(kwargs):
        if "aspect_ratio" in kwargs["extra_body"]:
            raise _Refused(400, "Unknown parameter: 'aspect_ratio'")
        return _images_answer(_png(1536, 1024))

    provider = _Provider(models=_listed(["image"]), chat=chat, images=images)
    result = await _run(provider, {"aspect_ratio": "16:9"}, tmp_path)
    assert "error" not in result
    assert len(provider.chat_calls) == 1
    assert provider.images_calls[-1]["size"] == "1536x1024"


@pytest.mark.asyncio
async def test_a_gateway_that_serves_its_own_sizes_gets_the_ratio_not_an_openai_size(tmp_path):
    # llm.buzz admits an Images request by the model's declared sizes: Gemini
    # serves 1344x768 for 16:9 and refuses 1536x1024.
    def chat(kwargs):
        raise _Refused(400, "The parameter 'modalities' is not supported by this gateway profile")

    def images(kwargs):
        if "size" in kwargs:
            raise _Refused(400, f"{MODEL} serves size 1024x1024, 1344x768; resend with one of them.")
        assert kwargs["extra_body"]["aspect_ratio"] == "16:9"
        return _images_answer(_png(1344, 768))

    provider = _Provider(models=_listed(["image"]), chat=chat, images=images)
    result = await _run(provider, {"aspect_ratio": "16:9"}, tmp_path)
    assert "error" not in result
    assert len(provider.images_calls) == 1
    assert "not_as_asked" not in result


@pytest.mark.asyncio
async def test_transparency_goes_to_the_images_api_and_is_checked(tmp_path):
    clear = _png(4, 4, mode="RGBA", color=(0, 0, 0, 0))
    provider = _Provider(images=lambda _: _images_answer(clear))
    result = await _run(provider, {"transparent_background": True}, tmp_path)
    assert provider.images_calls[0]["extra_body"] == {
        "background": "transparent", "output_format": "png",
    }
    assert provider.chat_calls == []
    assert "not_as_asked" not in result


@pytest.mark.asyncio
async def test_a_model_without_transparency_says_so(tmp_path):
    def images(_):
        raise _Refused(404, "No endpoints found for /images/generations")

    provider = _Provider(images=images, chat=lambda _: _chat_image(_png()))
    result = await _run(provider, {"transparent_background": True}, tmp_path)
    assert "error" not in result
    assert any("transparent" in note for note in result["not_as_asked"])


@pytest.mark.asyncio
async def test_an_ignored_aspect_ratio_is_reported_not_hidden(tmp_path):
    provider = _Provider(chat=lambda _: _chat_image(_png(1024, 1024)))
    result = await _run(provider, {"aspect_ratio": "16:9"}, tmp_path)
    assert result["not_as_asked"] == [
        "Asked for 16:9, got 1024x1024: this image model did not apply the aspect ratio."
    ]


@pytest.mark.asyncio
async def test_a_server_error_is_not_retried_in_another_shape(tmp_path):
    def chat(_):
        raise _Refused(503, "upstream overloaded")

    provider = _Provider(chat=chat)
    result = await _run(provider, {}, tmp_path)
    assert len(provider.chat_calls) == 1
    assert result["error"] == (
        "Image generation failed: the image provider is unavailable right now; "
        "try again shortly (upstream overloaded)."
    )


@pytest.mark.asyncio
async def test_a_refusal_reads_as_one_line_not_raw_json(tmp_path):
    def chat(_):
        raise _Refused(400, "Prompt was blocked by the safety filter.")

    provider = _Provider(chat=chat, images=lambda _: (_ for _ in ()).throw(_Refused(400, "blocked")))
    result = await _run(provider, {}, tmp_path)
    assert result["error"].startswith("Image generation failed: the image provider rejected the request")
    assert "{" not in result["error"]


@pytest.mark.asyncio
async def test_the_buyer_pays_the_reported_cost_only_for_a_saved_image(tmp_path):
    hooks = _Hooks()
    provider = _Provider(chat=lambda _: _chat_image(_png(), cost=0.039))
    await _run(provider, {}, tmp_path, hooks=hooks)
    assert hooks.settled == [4]

    hooks = _Hooks()
    provider = _Provider(chat=lambda _: _chat_image(_png(), cost=0.12))
    await _run(provider, {}, tmp_path, hooks=hooks)
    assert hooks.settled == [12]


@pytest.mark.asyncio
async def test_bytes_that_are_not_an_image_cost_nothing(tmp_path):
    hooks = _Hooks()
    provider = _Provider(chat=lambda _: _chat_image(b"<html>rate limited</html>"))
    result = await _run(provider, {}, tmp_path, hooks=hooks)
    assert result["error"] == "The image model returned data that is not an image"
    assert hooks.settled == [0]
    assert not (tmp_path / "media").exists()


@pytest.mark.asyncio
async def test_the_extension_follows_the_bytes(tmp_path):
    provider = _Provider(chat=lambda _: _chat_image(_png()))
    result = await _run(provider, {"output_path": "media/hero-bread.jpg"}, tmp_path)
    assert result["path"] == "media/hero-bread.png"
    assert (tmp_path / "media" / "hero-bread.png").is_file()
    assert "Saved as media/hero-bread.png" in result["not_as_asked"][0]


@pytest.mark.asyncio
async def test_a_model_that_takes_no_input_image_refuses_before_any_charge(tmp_path):
    hooks = _Hooks()
    provider = _Provider(models=_listed(["image"], inputs=("text",)))
    result = await _run(
        provider, {"input_images": ["data:image/png;base64," + base64.b64encode(_png()).decode()]},
        tmp_path, hooks=hooks,
    )
    assert "cannot start from an input image" in result["error"]
    assert provider.chat_calls == []
    assert hooks.settled == []


@pytest.mark.asyncio
async def test_a_text_model_set_as_the_image_model_is_named(tmp_path):
    provider = _Provider(models=_listed(["text"]))
    result = await _run(provider, {}, tmp_path)
    assert f"the configured image model {MODEL} does not generate images" in result["error"]


@pytest.mark.parametrize(("channel", "says"), [
    ("whatsapp", "MEDIA:"),
    ("slack", "MEDIA:"),
    ("scheduled", "Nobody sees this run"),
    ("web", "Saved in the workspace"),
])
@pytest.mark.asyncio
async def test_delivery_says_how_the_image_reaches_anyone(tmp_path, channel, says):
    provider = _Provider(chat=lambda _: _chat_image(_png()))
    result = await _run(provider, {}, tmp_path, channel=channel)
    assert says in result["delivery"]
    assert result["path"] in result["delivery"]


async def test_an_image_link_to_an_internal_address_is_not_fetched():
    with pytest.raises(image_request.ImageRequestError, match="unsafe"):
        await image_request._image_bytes("https://127.0.0.1/generated.png")


@pytest.mark.asyncio
async def test_one_model_listing_serves_every_model_of_a_provider():
    provider = _Provider(models=[
        {"id": "a", "architecture": {"output_modalities": ["image"], "input_modalities": ["text"]}},
        {"id": "b", "architecture": {"output_modalities": ["image", "text"], "input_modalities": ["image"]}},
    ])
    asked = []
    get = provider.get

    async def counting_get(path, **kwargs):
        asked.append(path)
        return await get(path, **kwargs)

    provider.get = counting_get
    a = await image_request.model_info(provider, "a")
    b = await image_request.model_info(provider, "b")
    assert (a.listed, a.makes_text, a.takes_images) == (True, False, False)
    assert (b.listed, b.makes_text, b.takes_images) == (True, True, True)
    assert asked == ["models"]
