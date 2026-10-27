"""Short auxiliary calls turn thinking off where the model takes the switch."""

from surogates.harness.auxiliary_llm import reasoning_disabled_extra_body

_BYO = "https://ops.example/proxy/services/_model/592b98e9/v1"


def test_a_model_that_takes_the_thinking_switch_gets_it_off():
    assert reasoning_disabled_extra_body("surogate3.7", _BYO) == {
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_the_pro_tier_gets_nothing():
    # Claude behind the pro sentinel has no chat template to switch.
    assert reasoning_disabled_extra_body("surogate-pro", _BYO) is None


def test_a_model_without_the_switch_gets_nothing():
    assert reasoning_disabled_extra_body("gemini-3.7-flash", _BYO) is None


async def test_create_without_reasoning_merges_and_falls_back() -> None:
    from types import SimpleNamespace

    from surogates.harness.auxiliary_llm import create_without_reasoning

    sent: list[dict] = []

    async def create(**request):
        sent.append(request)
        if len(sent) == 1:
            raise TypeError("unexpected keyword argument 'chat_template_kwargs'")
        return "ok"

    client = SimpleNamespace(base_url=_BYO, chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    request = {"model": "surogate3.7", "messages": [], "extra_body": {"top_k": 5}}
    assert await create_without_reasoning(client, **request) == "ok"
    assert sent[0]["extra_body"] == {"top_k": 5, "chat_template_kwargs": {"enable_thinking": False}}
    assert sent[1] == request
