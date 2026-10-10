# What guest-history-tree.test.ts runs in the guest as its root, before the agent starts: two threads'
# landings on one folder through the tree the agent disk carries, one request a run as the agent
# runs them, on the guest's own python and git.  Each check says a line; the last says how it ended.
import json
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

TREE = Path("/run/surogate/agent/history")
ONE, TWO = "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f", "7d8e9f00-1a2b-4c3d-8e4f-5a6b7c8d9e0f"
YOURS = {"name": "u1", "email": "user:u1@surogate"}
A = {"name": "Draft A", "email": f"thread:{ONE}@surogate"}
B = {"name": "Draft B", "email": f"thread:{TWO}@surogate"}


def scenario(place: Path) -> None:
    folder, store = place / "Documents", place / "store"
    folder.mkdir()
    (folder / "Report.docx").write_text("report v1\n")
    (folder / "notes.txt").write_text("v1 notes\n")

    def ask(thread: str, action: str, **args: object) -> dict:
        request = {"store": str(store), "folder": str(folder), "thread": thread, "user": "u1", "action": action, "args": args}
        ran = subprocess.run(
            [sys.executable, "-I", str(TREE / "main.py")], input=json.dumps(request), capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin"}, cwd="/", timeout=120,
        )
        assert ran.returncode == 0, ran.stderr
        return json.loads(ran.stdout)

    def applied(thread: str, author: dict, saga: str) -> tuple[dict, dict]:
        picked = ask(thread, "pickup", author=YOURS, trailers=[["Surogate-Saga", saga], ["Surogate-Kind", "pickup"]])
        turn = ask(thread, "commit", author=author, trailers=[["Surogate-Saga", saga], ["Surogate-Kind", "turn"]], pickup=picked["commit"])
        for change in turn["changes"]:
            shutil.copyfile(store / "threads" / thread / change["path"], folder / change["path"])
        return turn, {
            "turn": turn["commit"], "applied": turn["changes"], "author": author,
            "trailers": [["Surogate-Saga", saga], ["Surogate-Kind", "landing"]], "main": picked["main"], "pickup": picked["commit"],
        }

    def files(of: Path) -> dict:
        return {str(p.relative_to(of)): p.read_text() for p in sorted(of.rglob("*")) if p.is_file()}

    assert ask(ONE, "prune") == {"error": {"code": "not_a_request", "message": "refused the request: it names no action this computer's history takes"}}
    assert ask(ONE, "open") == {"copy": "made"} == ask(TWO, "open")
    one, two = store / "threads" / ONE, store / "threads" / TWO
    assert files(one) == files(folder) and not (one / ".git").exists()
    print("each thread's copy is made")
    (one / "Report.docx").write_text("A's report\n")
    (one / "A-new.md").write_text("A's new file\n")
    (two / "Report.docx").write_text("B's report\n")
    (two / "B.md").write_text("B's own\n")
    assert ask(ONE, "changed") == {"paths": ["A-new.md", "Report.docx"]}

    turn, step = applied(ONE, A, "saga:1")
    assert [c["path"] for c in turn["changes"]] == ["A-new.md", "Report.docx"], turn
    assert ask(ONE, "forget", saga="saga:1", applied=step["applied"])["error"]["code"] == "landing_unsettled"
    landed = ask(ONE, "record", **step)
    assert landed["set_aside"] is None and ask(ONE, "forget", saga="saga:1", applied=step["applied"]) == {"landing": landed["commit"]}, landed
    print("the first thread's turn landed, and what it kept may be forgotten only then")

    turn, step = applied(TWO, B, "saga:2")
    assert [c["path"] for c in turn["changes"]] == ["B.md"], turn
    assert [(o["path"], o["reason"], o["by"]) for o in turn["overlapped"]] == [
        ("Report.docx", "changed", {"kind": "thread", "id": ONE, "title": "Draft A"}),
    ], turn
    # The second thread's record, cut right after its push: the cloud's half alone.
    sys.path.insert(0, str(TREE))
    from surogates.sandbox.history import History
    from surogates.sandbox.local_history import LocalHistory

    landing = History.record(LocalHistory.at(store, folder, thread=TWO, user="u1"), **step)["commit"]
    assert (two / "Report.docx").read_text() == "B's report\n"
    assert ask(TWO, "open") == {"copy": "moved"}
    assert files(two) == files(folder) == {
        "A-new.md": "A's new file\n", "B.md": "B's own\n", "Report.docx": "A's report\n", "notes.txt": "v1 notes\n",
    }
    assert ask(TWO, "changed") == {"paths": []}
    after = ask(TWO, "commit", author=B, trailers=[["Surogate-Saga", "saga:3"]], pickup=None)
    assert (after["commit"], after["changes"], after["overlapped"]) == (None, [], []), after
    assert ask(TWO, "record", **step) == {"commit": landing, "set_aside": None}
    print("a record cut after its push is finished at the thread's next open, and its next landing takes nothing of the other's")

    # A command still running writes the copy after the turn is committed: set aside before the record takes it away,
    # named by every open after, and no snapshot the copy is put back to.
    (one / "Report.docx").write_text("A's report, again\n")
    (two / "Report.docx").write_text("B's report, on A's first\n")
    assert ask(ONE, "open") == {"copy": "kept"}
    _, step = applied(ONE, A, "saga:4")
    assert ask(ONE, "record", **step)["set_aside"] is None
    turn, step = applied(TWO, B, "saga:5")
    (two / "late.md").write_text("written after the turn was committed\n")
    aside = ask(TWO, "record", **step)["set_aside"]
    assert not (two / "late.md").exists() and (two / "Report.docx").read_text() == "A's report, again\n"
    assert ask(TWO, "restore", commit=aside)["error"]["code"] == "not_on_base"
    assert not (two / "late.md").exists() and (two / "Report.docx").read_text() == "A's report, again\n"
    assert ask(TWO, "open") == {"copy": "moved", "set_asides": [aside]} == ask(TWO, "open")
    print("what a copy held beyond its turn is set aside, named at every open, and the copy is not put back to it")

    # A clean copy's move to main, cut after its files and before its base: the next act finishes it.
    (two / "B2.md").write_text("more of B's\n")
    _, step = applied(TWO, B, "saga:6")
    ask(TWO, "record", **step)
    (folder / "yours.txt").write_text("saved by you since\n")

    class Cut(Exception):
        pass

    def cut(self: History, old: str, new: str, moved: object = History._switch) -> str:
        moved(self, old, new)
        raise Cut

    moved_by, pushed_by = History._switch, History._push
    History._switch = cut
    try:
        LocalHistory.at(store, folder, thread=ONE, user="u1").open()
    except Cut:
        pass
    finally:
        History._switch = moved_by
    assert (one / "B2.md").exists() and (one / "yours.txt").exists()
    assert ask(ONE, "changed") == {"paths": []}
    assert ask(ONE, "open") == {"copy": "moved"} and files(one) == files(folder)
    print("a clean copy's move to main cut before its base moved is finished by the next act")

    def up_to_its_record(thread: str, author: dict, saga: str, left: tuple[str, ...]) -> dict:
        # A landing that leaves out what no landing writes: every other change applied, the host's way.
        picked = ask(thread, "pickup", author=YOURS, trailers=[["Surogate-Saga", saga], ["Surogate-Kind", "pickup"]])
        turn = ask(thread, "commit", author=author, trailers=[["Surogate-Saga", saga], ["Surogate-Kind", "turn"]], pickup=picked["commit"])
        applied = [change for change in turn["changes"] if change["path"] not in left]
        for change in applied:
            if change["after"] is None:
                (folder / change["path"]).unlink()
            else:
                shutil.copyfile(store / "threads" / thread / change["path"], folder / change["path"])
        return {
            "turn": turn["commit"], "applied": applied, "author": author, "left": list(left),
            "trailers": [["Surogate-Saga", saga], ["Surogate-Kind", "landing"]], "main": picked["main"], "pickup": picked["commit"],
        }

    def main_has() -> list[str]:
        listed = subprocess.run(
            ["git", f"--git-dir={store / 'history.git'}", "ls-tree", "-r", "-z", "--name-only", "refs/heads/main"],
            capture_output=True, text=True, check=True, env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
        )
        return sorted(name for name in listed.stdout.split("\0") if name)

    # A name that runs code, and a file the folder has under a second name: no landing writes either.
    (one / ".vscode").mkdir()
    (one / ".vscode" / "tasks.json").write_text("{}\n")
    (one / "notes.txt").unlink()
    (one / "C.md").write_text("A's third\n")
    step = up_to_its_record(ONE, A, "saga:7", (".vscode/tasks.json", "notes.txt"))
    assert [change["path"] for change in step["applied"]] == ["C.md"], step
    assert ask(ONE, "record", **step)["set_aside"] is None
    assert (one / ".vscode" / "tasks.json").read_text() == "{}\n" and not (one / "notes.txt").exists()
    assert (folder / "notes.txt").read_text() == "v1 notes\n" and not (folder / ".vscode").exists()
    assert "notes.txt" in main_has() and ".vscode/tasks.json" not in main_has()
    assert ask(ONE, "changed") == {"paths": [".vscode/tasks.json", "notes.txt"]} and ask(ONE, "open") == {"copy": "kept"}
    # Cut right after its push, the next open finishes the record from what the landing's own commit names.
    (two / ".vscode").mkdir()
    (two / ".vscode" / "launch.json").write_text("{}\n")
    (two / "D.md").write_text("B's fourth\n")
    step = up_to_its_record(TWO, B, "saga:8", (".vscode/launch.json",))

    def pushed_then_cut(self: History, updates: dict, *, expect: dict, push: object = History._push) -> None:
        push(self, updates, expect=expect)
        if "refs/heads/main" in updates:
            raise Cut

    History._push = pushed_then_cut
    try:
        LocalHistory.at(store, folder, thread=TWO, user="u1").record(**step)
    except Cut:
        pass
    finally:
        History._push = pushed_by
    assert ask(TWO, "open") == {"copy": "kept", "set_asides": [aside]}
    assert (two / ".vscode" / "launch.json").read_text() == "{}\n" and (two / "C.md").read_text() == "A's third\n"
    assert ask(TWO, "changed") == {"paths": [".vscode/launch.json"]} and ".vscode/launch.json" not in main_has()
    print("what a landing left out stays in the copy as the thread left it, by the guest's git, and so when its record is cut after its push")


def a_large_folder_left_out(place: Path) -> str:
    """A landing's commit and record, with the guest's git, over a copy that holds a node_modules of fifty
    thousand files, which history leaves out; then the next turn's deletion, which lands.  How long each took."""
    folder, store = place / "Documents", place / "store"
    folder.mkdir()
    for name in ("Report.docx", "notes.txt"):
        (folder / name).write_text(f"{name} v1\n")
    thread = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"

    def ask(action: str, **args: object) -> tuple[dict, float]:
        request = {"store": str(store), "folder": str(folder), "thread": thread, "user": "u1", "action": action, "args": args}
        begun = time.monotonic()
        ran = subprocess.run(
            [sys.executable, "-I", str(TREE / "main.py")], input=json.dumps(request), capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin"}, cwd="/", timeout=300,
        )
        assert ran.returncode == 0, ran.stderr
        return json.loads(ran.stdout), time.monotonic() - begun

    def landed(saga: str) -> tuple[dict, float, float]:
        picked, _ = ask("pickup", author=YOURS, trailers=[["Surogate-Saga", saga], ["Surogate-Kind", "pickup"]])
        turn, committed = ask("commit", author=A, trailers=[["Surogate-Saga", saga], ["Surogate-Kind", "turn"]], pickup=picked["commit"])
        for change in turn["changes"]:
            if change["after"] is None:
                (folder / change["path"]).unlink()
            else:
                shutil.copyfile(store / "threads" / thread / change["path"], folder / change["path"])
        _, recorded = ask(
            "record", turn=turn["commit"], applied=turn["changes"], author=A, main=picked["main"], pickup=picked["commit"],
            trailers=[["Surogate-Saga", saga], ["Surogate-Kind", "landing"]],
        )
        return turn, committed, recorded

    assert ask("open")[0] == {"copy": "made"}
    copy = store / "threads" / thread
    # As an install leaves it: five hundred packages of a hundred files each.
    for package in range(500):
        (copy / "node_modules" / f"pkg-{package:03d}").mkdir(parents=True)
        for n in range(100):
            (copy / "node_modules" / f"pkg-{package:03d}" / f"f{n:02d}.js").write_text(f"module.exports = {n};\n")
    (copy / "Report.docx").write_text("report v2\n")
    first, committed, recorded = landed("saga:big-1")
    assert [c["path"] for c in first["changes"]] == ["Report.docx"] and first["excluded"] == ["node_modules/"], first
    (copy / "notes.txt").unlink()
    second, committed_again, recorded_again = landed("saga:big-2")
    assert [(c["path"], c["after"]) for c in second["changes"]] == [("notes.txt", None)] and second["overlapped"] == [], second
    assert second["excluded"] == [] and not (folder / "notes.txt").exists()
    return (
        f"a landing over a copy that holds 50000 files history leaves out: commit {committed:.1f}s, record {recorded:.1f}s;"
        f" the next turn's deletion lands: commit {committed_again:.1f}s, record {recorded_again:.1f}s"
    )


place = Path(tempfile.mkdtemp(prefix="history-tree-", dir="/run"))
# On the sessions disk, ext4 as a place is on the computer's: fifty thousand files are too many for /run's memory.
large = Path(tempfile.mkdtemp(prefix="history-tree-", dir="/run/surogate/sessions"))
try:
    print(f"python {sys.version.split()[0]}, {subprocess.run(['git', '--version'], capture_output=True, text=True).stdout.strip()}")
    scenario(place)
    print(a_large_folder_left_out(large))
    print("passed")
except Exception:
    traceback.print_exc(file=sys.stdout)
    print("failed")
finally:
    shutil.rmtree(place, ignore_errors=True)
    shutil.rmtree(large, ignore_errors=True)
