"""A value the config file gives follows the file: a rotated key reaches the next ``load_settings()``.

``load_settings()`` puts the config file's values into the environment (``SUROGATES_*``), where pydantic-settings
reads them, but only a variable that was not set yet.  The value it put there itself counted as set: a key or
password changed in the file (a rotated secret in a mounted config) never reached the process, although the
settings are built again whenever the file's bytes change.  A real environment variable still wins.
"""

from __future__ import annotations

import os

import pytest

import surogates.config as config
from surogates.config import load_settings
from surogates.mcp_proxy.config import load_proxy_settings


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    before = dict(os.environ)
    for key in [k for k in os.environ if k.upper().startswith("SUROGATES_")]:
        del os.environ[key]
    path = tmp_path / "config.yaml"
    os.environ["SUROGATES_CONFIG"] = str(path)
    monkeypatch.setattr(config, "_config_environ", {})
    monkeypatch.setattr(config, "_settings_cache", None)

    def write(text: str) -> None:
        path.write_text(text)

    yield write
    os.environ.clear()
    os.environ.update(before)


def test_a_rotated_value_in_the_file_is_read_again(config_file):
    config_file("llm:\n  api_key: first-key\n")
    assert load_settings().llm.api_key == "first-key"
    config_file("llm:\n  api_key: rotated-key\n")
    assert load_settings().llm.api_key == "rotated-key"


def test_a_value_taken_out_of_the_file_is_no_longer_given(config_file):
    config_file("llm:\n  api_key: first-key\n  base_url: http://engine.test/v1\n")
    assert load_settings().llm.base_url == "http://engine.test/v1"
    config_file("llm:\n  api_key: first-key\n")
    assert load_settings().llm.base_url == ""
    assert "SUROGATES_LLM_BASE_URL" not in os.environ


def test_a_real_environment_variable_still_wins(config_file):
    os.environ["SUROGATES_LLM_API_KEY"] = "from-the-operator"
    config_file("llm:\n  api_key: first-key\n")
    assert load_settings().llm.api_key == "from-the-operator"
    config_file("llm:\n  api_key: rotated-key\n")
    assert load_settings().llm.api_key == "from-the-operator"
    config_file("llm:\n  base_url: http://engine.test/v1\n")
    assert load_settings().llm.api_key == "from-the-operator"


def test_a_variable_set_by_someone_else_after_the_file_gave_it_is_theirs(config_file):
    config_file("llm:\n  api_key: first-key\n")
    assert load_settings().llm.api_key == "first-key"
    os.environ["SUROGATES_LLM_API_KEY"] = "set-later"
    config_file("llm:\n  api_key: rotated-key\n")
    assert load_settings().llm.api_key == "set-later"


@pytest.mark.parametrize("gone", ["", "not: [valid yaml"])
def test_a_file_that_is_empty_or_unreadable_keeps_what_it_gave(config_file, gone):
    config_file("llm:\n  api_key: first-key\n")
    assert load_settings().llm.api_key == "first-key"
    config_file(gone)
    assert load_settings().llm.api_key == "first-key"


def test_the_mcp_proxy_settings_follow_the_file_too(config_file):
    config_file("platform_api_token: first-token\n")
    assert load_proxy_settings().platform_api_token == "first-token"
    config_file("platform_api_token: rotated-token\n")
    assert load_proxy_settings().platform_api_token == "rotated-token"
