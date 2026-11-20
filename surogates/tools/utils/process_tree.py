"""Stop a command and everything it started: its whole process tree.

A timed-out or cancelled tool call used to kill only the process it held
(the shell, or the sandbox daemon's forked tool child).  What the command
had started kept running, reparented to the container's init: a script
that grew without end ate a shared host's memory for an hour after its
call had timed out.

:func:`stop_process_tree` freezes the tree (``SIGSTOP``, so nothing in it
forks past the walk), sends every member but the root ``SIGTERM`` and
``SIGCONT``, waits a grace period, freezes what is left again, and
``SIGKILL``\\ s it, the root last.  The root stays stopped meanwhile: where
it is a subreaper (the daemon's tool child) what its members orphan is
reparented to it and stays in the tree.  The members are the root's
descendants and, for a command started in a session of its own, the
session's processes.  A process is told apart by its start time as well as
its pid, so a pid reused meanwhile is never signalled.  Linux only (it
reads ``/proc``); elsewhere (macOS) it signals the group of the session the
command leads, with the grace, and only that.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import threading
import time
from collections.abc import Iterable

#: Seconds a stopped tree's members have between ``SIGTERM`` and ``SIGKILL``.
KILL_GRACE_SECONDS = 2.0
#: ``oom_score_adj`` of what runs a tool's commands: the kernel's first pick at the sandbox's memory limit.
COMMAND_OOM_SCORE_ADJ = 1000
# Walks of a frozen tree before it is taken as complete: each round stops what the last one found.
_MAX_FREEZE_ROUNDS = 20
_POLL_SECONDS = 0.05
_PROC = "/proc"
_HAS_PROC = os.path.isdir(_PROC)

try:  # resolved once, at import, not in a fork
    import ctypes

    _PRCTL = ctypes.CDLL(None, use_errno=True).prctl
except (OSError, AttributeError):
    _PRCTL = None

# Set in a process every one of whose descendants is the work of the one call it runs (the daemon's tool child).
_OWNS_DESCENDANTS = False
# The calls a stop makes, by these names (tests stand in for them here, not for ``os``'s own).
_kill = os.kill
_getpid = os.getpid
# A root's start time not given: read at the stop.  ``None`` given: the root is gone.
_UNREAD: object = object()


def _stat(pid: int) -> tuple[str, int, int, int] | None:
    """(state, ppid, session, start time) of *pid*, from ``/proc/<pid>/stat``; ``None`` when it is gone."""
    try:
        with open(f"{_PROC}/{pid}/stat", "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    # The command name is in parentheses and may hold spaces or parentheses: the fields follow the last ")".
    fields = data[data.rfind(b")") + 2:].split()
    try:
        return fields[0].decode(), int(fields[1]), int(fields[3]), int(fields[19])
    except (IndexError, ValueError):
        return None


def start_time(pid: int) -> int | None:
    """When *pid* started (clock ticks after boot), or ``None``: with its pid, what tells it from a later one."""
    stat = _stat(pid)
    return stat[3] if stat is not None else None


def _all_stats() -> dict[int, tuple[str, int, int, int]]:
    stats: dict[int, tuple[str, int, int, int]] = {}
    try:
        names = os.listdir(_PROC)
    except OSError:
        return stats
    for name in names:
        if name.isdigit():
            stat = _stat(int(name))
            if stat is not None:
                stats[int(name)] = stat
    return stats


def descendants(pid: int) -> dict[int, int]:
    """Every live process under *pid* (its children, theirs, ...), pid -> start time."""
    return _members(_all_stats(), roots=(pid,), session=None, since=0)


def _members(
    stats: dict[int, tuple[str, int, int, int]], *, roots: Iterable[int], session: int | None, since: int,
) -> dict[int, int]:
    """The descendants of *roots* and, with *session*, the processes of that session started at or after *since*,
    with theirs: pid -> start time."""
    children: dict[int, list[int]] = {}
    for child, (_state, ppid, _sid, _start) in stats.items():
        children.setdefault(ppid, []).append(child)
    found: dict[int, int] = {}
    stack = list(roots)
    if session is not None:
        for pid, (_state, _ppid, sid, start) in stats.items():
            if sid == session and start >= since and pid != session:
                found[pid] = start
                stack.append(pid)
    while stack:
        for child in children.get(stack.pop(), ()):
            if child not in found:
                found[child] = stats[child][3]
                stack.append(child)
    return found


def _alive(pid: int, start: int | None) -> bool:
    """Whether *pid* still runs as the process that started at *start* (a zombie has ended)."""
    if not _HAS_PROC:
        try:
            _kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    stat = _stat(pid)
    if stat is None:
        return False
    state, _ppid, _sid, started = stat
    return state not in ("Z", "X", "x") and (start is None or started == start)


def _signal(pids: dict[int, int | None], sig: int) -> None:
    me = _getpid()
    for pid, start in pids.items():
        if pid != me and _alive(pid, start):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                _kill(pid, sig)


def _own_session() -> int | None:
    try:
        return os.getsid(0)
    except (AttributeError, OSError):
        return None


def stop_process_tree(
    root: int,
    *,
    root_start: int | None | object = _UNREAD,
    include_root: bool = True,
    session: int | None = None,
    also_below: Iterable[int] = (),
    grace: float = KILL_GRACE_SECONDS,
) -> None:
    """Stop *root*'s whole process tree: ``SIGTERM``, then ``SIGKILL`` after *grace* seconds; the root last.

    *root_start* is the root's start time as read when it was started (not
    given: read now; ``None``: it had ended by then): a root that is no
    longer that process (it ended, and its pid was taken) is not signalled
    nor walked from, and a session it led that is now another's is not stopped.  *include_root* stops *root* itself too.
    *session* is the session the command leads (started with
    ``start_new_session``): its processes are stopped with the tree, also
    what left the tree (a double fork) but not the session.  *also_below* are
    processes, never signalled themselves, whose every descendant belongs to
    this stop (the daemon's tool child, the subreaper of its call).  Never
    the caller, nor its own session.  Blocking (sleeps up to *grace*): from
    async code, :func:`stop_process_tree_async`.
    """
    me = _getpid()
    if session is not None and session in (me, _own_session()):
        session = None
    if root_start is _UNREAD:
        root_start = start_time(root)
    root_is_it = root_start is not None and _alive(root, root_start)
    if session is not None and not root_is_it and session == root and _alive(root, None):
        # The leader's pid is another process's now: so is the session, if it holds one.
        session = None
    # The caller may be the root, walked from, never signalled.
    anchor: dict[int, int | None] = {root: root_start} if include_root and root_is_it and root != me else {}
    if not _HAS_PROC:
        _stop_without_proc(anchor, session, grace)
        return
    since = root_start or 0
    roots = [*((root,) if root_is_it else ()), *also_below]
    tree: dict[int, int | None] = {}

    def freeze() -> None:
        # What is stopped forks no more, so a walk after the last stop sees the whole tree.
        for _ in range(_MAX_FREEZE_ROUNDS):
            members = _members(_all_stats(), roots=[*roots, *tree], session=session, since=since)
            found = {pid: start for pid, start in members.items() if pid not in tree and pid not in (me, root)}
            if not found:
                return
            tree.update(found)
            _signal(found, signal.SIGSTOP)

    # Stopped, and kept so to the end: a subreaper root keeps what its members orphan.
    _signal(anchor, signal.SIGSTOP)
    freeze()
    # Stopped processes take the SIGTERM when continued.
    _signal(tree, signal.SIGTERM)
    _signal(tree, signal.SIGCONT)
    _wait_gone(tree, grace)
    # Stopped again, all of it: one that ignored the SIGTERM forks no more between the last walk and the SIGKILL.
    _signal(tree, signal.SIGSTOP)
    freeze()  # what the SIGTERM handlers started meanwhile
    _signal(tree, signal.SIGKILL)
    _signal(anchor, signal.SIGKILL)


def _stop_without_proc(anchor: dict[int, int | None], session: int | None, grace: float) -> None:
    """No ``/proc`` (macOS): the process group the session's leader leads (what kept to it), and the root."""
    def killpg(sig: int) -> bool:
        if session is None or not hasattr(os, "killpg"):
            return False
        try:
            os.killpg(session, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        return True

    killpg(signal.SIGTERM)
    _signal(anchor, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and (killpg(0) or any(_alive(pid, start) for pid, start in anchor.items())):
        time.sleep(_POLL_SECONDS)
    killpg(signal.SIGKILL)
    _signal(anchor, signal.SIGKILL)


def _wait_gone(tree: dict[int, int | None], grace: float) -> None:
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not any(_alive(pid, start) for pid, start in tree.items()):
            return
        time.sleep(_POLL_SECONDS)


async def stop_process_tree_async(root: int, **kwargs) -> None:
    """:func:`stop_process_tree` on a thread of its own, awaited: never on the event loop's executor, whose
    threads a cancelled call's own wait may hold."""
    loop = asyncio.get_running_loop()
    done = loop.create_future()

    def finish() -> None:
        if not done.done():
            done.set_result(None)

    def run() -> None:
        try:
            stop_process_tree(root, **kwargs)
        finally:
            with contextlib.suppress(RuntimeError):  # the loop closed meanwhile
                loop.call_soon_threadsafe(finish)

    threading.Thread(target=run, name=f"stop-tree-{root}").start()
    await done


def stop_process_tree_soon(root: int, **kwargs) -> None:
    """:func:`stop_process_tree` on a thread of its own, not awaited: for a path that must not wait (a cancel).
    Not a daemon thread: the stop is not cut short, leaving processes stopped, when the process exits."""
    threading.Thread(target=stop_process_tree, args=(root,), kwargs=kwargs, name=f"stop-tree-{root}").start()


def become_subreaper() -> bool:
    """Make this process its descendants' subreaper: what they orphan is reparented to it, not to init.

    So a double-forked process stays in this process's tree, where
    :func:`stop_process_tree` finds it.  ``False`` where the kernel has no
    such option (not Linux).
    """
    if _PRCTL is None:
        return False
    PR_SET_CHILD_SUBREAPER = 36
    return _PRCTL(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0


def own_descendants() -> None:
    """Mark this process as one whose every descendant is the work of the one call it runs: a command's timeout
    then stops all of them, also what a double fork reparented to it (:func:`owns_descendants`)."""
    global _OWNS_DESCENDANTS
    _OWNS_DESCENDANTS = True


def owns_descendants() -> bool:
    """Whether :func:`own_descendants` marked this process."""
    return _OWNS_DESCENDANTS


def go_first_out_of_memory(pid: int | str = "self", adj: int = COMMAND_OOM_SCORE_ADJ) -> bool:
    """Make *pid* (and what it starts later, which inherits it) the kernel's first pick when memory runs out.

    In a Docker sandbox held to a memory limit the runaway command dies
    there, not the daemon that serves the sandbox or the workspace's mount.
    (Kubernetes on cgroup v2 kills a container's whole group at its limit.)
    Raising a score needs no privilege, but the process must be dumpable;
    ``False`` where it cannot be set.
    """
    try:
        with open(f"{_PROC}/{pid}/oom_score_adj", "w") as fh:
            fh.write(str(adj))
        return True
    except OSError:
        return False
