"""The history side of a laptop in a test: what Surogate Desktop does for a project's thread bound with a copy of its own.

A thread's root is bound with ``history: {thread}``, and the app says so in
its answer.  Its file and process operations then work in its copy of the
folder, which the folder's history makes, and three kinds are its own
(``surogates.devices.binding.THREAD_KINDS``): ``checkpoint`` and ``history``,
which the app passes to git in its guest, and ``land``, its file helper's.
Here the history is the module the guest runs, ``surogates.sandbox.local_history``,
on a place in a folder of the test's; the land kind is the file helper's rules
(``desktop/src/files/land.ts``) in Python, which ``tests/test_fake_places.py``
runs against the real helper, step by step.

What this stands in for that the app does not do yet, as it is to do it.
Each is checked first through the real app; a change of the app's that
settles one otherwise changes this file with it.

- The bind: the app's binder takes a bind's ``history`` only for the chat's
  own root, and answers it ``{"ok": {"history": {"thread": <root>}}}``: it
  keeps that copy.  An app that keeps none binds the folder itself and
  answers ``{"ok": null}``, as an app older than copies does (FakeLaptop
  with no *data*).  On master the binder answers ``{"ok": null}`` for both.
- The gate (the app's ``refusedOf``): a root not bound here is answered
  ``folder_unavailable``, one bound to the folder itself ``unsupported``; then
  the action is the kind's own, a ``checkpoint`` is asked under
  ``checkpoint:`` by any session of the thread's, a ``history`` or ``land``
  step by the thread itself under ``land:``, and an ``open`` also under
  ``open:``.  It reads no other argument.
- A thread's file and process operations work in its copy, named by the
  folder's path as a helper on a copy names them; a command's text has the
  folder's path read as the copy's, where the guest runs it in the copy
  mounted at that path.  The app opens the copy itself before the first of
  them, and on master's history that open moves a clean copy to ``main``.
- The hold: any ``land`` operation takes the folder, and another root's is
  answered ``busy`` at once, where the app waits up to 150 s first; it lapses
  after two idle minutes, and a landing's helper is started anew with each
  hold, which recovers first.  The forgetting of a landing the turn only
  settled, asked under ``land:<turn>:settle:…``, lets the folder go no more
  than the settle did; and a hold given back (``forget {saga: "hold:…"}``)
  by a root that holds nothing is answered ``{}``, nothing taken and nothing
  asked (desktop/src/hosts/tool-hosts.ts, ``ToolHosts.land``).
- ``land`` ``forget`` carries ``applied``, each apply that was sent with its
  step, and is ``value`` where it names none as the app takes one: the app
  asks the history's ``forget`` first, and answers with its refusal where it
  refuses, and with ``value`` where its answer is no forgetting's; then
  ``os``/``EIO`` where the helper's records of the saga cannot all be read
  (one that is none, a link in a record's or a folder's stead), and
  ``conflict`` where the helper holds a record of a step ``applied`` leaves
  out, or names for another file, or, where the history holds no landing of
  the saga, a step still keeps the file it replaced; only then is the
  helper's ``forget`` asked, and the folder let go.
- A folder with no history: the app's open answers ``{history: "off",
  reason}``, and every other operation of a thread's, its file operations
  among them, ``history_off``.  The thread works nowhere.
- A step the history refuses ``no_whole_copy`` is asked again once, after
  the app opens the copy; only a second refusal reaches the worker.
- The app's check of its guest's answers (``desktop/src/vm/history.ts``) is
  the server's own: what the history answers is passed on as it came, but for
  a commit's changes to a name whose change could run code on the computer,
  which it moves among ``overlapped`` with the reason ``protected``; and an
  open's names of what was set aside pass through unchanged.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from surogates.sandbox import local_history
from surogates.sandbox.history import HistoryError
from surogates.sandbox.local_history import NO_WHOLE_COPY

# As desktop/src/history/kinds.ts answers them.
NOT_A_THREAD = {"error": {
    "type": "unsupported", "message": "This chat works in its folder itself: it has no copy of it, no history and nothing to land",
}}
NOT_ITS_TURN = {"error": {
    "type": "refused", "message": "Only a thread's own turn may take its snapshots, move its history or land its work on this computer",
}}
FOLDER_UNAVAILABLE = {"error": {"type": "folder_unavailable", "message": "The folder for this chat is no longer available on this computer"}}
# As desktop/src/history/copies.ts answers every kind but a turn's open on a folder with no history.
HISTORY_OFF = {"error": {"type": "history_off", "message": "This folder has no history on this computer"}}
BUSY = {"error": {"type": "busy", "message": "Another chat is working in this folder on this computer"}}
# As desktop/src/history/kinds.ts answers a landing's forgetting that names no saga or applies it takes.
NOT_A_FORGETTING_ASKED = {"error": {
    "type": "value", "message": "A landing's forgetting names its saga, and each apply that was sent for it: its step, its file and that file's two versions",
}}
# As desktop/src/history/kinds.ts answers a forgetting whose records on the computer cannot all be read.
RECORDS_UNREAD = {"error": {
    "type": "os", "code": "EIO", "message": "This computer's records of this landing cannot all be read, so nothing the landing kept was forgotten",
}}
# As desktop/src/vm/history.ts answers an answer to a forgetting that is none.
NOT_A_FORGETTING = {"error": {
    "type": "value", "message": "This is no answer of a folder's history to forgetting a landing, so what the landing kept was not forgotten",
}}
# The actions of each of a thread's kinds the app takes.
ACTIONS = {
    "checkpoint": {"take", "restore"},
    "history": {"open", "changed", "fetch", "pickup", "commit", "record", "keep", "forget"},
    "land": {"recover", "revisions", "apply", "unapply", "forget"},
}
#: How long the folder is held for a landing with no step, as the app lets a landing's host idle.
IDLE_S = 120.0
#: A step a turn's landing asks for a landing it only settles, one another left running in the folder (the app's ``SETTLES``).
_SETTLES = re.compile(r"land:[^:]+:settle:")


def cannot(kind: str) -> dict[str, Any]:
    """What an app older than copies answers a kind it does not have (desktop/src/files/operations.ts)."""
    return {"error": {"type": "unsupported", "message": f"This computer cannot do '{kind}' yet"}}


class Places:
    """The app's data for one folder: its place, its threads' copies, and what its landings keep."""

    def __init__(self, data: Path, real: Path, user: str) -> None:
        self.real = real
        self.user = user
        key = hashlib.sha256(str(real).encode()).hexdigest()[:16]
        self.place = data / "history" / key
        self.kept = data / "landings" / key
        # Root session id -> the thread whose copy it works in: a bind's history.
        self.threads: dict[str, str] = {}
        # The thread kinds asked past the gate, as (invocation, calling session, kind, action), in order.
        self.asked: list[tuple[str, str, str, str]] = []
        # Why this folder has no history, as the app found it, or None.
        self.off: str | None = None
        # The root whose landing holds the folder, from its first land operation to its forgetting, and when it last asked.
        self.holder: str | None = None
        self.idle_s = IDLE_S
        self._held_at = 0.0
        # How a landing's host starts its helper on the folder, given the thread's copy and where replaced files are kept:
        # these rules, or the app's own helper in their place (tests.test_fake_places.Real).
        self.land_helper: Any = LandHelper
        self._helper: Any = None
        # The threads whose copy the app opened, in this run of it.
        self._opened: set[str] = set()

    def copy(self, thread: str) -> Path:
        return self.place / "threads" / thread

    # -- a thread's file and process operations ------------------------------

    def opened(self, root: str) -> Path | dict[str, Any]:
        """Where *root*'s file and process operations work: its copy, opened first; or the refusal they are answered with."""
        thread = self.threads[root]
        if self.off is None and (thread not in self._opened or not self.copy(thread).is_dir()):
            answer = self._ask(thread, "open", {})
            if "error" in answer:
                return answer
        if self.off is not None:
            return HISTORY_OFF
        return self.copy(thread)

    def into(self, root: str, args: dict[str, Any]) -> dict[str, Any]:
        """An operation's arguments, its folder's path read as its copy's, as a helper on a copy reads them (edge.ts)."""
        folder, copy = str(self.real), str(self.copy(self.threads[root]))
        moved = dict(args)
        for name in ("key", "workdir", "path"):
            value = moved.get(name)
            if isinstance(value, str) and (value == folder or value.startswith(f"{folder}/")):
                moved[name] = copy + value[len(folder):]
        if isinstance(moved.get("command"), str):
            moved["command"] = moved["command"].replace(folder, copy)
        return moved

    def out_of(self, root: str, outcome: Any) -> Any:
        """An operation's outcome, each path in it named by the folder's, as a helper on a copy answers."""
        folder, copy = str(self.real), str(self.copy(self.threads[root]))
        if isinstance(outcome, str):
            return outcome.replace(copy, folder)
        if isinstance(outcome, dict):
            return {key: self.out_of(root, value) for key, value in outcome.items()}
        if isinstance(outcome, list):
            return [self.out_of(root, value) for value in outcome]
        return outcome

    # -- a thread's own kinds -----------------------------------------------

    def run(self, frame: dict[str, Any]) -> dict[str, Any]:
        """One operation of a thread's own kinds, for a root bound here; its outcome, as the app's executor answers."""
        root, calling, invocation, kind = frame["session_id"], frame["calling_session_id"], frame["invocation_id"], frame["kind"]
        args = dict(frame["args"])
        action = args.pop("action", None)
        thread = self.threads.get(root)
        if thread is None:
            return NOT_A_THREAD
        if action not in ACTIONS[kind]:
            return NOT_ITS_TURN
        if kind == "checkpoint":
            if not invocation.startswith("checkpoint:"):
                return NOT_ITS_TURN
        elif calling != root or not (
            invocation.startswith("land:") or (kind == "history" and action == "open" and invocation.startswith("open:"))
        ):
            return NOT_ITS_TURN
        self.asked.append((invocation, calling, kind, action))
        if self.off is not None:
            return {"ok": {"history": "off", "reason": self.off}} if (kind, action) == ("history", "open") else HISTORY_OFF
        if kind == "land":
            return self._land(root, thread, action, args, invocation)
        if kind == "checkpoint":
            if action == "take":
                return self._ask(thread, "snapshot", {"reason": args.get("reason")})
            return self._ask(thread, "restore", {"commit": args.get("hash")})
        answer = self._ask(thread, action, args)
        if "ok" in answer and action == "open" and answer["ok"].get("history") == "off":
            self.off = answer["ok"]["reason"]
        return answer

    def _ask(self, thread: str, action: str, args: dict[str, Any]) -> dict[str, Any]:
        """One request of the folder's history for *thread*'s copy, as the guest answers and the app passes it on."""
        if action != "open" and thread not in self._opened:
            # The app's own open, before anything else is asked of a copy it has not opened.
            opened = self._ask(thread, "open", {})
            if "error" in opened:
                return opened
            if self.off is not None:
                return HISTORY_OFF
        answer = self._guest(thread, action, args)
        if action != "open" and answer.get("error", {}).get("code") == NO_WHOLE_COPY:
            # The app opens the copy again, and asks once more.
            opened = self._guest(thread, "open", {})
            answer = opened if "error" in opened else self._guest(thread, action, args)
        if action == "open":
            self._opened.add(thread)
            if answer.get("ok", {}).get("history") == "off":
                self.off = answer["ok"]["reason"]
        if action == "commit" and "ok" in answer:
            answer = {"ok": _protected_held(answer["ok"])}
        return answer

    def _guest(self, thread: str, action: str, args: dict[str, Any]) -> dict[str, Any]:
        """The history as the guest's agent runs it, its refusal by its code as the app passes one on."""
        try:
            return {"ok": local_history.run({
                "store": str(self.place), "folder": str(self.real), "thread": thread, "user": self.user, "action": action, "args": args,
            })}
        except HistoryError as refused:
            return {"error": {"type": "history", "code": refused.code, "message": str(refused)}}
        except (OSError, subprocess.TimeoutExpired, TypeError, ValueError, KeyError) as failed:
            # As the guest's main(): anything else that went wrong is the history's failure.
            return {"error": {"type": "history", "code": "failed", "message": str(failed)}}

    def _land(self, root: str, thread: str, action: str, args: dict[str, Any], invocation: str) -> dict[str, Any]:
        now = time.monotonic()
        holds = self.holder == root and self._helper is not None and now - self._held_at <= self.idle_s
        if action == "forget" and str(args.get("saga", "")).startswith("hold:") and not holds:
            # A hold given back where the root holds nothing: nothing to let go, and no landing's host started to say so.
            return {"ok": {}}
        if action == "forget" and not _forgetting(args):
            return NOT_A_FORGETTING_ASKED
        if self.holder not in (None, root) and now - self._held_at <= self.idle_s:
            return BUSY
        if not holds:
            # A landing's host, its helper started anew on the folder, with the thread's copy.
            self.let_go()
            self._helper = self.land_helper(self.real, self.copy(thread), self.kept)
        self.holder, self._held_at = root, now
        if action != "forget":
            return self._helper.land({"action": action, **args})
        # The history first: what a landing kept goes only where it was recorded, or put back whole.
        forgotten = self._ask(thread, "forget", {"saga": args.get("saga"), "applied": args.get("applied")})
        if "error" in forgotten:
            return forgotten
        ok = forgotten.get("ok")
        if not (isinstance(ok, dict) and ok.keys() == {"landing"} and (ok["landing"] is None or _id(ok["landing"]))):
            return NOT_A_FORGETTING
        if (unrecorded := _unrecorded(_Landing(self._helper, args["saga"]), args["applied"], recorded=ok["landing"] is not None)) is not None:
            return unrecorded
        outcome = self._helper.land({"action": "forget", "saga": args.get("saga")})
        # The turn's own landing over, the folder is let go, its helper with it; not at the forgetting of one it only settled.
        if "ok" in outcome and not _SETTLES.match(invocation):
            self.let_go()
            self.holder = None
        return outcome

    def let_go(self) -> None:
        """A landing's host stops: its helper with it, one the app started included."""
        helper, self._helper = self._helper, None
        if helper is not None and hasattr(helper, "end"):
            helper.end()


def _forgetting(args: dict[str, Any]) -> bool:
    """Whether a landing's forgetting names a saga and every apply sent for it as the app takes them: a step once, a file, two versions."""
    saga, applied = args.get("saga"), args.get("applied")
    if not (isinstance(saga, str) and _SAGA.fullmatch(saga) and isinstance(applied, list) and len(applied) <= 50_000):
        return False
    steps = [entry.get("step") if isinstance(entry, dict) else None for entry in applied]
    return len(set(steps)) == len(steps) and all(
        type(step) is int and 0 <= step <= 2**53 - 1
        and isinstance(path := entry.get("path"), str) and 0 < len(path) <= 4_096 and "\0" not in path
        and all(entry.get(side) is None or (isinstance(entry.get(side), str) and _ID.fullmatch(entry[side])) for side in ("before", "after"))
        and {"before", "after"} <= entry.keys()
        for step, entry in zip(steps, applied, strict=True)
    )


def _unrecorded(landing: _Landing, applied: list[dict[str, Any]], *, recorded: bool) -> dict[str, Any] | None:
    """The app's refusal of a forgetting by the helper's own records of the landing, as the app reads them.

    Records that cannot all be read; a step it holds a record of that
    *applied* leaves out or names for another file; and, for a landing the
    history does not hold (*recorded* false), a step that still keeps the
    file it replaced.
    """
    if any(path.is_symlink() or (path.exists() and not path.is_dir()) for path in (landing.store, landing.kept)):
        return RECORDS_UNREAD
    named = {entry["step"]: entry["path"] for entry in applied}
    records: dict[int, str] = {}
    try:
        steps, names = landing.steps(), os.listdir(landing.kept) if landing.kept.is_dir() else []
    except OSError:
        return RECORDS_UNREAD
    for step in sorted(steps):
        if (landing.kept / f"{step}.json").is_symlink():
            return RECORDS_UNREAD
        try:
            record = landing.read(step, as_the_app=True)
        except _Unreadable:
            return RECORDS_UNREAD
        if record is None:
            continue
        records[step] = path = record["path"]
        if named.get(step) != path:
            return {"error": {"type": "conflict", "message": (
                f"Step {step} of this landing, of {path}, was applied on this computer, and the steps named to forget the landing leave it out, "
                "so nothing the landing kept was forgotten"
            )}}
    keeping = sorted(int(name) for name in names if _KEPT.fullmatch(name))
    if recorded or not keeping:
        return None
    of = f", of {records[keeping[0]]}," if keeping[0] in records else ""
    return {"error": {"type": "conflict", "message": (
        f"Step {keeping[0]} of this landing{of} was neither recorded nor put back on this computer, "
        "and keeps the file it replaced, so nothing the landing kept was forgotten"
    )}}


def _protected_held(answer: dict[str, Any]) -> dict[str, Any]:
    """A commit's answer as the app passes it on: a change it may not land is left out, said ``protected``."""
    held = [{**change, "reason": "protected"} for change in answer["changes"] if not landable(change["path"])]
    if not held:
        return answer
    changes = [change for change in answer["changes"] if landable(change["path"])]
    return {**answer, "changes": changes, "overlapped": sorted([*answer["overlapped"], *held], key=lambda entry: entry["path"])}


# -- what a landing may not write (desktop/src/files/protect.ts) --------------------------------------------------

_PROTECTED_NAMES = {".gitconfig", ".gitmodules", ".bashrc", ".bash_profile", ".zshrc", ".zprofile", ".profile", ".ripgreprc", ".mcp.json", ".vscode", ".idea"}
_GIT_NAMES = {".gitconfig", ".gitmodules"}
_DEPENDENCY_FOLDERS = {"node_modules", "site-packages", "dist-packages"}
_PROTECTED_PAIRS = ((".claude", "commands"), (".claude", "agents"), (".git", "hooks"), (".git", "config"))
_GIT_CONFIGS = {"config", "config.worktree", "commondir"}
_GIT_STATE = {"worktrees", "rebase-merge", "rebase-apply", "sequencer"}


def protected(path: str) -> bool:
    """Whether *path*, a file from the folder's top, is a name whose change could run code on the computer."""
    parts = path.lower().split("/")
    if parts[-1] == ".git":
        return True
    for index, part in enumerate(parts):
        counts = part == ".git" or part in _GIT_NAMES or not any(above in _DEPENDENCY_FOLDERS for above in parts[:index])
        paired = any(part == first and parts[index + 1:index + 2] == [second] for first, second in _PROTECTED_PAIRS)
        if (counts and (part in _PROTECTED_NAMES or paired)) or (part == ".git" and _runs_code(parts[index + 1:])):
            return True
    return False


def _runs_code(rest: list[str]) -> bool:
    if not rest:
        return False
    first, after = rest[0], rest[1:]
    if first == "modules" and len(after) > 1:
        below = after[1:]
        return any(part == "hooks" or part in _GIT_STATE for part in below) or below[-1] in _GIT_CONFIGS
    return first in _GIT_STATE or first == "hooks" or (len(rest) == 1 and first in _GIT_CONFIGS)


def landable(path: str) -> bool:
    """Whether a landing may write the file git names *path*: a path inside the folder, and no protected name."""
    try:
        _parts(path)
    except _Refused:
        return False
    return not protected(path)


# -- the file helper's land kind, by its rules (desktop/src/files/land.ts) ----------------------------------------

_MAX_LOOKED = 2_000
_SAGA = re.compile(r"[A-Za-z0-9][A-Za-z0-9_:.-]{0,127}")
_REVISION = re.compile(r"[0-9]+:[0-9]+:[0-9]+:-?[0-9]+:-?[0-9]+")
_ID = re.compile(r"[0-9a-f]{40}")
_STEP = re.compile(r"(0|[1-9][0-9]*)\.json")
_KEPT = re.compile(r"0|[1-9][0-9]*")
_MAX_LAND_BYTES = 1 << 30
_MAX_KEPT_BYTES = 4 << 30
_HOLD = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
_FORGOTTEN = ".forgotten-"
_BAD = "land takes revisions of paths, an apply or an unapply of a saga's step on a path, the forgetting of a saga, or a recovery"
_USED = "land's step was already used for another file of this saga"


class _Refused(Exception):
    def __init__(self, kind: str, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.refusal = {"type": kind, **({"code": code} if code else {}), "message": message}


def _id(value: Any) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _value(message: str = _BAD) -> _Refused:
    return _Refused("value", message)


def _sandbox(message: str) -> _Refused:
    return _Refused("sandbox", message)


def _os(code: str, path: str, words: str | None = None) -> _Refused:
    # As an OSError's str() words it, without its number: Python's repr of the path, as the helper spells it.
    return _Refused("os", f"{words or os.strerror(getattr(errno, code))}: {path!r}", code)


def _conflict(path: str) -> _Refused:
    return _Refused("conflict", f"{path} changed in the folder while this landing ran, so it was not replaced")


def _parts(path: Any) -> list[str]:
    if not isinstance(path, str) or not path or "\0" in path or path.startswith("/"):
        raise _value()
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise _value()
    return parts


def _identity(info: os.stat_result | None) -> list[int] | None:
    return None if info is None else [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def _plain(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1


def _token(info: os.stat_result | None) -> str:
    if info is None:
        return "absent"
    return f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}" if _plain(info) else "other"


def _alike(info: os.stat_result | None, identity: list[int] | None) -> bool:
    """The same file, or its copy brought back from another filesystem: its size, and its time to the microsecond."""
    if info is None or identity is None:
        return info is None and identity is None
    return _identity(info) == identity or (info.st_size == identity[2] and info.st_mtime_ns // 1000 == identity[3] // 1000)


def _own_file() -> str:
    return f".surogate-{uuid.uuid4()}.tmp"


def _into(dir_fd: int, name: str, path: str) -> int | None:
    """The folder *name* in *dir_fd*, held: None where there is none; refused where it is no folder, a link least of all."""
    try:
        return os.open(name, _HOLD, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in (errno.ENOTDIR, errno.ELOOP):
            raise _sandbox(f"Not a path in this folder: '{path}'") from None
        raise _os(errno.errorcode[exc.errno], path) from None


def _enter(start: int, names: list[str], path: str, *, make: bool = False, modes: dict[str, int] | None = None) -> tuple[int | None, int, list[int]]:
    """Where *names* lead from *start*, which is let go: the last folder, held, or None with how many are there; and each one's mode."""
    at, seen = start, []
    try:
        for depth, name in enumerate(names):
            following = _into(at, name, path)
            if following is None:
                if not make:
                    os.close(at)
                    return None, depth, seen
                try:
                    os.mkdir(name, dir_fd=at)
                except FileExistsError:
                    pass
                following = _into(at, name, path)
                if following is None:
                    raise _os("ENOENT", path)
                if modes is not None and "/".join(names[:depth + 1]) in modes:
                    os.chmod(f"/proc/self/fd/{following}", modes["/".join(names[:depth + 1])])
            os.close(at)
            at = following
            seen.append(stat.S_IMODE(os.fstat(at).st_mode))
        return at, len(names), seen
    except BaseException:
        os.close(at)
        raise


def _look(dir_fd: int | None, name: str) -> os.stat_result | None:
    if dir_fd is None:
        return None
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


class LandHelper:
    """A landing's file helper on one folder, given the thread's copy and where the folder's landings keep what they replace.

    It recovers once, at its first ask: what an earlier helper left of a step
    is put back.  A step here is never cut short, so all a later helper finds
    is a record that cannot be read: it is said, and left.
    """

    def __init__(self, folder: Path, copy: Path, store: Path) -> None:
        self.folder, self.copy, self.store = folder, copy, store
        self._recovered: dict[str, list] | None = None

    def land(self, args: dict[str, Any]) -> dict[str, Any]:
        """One ``land`` operation's outcome, as the helper answers it: never raised."""
        try:
            return {"ok": self._land(args)}
        except _Refused as refused:
            return {"error": refused.refusal}
        except OSError as failed:
            code = errno.errorcode.get(failed.errno or 0, "EIO")
            return {"error": _os(code, str(args.get("path", "")), failed.strerror).refusal}

    def _land(self, args: dict[str, Any]) -> Any:
        action = args.get("action")
        report = self.recover()
        if action == "recover":
            return report
        if action == "revisions":
            paths = args.get("paths")
            if not isinstance(paths, list) or len(paths) > _MAX_LOOKED:
                raise _value()
            looked = [[one, self._revision(_parts(one))] for one in paths]
            named: dict[str, set[str]] = {}
            for one, token in looked:
                named.setdefault(token, set()).add(one)
            return {"revisions": [[one, "other" if _REVISION.fullmatch(token) and len(named[token]) > 1 else token] for one, token in looked]}
        saga = args.get("saga")
        if not isinstance(saga, str) or _SAGA.fullmatch(saga) is None:
            raise _value()
        landing = _Landing(self, saga)
        if action == "forget":
            return landing.forget()
        step, path = args.get("step"), args.get("path")
        if type(step) is not int or not 0 <= step <= 2**53 - 1 or not isinstance(path, str):
            raise _value()
        if action == "unapply":
            return landing.unapply(step, path)
        before, after, expected = args.get("before"), args.get("after"), args.get("expected")
        looked = isinstance(expected, str) and (expected == "absent" or _REVISION.fullmatch(expected) is not None)
        if action != "apply" or not all(one is None or _id(one) for one in (before, after)) or before is after is None or not looked:
            raise _value()
        return landing.apply(step, path, before, after, expected)

    def recover(self) -> dict[str, list]:
        """What an earlier helper's landings left in this folder: a record that cannot be read is said; once for a helper."""
        if self._recovered is not None:
            return self._recovered
        report: dict[str, list] = {"restored": [], "beside": [], "lost": [], "unread": []}
        names = sorted(os.listdir(self.store)) if self.store.is_dir() else []
        for saga in names:
            if saga.startswith(_FORGOTTEN):
                shutil.rmtree(self.store / saga, ignore_errors=True)
            if _SAGA.fullmatch(saga) is None:
                continue
            landing = _Landing(self, saga)
            for step in landing.steps():
                try:
                    landing.read(step)
                except _Unreadable as unread:
                    report["unread"].append([saga, step, unread.path])
            for name in os.listdir(landing.kept) if landing.kept.is_dir() else []:
                if name.endswith(".json.new"):
                    os.unlink(landing.kept / name)
            landing.rmdir()
        report["unread"].sort(key=lambda one: (one[0], one[1]))
        self._recovered = report
        return report

    def _revision(self, parts: list[str]) -> str:
        """What the look answers for the real file at *parts*: nothing through a link, which is ``other`` as any file a landing does not replace is."""
        held = None
        try:
            held, _, _ = _enter(os.open(self.folder, _HOLD), parts[:-1], "/".join(parts))
            return "absent" if held is None else _token(_look(held, parts[-1]))
        except (OSError, _Refused):
            return "other"
        finally:
            if held is not None:
                os.close(held)


class _Unreadable(_Refused):
    def __init__(self, saga: str, step: int, path: str | None) -> None:
        super().__init__("os", f"The record of step {step} of landing {saga} cannot be read, so nothing is done over it", "EIO")
        self.path = path


# A step's record as these rules write one; and as the app's helper writes one, which says more of where the step got.
_RECORD = {"path", "was", "wrote", "mode", "made", "above"}
_APPS_RECORD = _RECORD | {"temp", "aside", "moved", "out", "back"}


class _Landing:
    """One saga's steps in the folder, and what they kept: ``<kept>/<saga>/<step>.json``, and the file a step replaced at ``<step>``."""

    def __init__(self, helper: LandHelper, saga: str) -> None:
        self.folder, self.copy, self.store = helper.folder, helper.copy, helper.store
        self.saga = saga
        self.kept = helper.store / saga

    def steps(self) -> list[int]:
        names = os.listdir(self.kept) if self.kept.is_dir() else []
        return [int(match.group(1)) for name in names if (match := _STEP.fullmatch(name))]

    def read(self, step: int, *, as_the_app: bool = False) -> dict[str, Any] | None:
        """A step's record as these rules write one, or None where it has none; *as_the_app*, the app's helper's too, as the app reads either (files/land.ts, recordsOf)."""
        try:
            text = (self.kept / f"{step}.json").read_text()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError):
            raise _Unreadable(self.saga, step, None) from None
        try:
            record = json.loads(text)
        except ValueError:
            record = None
        if isinstance(record, dict) and (record.keys() == _RECORD or as_the_app and record.keys() == _APPS_RECORD) and landable(record["path"]):
            return record
        named = record.get("path") if isinstance(record, dict) else None
        raise _Unreadable(self.saga, step, named if isinstance(named, str) and landable(named) else None)

    def _write(self, step: int, record: dict[str, Any]) -> None:
        self.kept.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.kept / f"{step}.json").write_text(json.dumps(record))

    def _drop(self, step: int) -> None:
        for name in (str(step), f"{step}.json"):
            (self.kept / name).unlink(missing_ok=True)
        self.rmdir()

    def rmdir(self) -> None:
        try:
            self.kept.rmdir()
        except OSError:
            pass

    def _root(self, path: str) -> int:
        try:
            return os.open(self.folder, _HOLD)
        except OSError as failed:
            raise _os(errno.errorcode[failed.errno], path) from None

    def _kept_bytes(self) -> int:
        total = 0
        for saga in os.listdir(self.store) if self.store.is_dir() else []:
            if _SAGA.fullmatch(saga) and (self.store / saga).is_dir():
                total += sum((self.store / saga / name).lstat().st_size for name in os.listdir(self.store / saga) if name.isdigit())
        return total

    def _empty(self, folders: list[str]) -> None:
        """The folders *folders*, deepest first, go while they are empty, and no further up than one that is not."""
        for folder in folders:
            parts = folder.split("/")
            held = None
            try:
                held, _, _ = _enter(self._root(folder), parts[:-1], folder)
                if held is None:
                    return
                os.rmdir(parts[-1], dir_fd=held)
            except (OSError, _Refused):
                return
            finally:
                if held is not None:
                    os.close(held)

    def _twice(self, found: os.stat_result | None, path: str) -> _Refused | None:
        """The refusal of a file this landing wrote already under another of its names, in a folder that tells names apart less than git does."""
        if found is None:
            return None
        for step in self.steps():
            try:
                did = self.read(step)
            except _Unreadable:
                continue
            if did is not None and did["path"] != path and did["wrote"] is not None and _identity(found) == did["wrote"]:
                return _Refused("os", (
                    f"{path} and {did['path']} are one file in this folder, which tells names apart less than the thread's copy does, "
                    "so it was not written twice"
                ), "EEXIST")
        return None

    def apply(self, step: int, path: str, before: str | None, after: str | None, expected: str) -> dict[str, Any]:
        parts = _parts(path)
        name, folders = parts[-1], parts[:-1]
        if protected(path):
            raise _sandbox(f"Write denied: '{path}' is protected in this folder: a change to it could run code outside the sandbox.")
        try:
            copy = os.open(self.copy, _HOLD)
        except OSError:
            raise _sandbox("This thread's copy is not a folder of the app's own") from None
        theirs, _, _ = _enter(copy, folders, path)
        way, depth, modes = (None, 0, [])
        source = None
        try:
            way, depth, modes = _enter(self._root(path), folders, path)
            found = _look(way, name)
            done = {"path": path, "before": before, "after": after}
            earlier = self.read(step)
            if earlier is not None:
                if earlier["path"] != path:
                    raise _value(_USED)
                if _identity(found) == earlier["wrote"]:
                    return {**done, "made": earlier["made"]}
                raise _conflict(path)
            if _token(found) != expected:
                raise self._twice(found, path) or _conflict(path)
            if after is None and found is None:
                return {**done, "made": []}
            if after is None and _look(theirs, name) is not None:
                raise _Refused("stale", f"{path} is still in the thread's copy, so it was not deleted from the folder")
            if after is not None:
                source = self._source(theirs, name, path)
            if found is not None and self._kept_bytes() + found.st_size > _MAX_KEPT_BYTES:
                raise _Refused("os", (
                    f"{path} was not replaced: more than {_MAX_KEPT_BYTES >> 30} GiB would be kept of the files this folder's landings replaced"
                ), "EDQUOT")
            made = ["/".join(folders[:at + 1]) for at in range(depth, len(folders))][::-1]
            record = {
                "path": path, "was": _identity(found), "wrote": None, "mode": None if found is None else stat.S_IMODE(found.st_mode),
                "made": made, "above": [["/".join(folders[:at + 1]), mode] for at, mode in enumerate(modes)] if after is None else [],
            }
            if way is None:
                way, _, _ = _enter(self._root(path), folders, path, make=True)
            temp = aside = None
            try:
                if source is not None:
                    staged = _own_file()
                    self._stage(source, way, staged, path, after, record["mode"])
                    temp = staged
                if found is not None:
                    aside = _own_file()
                    os.rename(name, aside, src_dir_fd=way, dst_dir_fd=way)
                    moved = _look(way, aside)
                    if moved is None or not _plain(moved) or _token(moved).split(":")[:4] != expected.split(":")[:4]:
                        raise _conflict(path)
                if temp is not None:
                    try:
                        os.link(temp, name, src_dir_fd=way, dst_dir_fd=way, follow_symlinks=False)
                    except FileExistsError:
                        raise _conflict(path) from None
                    os.unlink(temp, dir_fd=way)
                    temp = None
                if aside is not None:
                    self.kept.mkdir(parents=True, exist_ok=True, mode=0o700)
                    os.rename(aside, self.kept / str(step), src_dir_fd=way)
                    os.chmod(self.kept / str(step), 0o600)
                    aside = None
                self._write(step, {**record, "wrote": _identity(_look(way, name)) if after is not None else None})
            except BaseException:
                # Undone: the real file at its name again, and nothing of the step left.
                if aside is not None:
                    os.rename(aside, name, src_dir_fd=way, dst_dir_fd=way)
                if temp is not None:
                    os.unlink(temp, dir_fd=way)
                self.rmdir()
                self._empty(made)
                raise
            if after is None:
                self._empty(["/".join(folders[:at]) for at in range(len(folders), 0, -1)])
            return {**done, "made": made}
        finally:
            for held in (theirs, way, source):
                if held is not None:
                    os.close(held)

    def _source(self, theirs: int | None, name: str, path: str) -> int:
        """The copy's file *name*, opened to read: only a plain file of the copy's, and no link at its name."""
        if theirs is None:
            raise _os("ENOENT", path)
        try:
            source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=theirs)
        except OSError as failed:
            if failed.errno == errno.ELOOP:
                raise _sandbox(f"Not a path in this folder: '{path}'") from None
            raise _os(errno.errorcode[failed.errno], path) from None
        info = os.fstat(source)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_LAND_BYTES:
            os.close(source)
            if stat.S_ISREG(info.st_mode):
                raise _Refused("os", f"File too large to land in a local folder (over {_MAX_LAND_BYTES >> 30} GiB)", "EFBIG")
            raise _sandbox(f"Not a path in this folder: '{path}'")
        return source

    def _stage(self, source: int, way: int, temp: str, path: str, after: str, mode: int | None) -> None:
        """The copy's file written at *temp* beside the real one, only where its bytes are the blob *after*."""
        size = os.fstat(source).st_size
        to = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666, dir_fd=way)
        try:
            blob, read = hashlib.sha1(b"blob %d\0" % size), 0
            while (piece := os.read(source, 1 << 20)) and read <= size:
                read += len(piece)
                blob.update(piece)
                os.write(to, piece)
            if read != size or blob.hexdigest() != after:
                raise _Refused("stale", f"{path} changed in the thread's copy after its turn was committed, so it was not landed")
            if mode is not None:
                os.fchmod(to, mode)
            os.fsync(to)
        except BaseException:
            os.unlink(temp, dir_fd=way)
            raise
        finally:
            os.close(to)

    def unapply(self, step: int, path: str) -> dict[str, Any]:
        parts = _parts(path)
        did = self.read(step)
        if did is None:
            return {"path": path, "put_back": False}
        if did["path"] != path:
            raise _value()
        if protected(path):
            raise _sandbox(f"Write denied: '{path}' is protected in this folder: a change to it could run code outside the sandbox.")
        if self._put_back(step, did, parts) == "changed":
            # The step ends as one whose file changed since: what it kept stays kept, for whoever settles the landing.
            if not (self.kept / str(step)).exists():
                self._drop(step)
                if did["was"] is None:
                    self._empty(did["made"])
            raise _Refused("conflict", f"{path} changed after the landing wrote it, so it was not put back")
        return {"path": path, "put_back": True}

    def _put_back(self, step: int, did: dict[str, Any], parts: list[str]) -> str:
        """The real file at its name again: ``restored``, or ``back`` where it never left it; ``changed`` where the name holds another's file."""
        name, folders, path = parts[-1], parts[:-1], did["path"]
        way, _, _ = _enter(self._root(path), folders, path)
        try:
            kept = self.kept / str(step)
            replaced = kept if kept.exists() else None
            now = _look(way, name)
            outcome = "back"
            if way is not None and did["wrote"] is not None and _identity(now) == did["wrote"]:
                if did["was"] is not None and replaced is None:
                    return "changed"
                out = _own_file()
                os.rename(name, out, src_dir_fd=way, dst_dir_fd=way)
                if did["was"] is not None:
                    if not self._place(did, way, replaced, name):
                        os.unlink(out, dir_fd=way)
                        return "changed"
                    outcome = "restored"
                os.unlink(out, dir_fd=way)
            elif now is None and did["was"] is not None:
                if replaced is None:
                    return "changed"
                if way is None:
                    way, _, _ = _enter(self._root(path), folders, path, make=True, modes=dict(map(tuple, did["above"])))
                if not self._place(did, way, replaced, name):
                    return "changed"
                outcome = "restored"
            elif not _alike(now, did["was"]):
                return "changed"
            self._drop(step)
            if did["was"] is None:
                self._empty(did["made"])
            return outcome
        finally:
            if way is not None:
                os.close(way)

    def _place(self, did: dict[str, Any], way: int, held: Path, name: str) -> bool:
        """The file a step replaced takes *name* again, its own mode back, only where nothing holds the name."""
        os.chmod(held, did["mode"])
        try:
            os.link(held, name, dst_dir_fd=way, follow_symlinks=False)
        except FileExistsError:
            os.chmod(held, 0o600)
            return False
        return True

    def forget(self) -> dict[str, Any]:
        for step in self.steps():
            self.read(step)
        gone = self.store / f"{_FORGOTTEN}{uuid.uuid4()}"
        try:
            os.rename(self.kept, gone)
        except FileNotFoundError:
            return {}
        shutil.rmtree(gone)
        return {}
