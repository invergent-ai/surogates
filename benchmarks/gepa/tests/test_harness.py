"""A candidate must reach the worker as the only changed file, or not at all."""
import pathlib
import time

import pytest

from promptgepa.harness import (
    CandidateRejected, WorkerHarness, free_port, is_worker_argv, split_fragment,
)

SEED = """\
---
name: execution_discipline
description: Model-agnostic execution discipline
applies_when: model matches MODELS_REQUIRING_DISCIPLINE
---
# Execution discipline
<tool_persistence>
- Use tools whenever they improve correctness.
</tool_persistence>
"""


@pytest.fixture
def repo(tmp_path):
    prompts = tmp_path / "surogates" / "harness" / "prompts"
    (prompts / "guidance").mkdir(parents=True)
    (prompts / "identity").mkdir(parents=True)
    (prompts / "guidance" / "execution_discipline.md").write_text(SEED)
    (prompts / "guidance" / "other.md").write_text("---\nname: other\n---\nbody\n")
    (prompts / "identity" / "default.md").write_text("---\nname: d\n---\nbody\n")
    return tmp_path


def harness_for(repo, tmp_path):  # noqa: D103
    return WorkerHarness(
        repo_root=repo,
        fragment="guidance/execution_discipline",
        workdir=tmp_path / "work",
    )


def test_split_fragment_round_trips():
    header, body = split_fragment(SEED)
    assert header + body == SEED
    assert body.startswith("# Execution discipline")


def test_split_fragment_tolerates_no_frontmatter():
    assert split_fragment("just a body\n") == ("", "just a body\n")


def test_seed_body_excludes_frontmatter(repo, tmp_path):
    assert "name: execution_discipline" not in harness_for(repo, tmp_path).seed_body


def test_only_the_candidate_is_a_real_file(repo, tmp_path):
    h = harness_for(repo, tmp_path)
    tree = h.build_tree("# Execution discipline\nrewritten\n")

    target = tree / "guidance" / "execution_discipline.md"
    assert not target.is_symlink()
    assert "rewritten" in target.read_text()
    # Everything else is the shipped file, so nothing else can drift.
    assert (tree / "guidance" / "other.md").is_symlink()
    assert (tree / "identity" / "default.md").is_symlink()
    assert (tree / "guidance" / "other.md").read_text() == "---\nname: other\n---\nbody\n"


def test_frontmatter_is_reattached_verbatim(repo, tmp_path):
    h = harness_for(repo, tmp_path)
    tree = h.build_tree("# New\nbody only, no frontmatter\n")
    written = (tree / "guidance" / "execution_discipline.md").read_text()
    assert written.startswith("---\nname: execution_discipline\n")
    assert "applies_when: model matches" in written


def test_rebuilding_does_not_leave_the_previous_candidate(repo, tmp_path):
    h = harness_for(repo, tmp_path)
    h.build_tree("# One\nfirst\n")
    tree = h.build_tree("# Two\nsecond\n")
    text = (tree / "guidance" / "execution_discipline.md").read_text()
    assert "second" in text and "first" not in text


def test_empty_candidate_is_rejected(repo, tmp_path):
    with pytest.raises(CandidateRejected, match="empty"):
        harness_for(repo, tmp_path).build_tree("   \n")


def test_unknown_fragment_is_refused(repo, tmp_path):
    with pytest.raises(FileNotFoundError):
        WorkerHarness(
            repo_root=repo, fragment="guidance/nope", workdir=tmp_path / "w",
        )


# ---------------------------------------------------------------------------
# Worker detection
# ---------------------------------------------------------------------------


def test_detects_the_console_script_worker():
    assert is_worker_argv(["/work/surogates/.venv/bin/surogates", "worker"])


def test_detects_the_vscode_debugpy_worker():
    """The launch configs run the module form, wrapped in debugpy.

    Missing this shape is not cosmetic: a second worker shares the Redis
    queue and serves part of the batch with the shipped prompt, so every
    score in the run becomes meaningless without anything looking wrong.
    """
    assert is_worker_argv([
        "/work/surogates/.venv/bin/python", "-X", "frozen_modules=off",
        "/home/u/.vscode/extensions/ms-python.debugpy/debugpy/launcher",
        "--connect", "127.0.0.1:49265", "--adapter-access-token", "deadbeef",
        "-m", "surogates.cli.main", "worker",
    ])


@pytest.mark.parametrize("argv", [
    [],
    ["/usr/bin/python", "-m", "surogates.cli.main", "api"],
    ["/bin/bash", "-c", "echo starting the worker now"],
    ["celery", "worker"],
    ["/work/surogates/benchmarks/gepa/.venv/bin/promptgepa", "optimize",
     "--runs", "dev-021,dev-022"],
])
def test_does_not_flag_other_processes(argv):
    assert not is_worker_argv(argv)


def test_free_port_is_bindable():
    import socket
    port = free_port()
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


def test_each_harness_gets_its_own_health_port(repo, tmp_path):
    a = harness_for(repo, tmp_path / "a")
    b = harness_for(repo, tmp_path / "b")
    assert a.health_port != b.health_port


def test_worker_is_configured_to_die_with_the_optimiser():
    """PDEATHSIG is the backstop for a run that is killed, not stopped.

    Without it a SIGTERM'd optimiser leaves a worker consuming the queue
    with the candidate prompt of an experiment that no longer exists --
    which then silently serves the next run's sessions.
    """
    import inspect
    import subprocess

    from promptgepa import harness as mod

    source = inspect.getsource(mod.WorkerHarness.running)
    assert "preexec_fn=_die_with_parent" in source

    # The child really does get killed when its parent dies.
    parent = subprocess.Popen([
        "python3", "-c",
        "import subprocess,ctypes,signal,sys,time\n"
        "def d():\n"
        "    ctypes.CDLL('libc.so.6').prctl(1, signal.SIGKILL)\n"
        "p=subprocess.Popen(['sleep','60'], preexec_fn=d)\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(30)\n",
    ], stdout=subprocess.PIPE, text=True)
    child_pid = int(parent.stdout.readline().strip())
    parent.kill()
    parent.wait(timeout=10)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not pathlib.Path(f"/proc/{child_pid}").exists():
            return
        time.sleep(0.2)
    raise AssertionError(f"child {child_pid} outlived its parent")


def test_a_relative_workdir_becomes_absolute(repo, tmp_path, monkeypatch):
    """The tree path is read by a worker started with a different cwd.

    A relative SUROGATES_PROMPTS_ROOT resolves against the worker's cwd,
    not the optimiser's, and the worker dies on the first fragment it
    cannot find.
    """
    monkeypatch.chdir(tmp_path)
    h = WorkerHarness(
        repo_root=repo, fragment="guidance/execution_discipline",
        workdir=pathlib.Path("relative/work"),
    )
    assert h.workdir.is_absolute()
    tree = h.build_tree("# X\nbody\n")
    assert tree.is_absolute()
    assert (tree / "guidance" / "execution_discipline.md").exists()
