"""config.yaml: a setting whose value is a list or a mapping is honoured as written (one JSON value, as in the
environment); a value no setting can take is left out with a warning, never without a word; an allow-list stays an
allow-list (empty or malformed never means "all")."""

from __future__ import annotations

import logging

import pytest

from surogates import config
from surogates.config import _flatten_yaml


@pytest.fixture()
def config_file(tmp_path, monkeypatch):
    """Write *text* as the config file and load the settings from it alone (no SUROGATES_ variable but its path).
    The file's values go into the environment (``_apply_config_environ``, not through monkeypatch): the whole
    environment is put back as it was afterwards, as tests/test_config_file_refresh.py does."""
    import os

    before = dict(os.environ)
    for key in [k for k in os.environ if k.upper().startswith("SUROGATES_")]:
        del os.environ[key]
    path = tmp_path / "config.yaml"
    os.environ["SUROGATES_CONFIG"] = str(path)
    monkeypatch.setattr(config, "_config_environ", {})
    monkeypatch.setattr(config, "_settings_cache", None)

    def load(text: str):
        path.write_text(text)
        return config.load_settings()

    yield load
    os.environ.clear()
    os.environ.update(before)


def test_lists_written_as_yaml_lists_are_read():
    flat = _flatten_yaml({"api": {"cors_origins": ["https://app.example"]},
                          "sandbox": {"allowed_images": ["ghcr.io/acme/tasks:*"],
                                      "ssh_egress_baseline_cidrs": ["10.0.0.0/8", "192.168.0.0/16"]}})
    assert flat == {"SUROGATES_API_CORS_ORIGINS": '["https://app.example"]',
                    "SUROGATES_SANDBOX_ALLOWED_IMAGES": '["ghcr.io/acme/tasks:*"]',
                    "SUROGATES_SANDBOX_SSH_EGRESS_BASELINE_CIDRS": '["10.0.0.0/8", "192.168.0.0/16"]'}


def test_the_settings_load_them(config_file):
    settings = config_file(
        "api:\n  cors_origins: ['https://app.example']\n"
        "sandbox:\n  allowed_images:\n    - ghcr.io/acme/tasks:*\n  ssh_egress_baseline_cidrs: [10.0.0.0/8]\n"
        "llm:\n  chat_template_kwargs: {reasoning_effort: 0.99}\n"
        "  models:\n    acme/model-x:\n      context_window: 204800\n"
    )
    assert settings.api.cors_origins == ["https://app.example"]
    assert settings.sandbox.allowed_images == ["ghcr.io/acme/tasks:*"]
    assert settings.sandbox.ssh_egress_baseline_cidrs == ["10.0.0.0/8"]
    assert settings.llm.chat_template_kwargs == {"reasoning_effort": 0.99}
    # A mapping setting goes in whole, not as keys no setting reads.
    assert settings.llm.models == {"acme/model-x": {"context_window": 204800}}


def test_the_environment_still_wins_over_the_file(config_file):
    import os

    os.environ["SUROGATES_SANDBOX_ALLOWED_IMAGES"] = '["ghcr.io/other/*"]'
    settings = config_file("sandbox:\n  allowed_images: [ghcr.io/acme/tasks:*]\n")
    assert settings.sandbox.allowed_images == ["ghcr.io/other/*"]


def test_scalars_and_nesting_are_unchanged():
    assert _flatten_yaml({"db": {"url": "postgresql://x", "pool_size": 5}, "sandbox": {"docker_pids_limit": 10,
                                                                                      "srt_enabled": True}}) == {
        "SUROGATES_DB_URL": "postgresql://x", "SUROGATES_DB_POOL_SIZE": "5",
        "SUROGATES_SANDBOX_DOCKER_PIDS_LIMIT": "10", "SUROGATES_SANDBOX_SRT_ENABLED": "True"}


def test_a_value_no_setting_can_take_is_left_out_with_a_warning(caplog):
    import datetime

    with caplog.at_level(logging.WARNING, logger="surogates.config"):
        flat = _flatten_yaml({"db": {"url": ["not", "a", "list", "setting"]},
                              "sandbox": {"image_idle_seconds": datetime.date(2026, 2, 12)}})
    assert flat == {}
    said = caplog.text
    assert "SUROGATES_DB_URL is a list" in said and "SUROGATES_SANDBOX_IMAGE_IDLE_SECONDS is a date" in said


def test_an_empty_key_keeps_the_default_and_says_so_for_a_list_setting(caplog):
    with caplog.at_level(logging.WARNING, logger="surogates.config"):
        assert _flatten_yaml({"sandbox": {"allowed_images": None, "docker_image": None}}) == {}
    # An empty cors_origins is not "no origin" but its default "*": said, never quiet.
    assert "SUROGATES_SANDBOX_ALLOWED_IMAGES is empty" in caplog.text
    assert "DOCKER_IMAGE" not in caplog.text


def test_a_yaml_set_goes_in_as_a_list():
    assert _flatten_yaml({"api": {"cors_origins": {"https://b.example", "https://a.example"}}}) == {
        "SUROGATES_API_CORS_ORIGINS": '["https://a.example", "https://b.example"]'}


def test_the_settings_of_the_tests_are_left_as_they_were(config_file):
    """The fixture's own check: the file's values leave the environment when a test ends."""
    import os

    config_file("sandbox:\n  allowed_images: [ghcr.io/acme/tasks:*]\n")
    assert os.environ["SUROGATES_SANDBOX_ALLOWED_IMAGES"] == '["ghcr.io/acme/tasks:*"]'


# -- the allow-list stays an allow-list ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "sandbox:\n  allowed_images: []\n",
    "sandbox:\n  allowed_images:\n",
    "sandbox:\n  allowed_images: ['']\n",
    "sandbox: {}\n",
])
def test_an_empty_allow_list_runs_no_image(config_file, text):
    from surogates.sandbox.session_image import SessionImageRefused, session_image_of

    settings = config_file(text)
    with pytest.raises(SessionImageRefused):
        session_image_of({"sandbox_image": "ghcr.io/acme/tasks:1", "sandbox_workdir": "/app"}, settings.sandbox)


@pytest.mark.parametrize("text", [
    "sandbox:\n  allowed_images: [1, 2]\n",
    "sandbox:\n  allowed_images: [{a: b}]\n",
    "sandbox:\n  allowed_images: ghcr.io/acme/tasks:*\n",
])
def test_a_malformed_allow_list_stops_the_settings(config_file, text):
    # Never read as empty-and-refusing silently, never as "all": the settings do not load.
    with pytest.raises(Exception) as caught:
        config_file(text)
    assert "allowed_images" in str(caught.value).lower()


def test_only_the_images_on_the_list_run(config_file):
    from surogates.sandbox.session_image import SessionImageRefused, session_image_of

    settings = config_file("sandbox:\n  allowed_images: [ghcr.io/acme/tasks:*]\n")
    assert session_image_of({"sandbox_image": "ghcr.io/acme/tasks:1", "sandbox_workdir": "/app"},
                            settings.sandbox).image == "ghcr.io/acme/tasks:1"
    with pytest.raises(SessionImageRefused):
        session_image_of({"sandbox_image": "ghcr.io/evil/x:1", "sandbox_workdir": "/app"}, settings.sandbox)


def test_every_list_and_mapping_setting_is_known():
    keys = config._collection_setting_keys()
    assert {"SUROGATES_API_CORS_ORIGINS", "SUROGATES_SANDBOX_ALLOWED_IMAGES",
            "SUROGATES_SANDBOX_SSH_EGRESS_BASELINE_CIDRS", "SUROGATES_LLM_MODELS",
            "SUROGATES_LLM_CHAT_TEMPLATE_KWARGS"} <= keys
    assert "SUROGATES_DB_URL" not in keys and "SUROGATES_SANDBOX" not in keys


def test_no_value_of_this_file_is_left_in_the_environment():
    """Runs after the others in this file: none of their config files left a setting behind."""
    import os

    from surogates.config import SandboxSettings

    assert "SUROGATES_SANDBOX_ALLOWED_IMAGES" not in os.environ
    assert SandboxSettings().allowed_images == []
