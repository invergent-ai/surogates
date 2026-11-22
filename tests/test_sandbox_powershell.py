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


def test_it_does_not_phone_home():
    assert "POWERSHELL_TELEMETRY_OPTOUT=1" in SANDBOX
    assert "POWERSHELL_UPDATECHECK=Off" in SANDBOX
