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
