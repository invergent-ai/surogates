"""The benchmark lives in a public repository: nothing private may be committed with it.

Endpoints, tokens, agent ids and the renderer come from the environment; results that name a machine, an agent or a
session stay in `runs/` (git-ignored). The patterns below are assembled from parts so this file does not match them.
"""
import os
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent

SKIPPED_DIRS = {".venv", "runs", "__pycache__", ".pytest_cache"}
SKIPPED_FILES = {".env"}
BINARY_SUFFIXES = {".pdf", ".pptx", ".docx", ".xlsx", ".png"}

ALLOWED_ADDRESSES = {"127.0.0.1", "0.0.0.0"}
IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
PRIVATE = {
    "a .local host": re.compile(r"\b[\w-]+\." + r"local\b"),
    "a storage endpoint": re.compile("cloudflare" + "storage", re.IGNORECASE),
    "a per-operator account name": re.compile("ops-chat-" + "[0-9a-f]", re.IGNORECASE),
    "a UUID": re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE),
}


def _committed_text_files() -> list[pathlib.Path]:
    files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIPPED_DIRS)
        for name in sorted(filenames):
            path = pathlib.Path(dirpath) / name
            if name in SKIPPED_FILES or path.suffix in BINARY_SUFFIXES or path.suffix == ".pyc":
                continue
            files.append(path)
    return files


def test_no_private_details():
    files = _committed_text_files()
    assert files, "found no files to scan"
    offenders = []
    for path in files:
        rel = path.relative_to(ROOT)
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # Only the location and what kind of detail: a public CI log must not repeat it.
            if any(address not in ALLOWED_ADDRESSES for address in IPV4.findall(line)):
                offenders.append(f"{rel}:{lineno}: an IPv4 address")
            offenders += [f"{rel}:{lineno}: {what}" for what, pattern in PRIVATE.items() if pattern.search(line)]
    assert offenders == [], "private details in committed files:\n" + "\n".join(offenders)


def test_no_progress_log():
    assert not (ROOT / "PROGRESS.md").exists(), "PROGRESS.md is a working log, not part of the benchmark"
