"""Main-loop calls send sampling values only when the session sets them.

Unset, the request carries none and the model's own (or its configured)
defaults apply.
"""

from surogates.harness.llm_call import sampling_kwargs


def test_unset_sends_nothing():
    assert sampling_kwargs({}) == {}
    assert sampling_kwargs({"temperature": None, "max_tokens": None, "reasoning_effort": None}) == {}


def test_set_values_are_sent():
    assert sampling_kwargs({"temperature": 0, "max_tokens": 4096, "reasoning_effort": "none", "other": 1}) == {
        "temperature": 0,
        "max_tokens": 4096,
        "reasoning_effort": "none",
    }


def test_thinking_goes_in_the_chat_template_only_when_set():
    assert sampling_kwargs({"enable_thinking": False}) == {
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }
    assert sampling_kwargs({"enable_thinking": True}) == {
        "extra_body": {"chat_template_kwargs": {"enable_thinking": True}},
    }
    assert sampling_kwargs({"enable_thinking": None}) == {}
