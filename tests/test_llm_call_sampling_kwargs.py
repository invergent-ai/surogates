"""Main-loop calls send sampling values only when the session sets them.

Unset, the request carries none and the model's own (or its configured)
defaults apply.
"""

from surogates.harness.llm_call import sampling_kwargs


def test_unset_sends_nothing():
    assert sampling_kwargs({}) == {}
    assert sampling_kwargs({"temperature": None, "max_tokens": None}) == {}


def test_set_values_are_sent():
    assert sampling_kwargs({"temperature": 0, "max_tokens": 4096, "other": 1}) == {
        "temperature": 0,
        "max_tokens": 4096,
    }
