"""What the virtual sessions did: one event per measured call or check, the step judged from them."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

What = Literal["open", "edit", "flush", "forcesave", "callback", "marker", "error"]
# A failure's blame. tool: the tool's own request was refused, which is no load failure.
Cause = Literal["client", "server", "lost", "timeout", "tool"]


@dataclass(frozen=True, slots=True)
class Event:
    session: int
    kind: str  # the document's: docx, xlsx, pptx or pdf
    what: What
    ok: bool
    seconds: float  # how long it took, measured on the monotonic clock
    cause: Cause | None  # None when ok
    at: float  # time.time() when it began: for lining up with the metrics and the servers' logs
    detail: str | None = None  # what failed, in the call's words
    abandoned: bool = False  # of a turn the step's deadline cut short, unsaved: judged by no bar


class EventLog:
    """The events, in the order they were logged; each also appended to *path* as it comes, when given."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = None if path is None else Path(path)
        self.events: list[Event] = []
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, event: Event) -> None:
        self.events.append(event)
        if self.path is not None:
            with self.path.open("a") as file:
                file.write(json.dumps(asdict(event)) + "\n")

    @staticmethod
    def read(path: str | Path) -> list[Event]:
        """The events an events.jsonl holds."""
        return [Event(**json.loads(line)) for line in Path(path).read_text().splitlines() if line.strip()]
