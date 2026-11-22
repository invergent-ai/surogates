"""The cloud sandbox image's PowerShell: pinned, checked, and outside the desktop guest's key."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "images" / "sandbox" / "Dockerfile").read_text(encoding="utf-8")
BEFORE, SANDBOX = DOCKERFILE.split("FROM tools AS sandbox\n", 1)


def test_it_is_in_the_sandbox_stage_only():
    """The stages before it are hashed into the desktop guest's key (images/guest/inputs.sh)."""
    assert "powershell" not in BEFORE.lower()
    assert "/usr/local/bin/pwsh" in SANDBOX


def test_the_version_is_exact_and_each_arch_has_a_hash():
    assert re.search(r"^ARG POWERSHELL_VERSION=\d+\.\d+\.\d+$", SANDBOX, re.M)
    pins = re.findall(r"(amd64|arm64)\) ps_arch=(\w+); +ps_sha256=([0-9a-f]{64}) ;;", SANDBOX)
    assert [(deb, arch) for deb, arch, _ in pins] == [("amd64", "x64"), ("arm64", "arm64")]
    assert len({digest for _, _, digest in pins}) == 2


def test_the_archive_is_checked_before_it_is_unpacked():
    assert SANDBOX.index("sha256sum -c -") < SANDBOX.index("tar -xzf /tmp/powershell.tar.gz")


def test_the_files_are_roots_and_it_runs_before_the_image_is_accepted():
    assert "--no-same-owner" in SANDBOX
    assert "chmod -R u=rwX,go=rX /opt/microsoft/powershell" in SANDBOX
    assert "PSVersion.ToString()" in SANDBOX


def test_it_is_installed_before_the_image_drops_to_the_sandbox_user():
    assert SANDBOX.index("/usr/local/bin/pwsh") < SANDBOX.index("USER sandbox")


def test_its_first_start_leaves_the_sandbox_users_home_to_useradd():
    """The image sets XDG_CACHE_HOME under /home/sandbox: a cache written there as root makes useradd skip the home."""
    assert "HOME=/tmp/pwsh-build XDG_CACHE_HOME=/tmp/pwsh-build/.cache XDG_DATA_HOME=/tmp/pwsh-build/.local/share pwsh" in SANDBOX
    assert SANDBOX.index("test ! -e /home/sandbox") < SANDBOX.index("useradd --uid 1000")


def test_the_wrapper_sets_what_the_terminal_tools_filtered_environment_drops():
    """An image ENV does not reach a command: only the names in the filter's list do."""
    from surogates.tools.workspace_io.local import _ALWAYS_INHERIT

    wrapper = SANDBOX[SANDBOX.index("'#!/bin/sh'"):SANDBOX.index("> /usr/local/bin/pwsh")]
    assert "export POWERSHELL_TELEMETRY_OPTOUT=1 POWERSHELL_UPDATECHECK=Off" in wrapper
    # The sandbox user's only: another image user keeps its own modules under its own HOME.
    assert '[ "$(id -u)" != 1000 ] || export XDG_DATA_HOME="${XDG_DATA_HOME:-/home/sandbox/.local/share}"' in wrapper
    assert 'exec /opt/microsoft/powershell/7/pwsh "$@"' in wrapper
    assert not {"POWERSHELL_TELEMETRY_OPTOUT", "POWERSHELL_UPDATECHECK", "XDG_DATA_HOME"} & _ALWAYS_INHERIT
