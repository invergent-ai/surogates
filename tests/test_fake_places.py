"""The tests' computer against the app it stands in for: its gate, and its land kind's rules beside the real file helper's.

The land kind's scenes run the app's own file helper (``desktop/dist/files/helper.js``),
asked one JSON line a request as a landing's host asks it, beside the rules of
``tests.fake_places`` on a twin of the same folder: after every step the two
answer alike, and leave their folders, and what they kept, alike.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from surogates.devices.binding import THREAD_ACTIONS
from tests.fake_places import ACTIONS, BUSY, NOT_A_FORGETTING_ASKED, NOT_A_THREAD, NOT_ITS_TURN, RECORDS_UNREAD, LandHelper, Places, landable

DESKTOP = Path(__file__).resolve().parents[1] / "desktop"
HELPER = DESKTOP / "dist" / "files" / "helper.js"
THREAD, HELPER_SESSION = str(uuid4()), str(uuid4())


def test_the_tests_computer_takes_the_actions_the_journal_asks_and_no_other():
    assert {kind: frozenset(actions) for kind, actions in ACTIONS.items()} == THREAD_ACTIONS


def frame(kind: str, action: Any, invocation: str, *, calling: str = THREAD, root: str = THREAD) -> dict[str, Any]:
    return {"session_id": root, "calling_session_id": calling, "invocation_id": invocation, "kind": kind, "args": {"action": action}}


def own(kind: str, action: str) -> str:
    if kind == "checkpoint":
        return "checkpoint:7:0:call_1"
    return "open:7" if (kind, action) == ("history", "open") else "land:7"


@pytest.mark.parametrize(("kind", "action"), [(kind, action) for kind in sorted(ACTIONS) for action in sorted(ACTIONS[kind])])
def test_the_tests_computer_refuses_a_threads_kind_as_the_apps_gate_does(tmp_path, kind, action):
    places = Places(tmp_path / "data", tmp_path / "Documents", "you")
    places.threads[THREAD] = THREAD
    # Asked as its turn asks it, it passes the gate, whatever the history then answers.
    places.run(frame(kind, action, own(kind, action)))
    assert places.asked == [(own(kind, action), THREAD, kind, action)]
    refused = ["17", "request:1", "bind", "land", "x:land:7", *sorted({"checkpoint:7:0:call_1", "open:7", "land:7"} - {own(kind, action)})]
    for invocation in refused:
        if (kind, action, invocation) == ("history", "open", "land:7"):
            continue
        assert places.run(frame(kind, action, invocation)) == NOT_ITS_TURN, invocation
    # A session under the thread asks its snapshots, and nothing else of the thread's.
    under = places.run(frame(kind, action, own(kind, action), calling=HELPER_SESSION))
    assert (under == NOT_ITS_TURN) == (kind != "checkpoint")
    assert places.run(frame(kind, "prune", own(kind, action))) == NOT_ITS_TURN
    # A chat bound to the folder itself has none of them.
    assert places.run(frame(kind, action, own(kind, action), root=str(uuid4()))) == NOT_A_THREAD


def test_the_tests_computer_holds_the_folder_through_a_settles_forgetting_and_takes_nothing_for_a_hold_given_back_unheld(tmp_path):
    # As the app's ToolHosts.land does: a turn holds the folder from its settle of a landing left running there to the end
    # of its own landing, and a hold given back by a turn that holds nothing starts no landing's host.
    folder = tmp_path / "Documents"
    folder.mkdir()
    (folder / "notes.txt").write_text("v1 notes\n")
    places = Places(tmp_path / "data", folder, "you")
    other = str(uuid4())
    places.threads.update({THREAD: THREAD, other: other})

    def land(action: str, invocation: str, root: str = THREAD, **args: Any) -> dict[str, Any]:
        return places.run({**frame("land", action, invocation, calling=root, root=root), "args": {"action": action, **args}})

    # Nothing held: the hold given back takes nothing, and nothing is asked of the folder's history.
    assert land("forget", "land:41:release:9", saga="hold:41", applied=[]) == {"ok": {}}
    assert places.holder is None
    assert not (tmp_path / "data" / "history").exists()
    assert "ok" in land("recover", "land:41:hold")
    # A landing another left running, settled by the turn and forgotten under the settle's name: still the turn's folder.
    assert land("forget", "land:41:settle:saga:left-running", saga="saga:left-running", applied=[]) == {"ok": {}}
    assert places.holder == THREAD
    assert land("revisions", "land:3", root=other, paths=[]) == BUSY
    # A hold given back by another while the turn holds the folder takes nothing from it.
    assert land("forget", "land:3:release:1", root=other, saga="hold:3", applied=[]) == {"ok": {}}
    assert places.holder == THREAD
    # Its own landing forgotten, the folder is let go.
    assert land("forget", "land:41", saga="saga:its-own", applied=[]) == {"ok": {}}
    assert places.holder is None
    assert "ok" in land("revisions", "land:3", root=other, paths=[])
    assert places.holder == other


def test_the_tests_computer_forgets_what_a_landing_kept_only_where_the_forgetting_names_every_step_its_helper_recorded(tmp_path):
    # As the app's ToolHosts.land does: the history's word first, then the helper's records against the steps named.
    folder = tmp_path / "Documents"
    folder.mkdir()
    (folder / "notes.txt").write_text("v1 notes\n")
    places = Places(tmp_path / "data", folder, "you")
    places.threads[THREAD] = THREAD

    def land(action: str, **args: Any) -> dict[str, Any]:
        return places.run({**frame("land", action, "land:41"), "args": {"action": action, **args}})

    assert places.run({**frame("history", "open", "open:41"), "args": {"action": "open"}})["ok"]["copy"] == "made"
    (places.copy(THREAD) / "notes.txt").write_text("v2 notes\n")
    revision = dict(land("revisions", paths=["notes.txt"])["ok"]["revisions"])["notes.txt"]
    before, after = blob(b"v1 notes\n"), blob(b"v2 notes\n")
    assert land("apply", saga="saga:1", step=0, path="notes.txt", before=before, after=after, expected=revision)["ok"]["path"] == "notes.txt"
    kept = places.kept / "saga:1"
    # A forgetting the app takes for none.
    for applied in ([{"path": "notes.txt", "before": before, "after": after}], [{"step": 0, "path": "", "before": before, "after": after}], None):
        assert land("forget", saga="saga:1", applied=applied) == NOT_A_FORGETTING_ASKED
    # The history holds no landing of the saga and finds each file as named, but the step the helper recorded is left out,
    # or named for another file: nothing goes.
    for applied in ([], [{"step": 0, "path": "other.txt", "before": None, "after": after}]):
        refused = land("forget", saga="saga:1", applied=applied)
        assert refused == {"error": {"type": "conflict", "message": (
            "Step 0 of this landing, of notes.txt, was applied on this computer, and the steps named to forget the landing leave it out, "
            "so nothing the landing kept was forgotten"
        )}}
        assert sorted(path.name for path in kept.iterdir()) == ["0", "0.json"]
    # Named as though what the landing wrote were what was there before: the history finds the file as named; the step
    # still keeps the file it replaced.
    assert land("forget", saga="saga:1", applied=[{"step": 0, "path": "notes.txt", "before": after, "after": after}]) == {"error": {"type": "conflict", "message": (
        "Step 0 of this landing, of notes.txt, was neither recorded nor put back on this computer, and keeps the file it replaced, "
        "so nothing the landing kept was forgotten"
    )}}
    # Records that cannot all be read forget nothing, however the steps are named.
    (kept / "1.json").write_text("not a record")
    assert land("forget", saga="saga:1", applied=[{"step": 0, "path": "notes.txt", "before": after, "after": after}]) == RECORDS_UNREAD
    (kept / "1.json").unlink()
    assert sorted(path.name for path in kept.iterdir()) == ["0", "0.json"]
    # Put back, it keeps nothing, and the landing may go.
    assert land("unapply", saga="saga:1", step=0, path="notes.txt") == {"ok": {"path": "notes.txt", "put_back": True}}
    assert land("forget", saga="saga:1", applied=[{"step": 0, "path": "notes.txt", "before": before, "after": after}]) == {"ok": {}}
    assert not kept.exists()
    assert (folder / "notes.txt").read_text() == "v1 notes\n"


# -- the land kind's rules, beside the app's own file helper ------------------------------------------------------

@pytest.fixture(scope="session")
def built_helper() -> Path:
    """The app's file helper, built from this checkout."""
    if shutil.which("npm") is None:
        pytest.fail("the file helper's cross-check needs npm")
    subprocess.run(["npm", "run", "build"], cwd=DESKTOP, check=True, capture_output=True)
    assert HELPER.exists(), f"the build left no {HELPER}"
    return HELPER


class Real:
    """A landing's helper as the app starts one, outside any sandbox: on the folder, given the thread's copy and where replaced files are kept."""

    def __init__(self, folder: Path, copy: Path, kept: Path) -> None:
        self.process = subprocess.Popen(
            [shutil.which("node"), str(HELPER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            env={"SUROGATE_FOLDER": str(folder), "HOME": str(folder.parent), "PATH": "/usr/bin:/bin", "SUROGATE_COPY": str(copy), "SUROGATE_KEPT": str(kept)},
        )
        assert json.loads(self.process.stdout.readline()) == {"ready": True}
        self.asked = 0

    def land(self, args: dict[str, Any]) -> dict[str, Any]:
        self.asked += 1
        self.process.stdin.write(json.dumps({"id": str(self.asked), "kind": "land", "args": args}) + "\n")
        self.process.stdin.flush()
        said = json.loads(self.process.stdout.readline())
        assert said["id"] == str(self.asked)
        return said["outcome"]

    def end(self) -> None:
        self.process.stdin.close()
        assert self.process.wait(10) == 0


class Twin:
    """One of the two folders a scene runs on: the user's files, the thread's copy, and where the folder's landings keep what they replace."""

    def __init__(self, base: Path) -> None:
        base.mkdir()
        self.folder, self.copy, self.kept = base / "Documents", base / "data" / "history" / "k" / "threads" / THREAD, base / "data" / "landings" / "k"
        for where in (self.folder, self.copy):
            (where / "Plans").mkdir(parents=True)
            (where / "Plans" / "Q3.md").write_text("Q3 plan\n")
            (where / "notes.txt").write_text("v1 notes\n")
        (self.folder / "Report.docx").write_bytes(b"PK report v1")
        os.chmod(self.folder / "Report.docx", 0o640)
        (self.folder / "old" / "deep").mkdir(parents=True)
        (self.folder / "old" / "deep" / "gone.md").write_text("gone\n")
        os.chmod(self.folder / "old", 0o750)
        (self.copy / "Report.docx").write_bytes(b"PK report v2")
        (self.copy / "Reports" / "2026").mkdir(parents=True)
        (self.copy / "Reports" / "2026" / "new.md").write_text("new\n")
        (self.copy / ".vscode").mkdir()
        (self.copy / ".vscode" / "tasks.json").write_text("{}\n")
        os.symlink("/etc/passwd", self.copy / "linked.txt")
        self.helper: Any = None

    def picture(self) -> dict[str, Any]:
        """The folder, entry by entry: kind, mode, size and bytes; and what is kept: each file's name, and a replaced file's mode and bytes."""
        seen: dict[str, Any] = {}
        for path in sorted(self.folder.rglob("*")):
            info = path.lstat()
            seen[f"folder/{path.relative_to(self.folder)}"] = (
                stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), info.st_size,
                path.read_bytes() if stat.S_ISREG(info.st_mode) else None,
            )
        for path in sorted(self.kept.rglob("*")) if self.kept.exists() else []:
            if path.is_file():
                seen[f"kept/{path.relative_to(self.kept)}"] = None if path.suffix == ".json" else (stat.S_IMODE(path.lstat().st_mode), path.read_bytes())
        return seen

    def revision(self, path: str) -> str:
        info = (self.folder / path).lstat()
        return f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}"


def blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def alike(outcome: dict[str, Any]) -> dict[str, Any]:
    """An outcome as two folders may share it: a look's revisions are each folder's own numbers."""
    revisions = outcome.get("ok", {}).get("revisions") if isinstance(outcome.get("ok"), dict) else None
    if revisions is None:
        return outcome
    return {"ok": {"revisions": [[path, token if token in ("absent", "other") else "a revision"] for path, token in revisions]}}


@pytest.mark.desktop
def test_the_tests_computers_land_rules_answer_and_leave_the_folder_as_the_apps_file_helper_does_step_by_step(tmp_path, built_helper):
    real, fake = Twin(tmp_path / "real"), Twin(tmp_path / "fake")
    saga, second = f"saga:{uuid4()}", f"saga:{uuid4()}"

    def hold() -> None:
        """A landing's host starts: a helper anew on each twin."""
        for twin in (real, fake):
            if isinstance(twin.helper, Real):
                twin.helper.end()
        real.helper = Real(real.folder, real.copy, real.kept)
        fake.helper = LandHelper(fake.folder, fake.copy, fake.kept)

    def both(step: str, args: Any) -> dict[str, Any]:
        """One step on each twin, its arguments made from that twin's own look: both answer it alike, and leave the same."""
        answers = [twin.helper.land(args(twin) if callable(args) else args) for twin in (real, fake)]
        assert alike(answers[0]) == alike(answers[1]), step
        assert real.picture() == fake.picture(), step
        return answers[1]

    def apply(step: int, path: str, before: bytes | None, after: bytes | None, *, expected: str | None = None, saga_id: str = saga):
        return lambda twin: {
            "action": "apply", "saga": saga_id, "step": step, "path": path,
            "before": None if before is None else blob(before), "after": None if after is None else blob(after),
            "expected": expected or (twin.revision(path) if os.path.lexists(twin.folder / path) else "absent"),
        }

    hold()
    assert both("recover", {"action": "recover"}) == {"ok": {"restored": [], "beside": [], "lost": [], "unread": []}}
    both("look", {"action": "revisions", "paths": ["Report.docx", "Reports/2026/new.md", "old/deep/gone.md", "Plans", "linked.txt", "nothing/here.md"]})
    # A file over the user's, and the same again as after an answer that was lost.
    assert both("replace", apply(0, "Report.docx", b"PK report v1", b"PK report v2")) == {
        "ok": {"path": "Report.docx", "before": blob(b"PK report v1"), "after": blob(b"PK report v2"), "made": []},
    }
    both("replace again", apply(0, "Report.docx", b"PK report v1", b"PK report v2", expected="absent"))
    assert both("a new file in new folders", apply(1, "Reports/2026/new.md", None, b"new\n"))["ok"]["made"] == ["Reports/2026", "Reports"]
    both("a deletion that empties its folders", apply(2, "old/deep/gone.md", b"gone\n", None))
    # What a landing does not write.
    assert both("a protected name", apply(3, ".vscode/tasks.json", None, b"{}\n"))["error"]["type"] == "sandbox"
    assert both("a link in the copy", apply(4, "linked.txt", None, b"x"))["error"]["type"] == "sandbox"
    assert both("a copy changed since its commit", apply(5, "Plans/Q3.md", b"Q3 plan\n", b"Q3 plan v2\n"))["error"]["type"] == "stale"
    assert both("a file changed since the look", apply(6, "notes.txt", b"v1 notes\n", b"x", expected="1:2:3:4:5"))["error"]["type"] == "conflict"
    assert both("a deletion of a file the copy still holds", apply(7, "notes.txt", b"v1 notes\n", None))["error"]["type"] == "stale"
    assert both("a step used for another file", apply(0, "notes.txt", b"v1 notes\n", b"x"))["error"]["type"] == "value"
    assert both("a change with no version on either side", apply(8, "notes.txt", None, None))["error"]["type"] == "value"
    assert both("what is no request", {"action": "apply", "saga": "../x", "step": 0, "path": "a"})["error"]["type"] == "value"
    # Put back: the new file and the folders made for it go; a step with no record says so.
    assert both("a new file put back", {"action": "unapply", "saga": saga, "step": 1, "path": "Reports/2026/new.md"}) == {
        "ok": {"path": "Reports/2026/new.md", "put_back": True},
    }
    assert both("a step that left no record", {"action": "unapply", "saga": saga, "step": 9, "path": "a.txt"}) == {
        "ok": {"path": "a.txt", "put_back": False},
    }
    deletion = {"action": "unapply", "saga": saga, "step": 2, "path": "old/deep/gone.md"}
    assert both("a deletion put back, its folders made again with their modes", deletion)["ok"]["put_back"]
    assert both("the forgetting", {"action": "forget", "saga": saga}) == {"ok": {}}
    # The next landing: a file you change after it was written is not put back, and what was kept stays kept.
    hold()
    both("recover", {"action": "recover"})
    for twin in (real, fake):
        (twin.copy / "Report.docx").write_bytes(b"PK report v3")
        (twin.copy / "notes.txt").write_text("v2 notes\n")
        (twin.copy / "Plans" / "Q3.md").write_text("Q3 plan v2\n")
    both("replace", apply(0, "Report.docx", b"PK report v2", b"PK report v3", saga_id=second))
    both("replace", apply(1, "notes.txt", b"v1 notes\n", b"v2 notes\n", saga_id=second))
    both("a third", apply(2, "Plans/Q3.md", b"Q3 plan\n", b"Q3 plan v2\n", saga_id=second))
    for twin in (real, fake):
        (twin.folder / "Plans" / "Q3.md").write_text("Q3 plan, by you\n")
    # Asked again as after a lost answer, a step whose file you changed since is not taken for done.
    again = apply(2, "Plans/Q3.md", b"Q3 plan\n", b"Q3 plan v2\n", expected="absent", saga_id=second)
    assert both("a step asked again over your change", again)["error"]["type"] == "conflict"
    assert both("a put-back over your change", {"action": "unapply", "saga": second, "step": 2, "path": "Plans/Q3.md"})["error"]["type"] == "conflict"
    # A record the disk lost cannot be read: the forgetting is refused, and so it is at the next helper's start.
    for twin in (real, fake):
        (twin.kept / second / "1.json").write_text("not a record")
    refused = both("a forgetting over a record that cannot be read", {"action": "forget", "saga": second})
    assert (refused["error"]["type"], refused["error"]["code"]) == ("os", "EIO")
    hold()
    assert both("recover", {"action": "recover"})["ok"]["unread"] == [[second, 1, None]]
    assert both("refused again", {"action": "forget", "saga": second})["error"]["code"] == "EIO"
    real.helper.end()


@pytest.mark.desktop
def test_the_tests_computer_protects_the_names_the_apps_landing_does(built_helper):
    paths = [
        "README.md", "a/b/c.txt", ".git", "a/.git", ".git/config", ".git/HEAD", ".git/hooks/pre-commit", "a/.git/config",
        ".git/modules/lib/config", ".git/modules/lib/hooks/x", ".git/modules/a/b/config", ".git/modules/a/b/x", ".git/refs/heads/hooks",
        ".git/rebase-merge/todo", ".git/worktrees/x/config", ".GIT/CONFIG", ".vscode/tasks.json", ".VSCode/x", "src/.idea/workspace.xml",
        ".claude/commands/x.md", ".claude/agents/y.md", ".claude/settings.json", "docs/.bashrc", ".mcp.json", "a/.ripgreprc", ".gitmodules",
        "node_modules/x/.vscode/settings.json", "node_modules/x/.gitconfig", "site-packages/.idea/x", "node_modules/x/.git/config",
        "dist-packages/a/.claude/commands/x", "/etc/passwd", "", "a//b", "a/./b", "../x", "x/..", "a\0b",
    ]
    said = subprocess.run(
        [shutil.which("node"), "--input-type=module", "-e",
         f"import {{ landable }} from {json.dumps(str(DESKTOP / 'dist' / 'files' / 'land.js'))};"
         "process.stdout.write(JSON.stringify(JSON.parse(process.argv[1]).map(landable)));", json.dumps(paths)],
        capture_output=True, text=True, check=True,
    )
    assert dict(zip(paths, json.loads(said.stdout))) == {path: landable(path) for path in paths}
