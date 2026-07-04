"""Terminal srt + env behavior for SSH-enabled sessions."""

from __future__ import annotations

import json
from types import SimpleNamespace

from surogates.tools.builtin import terminal

_TARGETS = json.dumps([
    {"alias": "deploy", "host": "deploy.example.com", "port": 22,
     "user": "ubuntu", "key_name": "prod"},
])


def _patch_settings_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(
        terminal, "load_settings",
        lambda: SimpleNamespace(sandbox=SimpleNamespace(srt_settings_dir=str(tmp_path))),
        raising=False,
    )


def test_ssh_enabled_allows_ssh_dir_and_hosts(tmp_path, monkeypatch):
    _patch_settings_dir(monkeypatch, tmp_path)
    monkeypatch.setenv("SUROGATES_SSH_TARGETS", _TARGETS)
    path = terminal._get_srt_settings_path("/workspace/x")
    settings = json.loads(open(path).read())
    assert "~/.ssh" not in settings["filesystem"]["denyRead"]
    assert "deploy.example.com" in settings["network"]["allowedDomains"]


def test_ssh_disabled_still_denies_ssh_dir(tmp_path, monkeypatch):
    _patch_settings_dir(monkeypatch, tmp_path)
    monkeypatch.delenv("SUROGATES_SSH_TARGETS", raising=False)
    path = terminal._get_srt_settings_path("/workspace/y")
    settings = json.loads(open(path).read())
    assert "~/.ssh" in settings["filesystem"]["denyRead"]
    assert "deploy.example.com" not in settings["network"]["allowedDomains"]


def test_ssh_state_changes_settings_hash(tmp_path, monkeypatch):
    _patch_settings_dir(monkeypatch, tmp_path)
    monkeypatch.delenv("SUROGATES_SSH_TARGETS", raising=False)
    off = terminal._get_srt_settings_path("/workspace/z")
    monkeypatch.setenv("SUROGATES_SSH_TARGETS", _TARGETS)
    on = terminal._get_srt_settings_path("/workspace/z")
    assert off != on  # distinct settings files → no stale allowlist reuse


def test_ssh_auth_sock_inherited():
    assert "SSH_AUTH_SOCK" in terminal._ALWAYS_INHERIT


def test_setup_ssh_home_writes_config(tmp_path, monkeypatch):
    import os
    import shutil

    ssh_dir = terminal._SSH_DIR
    shutil.rmtree(ssh_dir, ignore_errors=True)
    monkeypatch.setenv("SUROGATES_SSH_TARGETS", _TARGETS)
    monkeypatch.setenv("SUROGATES_SSH_KNOWN_HOSTS", "deploy.example.com ssh-ed25519 AAAA\n")
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    terminal._setup_ssh_home(env)

    config = os.path.join(ssh_dir, "config")
    known_hosts = os.path.join(ssh_dir, "known_hosts")
    ssh_wrapper = os.path.join(ssh_dir, "bin", "ssh")

    with open(config) as fh:
        cfg_text = fh.read()
    assert "Host deploy" in cfg_text
    # The config points UserKnownHostsFile at the absolute known_hosts path.
    assert f"UserKnownHostsFile {known_hosts}" in cfg_text
    with open(known_hosts) as fh:
        assert fh.read().strip().endswith("AAAA")

    # The ssh wrapper forces -F <config> and is executable.
    with open(ssh_wrapper) as fh:
        wrapper_text = fh.read()
    assert f"-F {config}" in wrapper_text
    assert wrapper_text.startswith("#!/bin/sh")
    assert os.access(ssh_wrapper, os.X_OK)

    # PATH is prefixed with the wrapper bin dir so plain `ssh` hits the wrapper.
    assert env["PATH"].startswith(os.path.join(ssh_dir, "bin") + ":")

    shutil.rmtree(ssh_dir, ignore_errors=True)


def test_setup_ssh_home_noop_when_disabled(tmp_path, monkeypatch):
    import os
    import shutil

    shutil.rmtree(terminal._SSH_DIR, ignore_errors=True)
    monkeypatch.delenv("SUROGATES_SSH_TARGETS", raising=False)
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    terminal._setup_ssh_home(env)
    assert not os.path.exists(os.path.join(terminal._SSH_DIR, "config"))
    assert env["PATH"] == "/usr/bin:/bin"
