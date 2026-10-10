# What guest-history-tree.test.ts runs in the guest as its root, before the agent starts: two threads'
# landings on one folder through the tree the agent disk carries, one request a run as the agent
# runs them, on the guest's own python and git.  Each check says a line; the last says how it ended.
import json
import shutil
import subprocess
import sys
import tempfile
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

    History._switch = cut
    try:
        LocalHistory.at(store, folder, thread=ONE, user="u1").open()
    except Cut:
        pass
    assert (one / "B2.md").exists() and (one / "yours.txt").exists()
    assert ask(ONE, "changed") == {"paths": []}
    assert ask(ONE, "open") == {"copy": "moved"} and files(one) == files(folder)
    print("a clean copy's move to main cut before its base moved is finished by the next act")


place = Path(tempfile.mkdtemp(prefix="history-tree-", dir="/run"))
try:
    print(f"python {sys.version.split()[0]}, {subprocess.run(['git', '--version'], capture_output=True, text=True).stdout.strip()}")
    scenario(place)
    print("passed")
except Exception:
    traceback.print_exc(file=sys.stdout)
    print("failed")
finally:
    shutil.rmtree(place, ignore_errors=True)
