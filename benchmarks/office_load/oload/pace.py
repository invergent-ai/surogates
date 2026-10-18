"""How fast a virtual session works: edits per turn, the time between them, and the idle between turns."""

from __future__ import annotations

import random
from typing import Literal

Workload = Literal["rollout", "chat"]
WORKLOADS: tuple[Workload, ...] = ("rollout", "chat")

# Recorded from the office benchmark's final run on the templates build (108 sessions, one user turn each), and only
# the numbers kept. RECORDED_EDITS: the office_edit calls each session made (median 1, p90 6, max 28).
# RECORDED_GAPS: the seconds between a session's consecutive tool calls, any tool, read as the time between the starts
# of consecutive model iterations that called tools (median 8.2, mean 11.9).
RECORDED_EDITS: tuple[int, ...] = (
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2,
    2, 2, 3, 3, 3, 3, 3, 4, 4, 4, 4, 4, 5, 5, 6, 6, 6, 6, 6, 6, 6, 7, 7, 7, 7, 9, 12, 12, 17, 28,
)

RECORDED_GAPS: tuple[float, ...] = (
    1.1, 1.1, 1.2, 1.2, 1.2, 1.3, 1.3, 1.3, 1.5, 1.5, 1.5, 1.6, 1.6, 1.6, 1.6, 1.6, 1.7, 1.7, 1.7, 1.8, 1.8, 1.8, 1.9,
    2.0, 2.4, 2.4, 2.4, 2.5, 2.5, 2.5, 2.5, 2.5, 2.6, 2.6, 2.6, 2.7, 2.7, 2.7, 2.7, 2.8, 2.8, 2.8, 2.8, 2.8, 2.9, 2.9,
    3.0, 3.0, 3.0, 3.0, 3.1, 3.1, 3.2, 3.2, 3.2, 3.2, 3.3, 3.3, 3.3, 3.3, 3.4, 3.4, 3.5, 3.5, 3.6, 3.6, 3.6, 3.6, 3.6,
    3.7, 3.7, 3.8, 3.8, 3.8, 3.8, 3.8, 3.8, 3.8, 3.8, 3.8, 3.8, 3.9, 3.9, 3.9, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0,
    4.0, 4.0, 4.0, 4.0, 4.0, 4.1, 4.1, 4.1, 4.1, 4.1, 4.1, 4.1, 4.1, 4.1, 4.1, 4.1, 4.1, 4.1, 4.1, 4.2, 4.2, 4.2, 4.3,
    4.3, 4.3, 4.3, 4.3, 4.3, 4.4, 4.4, 4.4, 4.4, 4.4, 4.4, 4.4, 4.5, 4.5, 4.5, 4.6, 4.6, 4.7, 4.7, 4.7, 4.8, 4.8, 4.8,
    4.8, 4.8, 4.9, 4.9, 4.9, 4.9, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.1, 5.1, 5.1, 5.1, 5.2, 5.2, 5.3, 5.4, 5.4, 5.4, 5.4,
    5.4, 5.4, 5.5, 5.6, 5.6, 5.6, 5.7, 5.7, 5.8, 5.8, 6.0, 6.0, 6.0, 6.0, 6.1, 6.1, 6.1, 6.1, 6.1, 6.1, 6.1, 6.1, 6.1,
    6.1, 6.2, 6.2, 6.2, 6.3, 6.3, 6.4, 6.4, 6.4, 6.4, 6.5, 6.7, 6.7, 6.7, 6.7, 6.8, 6.8, 6.9, 6.9, 6.9, 7.0, 7.0, 7.1,
    7.2, 7.2, 7.3, 7.3, 7.3, 7.4, 7.4, 7.4, 7.4, 7.5, 7.6, 7.7, 7.7, 7.7, 8.0, 8.1, 8.1, 8.2, 8.2, 8.2, 8.2, 8.2, 8.2,
    8.3, 8.3, 8.3, 8.3, 8.4, 8.4, 8.4, 8.4, 8.5, 8.5, 8.5, 8.6, 8.6, 8.6, 8.6, 8.6, 8.6, 8.6, 8.6, 8.6, 8.7, 8.7, 8.7,
    8.7, 8.9, 8.9, 9.0, 9.0, 9.1, 9.1, 9.1, 9.2, 9.3, 9.3, 9.3, 9.3, 9.4, 9.4, 9.5, 9.5, 9.5, 9.5, 9.6, 9.7, 9.8, 9.8,
    9.8, 9.9, 10.1, 10.2, 10.4, 10.5, 10.5, 10.6, 10.7, 10.7, 10.9, 10.9, 10.9, 11.2, 11.2, 11.3, 11.3, 11.4, 11.5,
    11.6, 11.8, 11.8, 11.9, 12.0, 12.0, 12.0, 12.1, 12.1, 12.2, 12.2, 12.2, 12.6, 12.8, 12.9, 12.9, 12.9, 13.0, 13.0,
    13.2, 13.2, 13.3, 13.3, 13.4, 13.5, 13.5, 13.5, 13.6, 13.6, 13.6, 13.6, 13.7, 13.7, 13.8, 13.8, 13.8, 13.8, 13.9,
    13.9, 13.9, 14.1, 14.1, 14.1, 14.2, 14.2, 14.2, 14.2, 14.2, 14.3, 14.3, 14.5, 14.5, 14.6, 14.6, 14.8, 14.9, 14.9,
    15.1, 15.7, 15.9, 16.1, 16.1, 16.4, 16.4, 16.4, 16.6, 16.7, 16.8, 16.8, 16.8, 16.8, 16.9, 17.0, 17.2, 17.2, 17.2,
    17.3, 17.5, 17.5, 17.7, 17.8, 17.8, 17.9, 17.9, 18.1, 18.3, 18.4, 18.6, 19.1, 19.2, 19.5, 19.7, 19.9, 20.1, 20.4,
    20.5, 20.6, 20.8, 20.9, 20.9, 21.2, 21.4, 21.5, 21.6, 21.6, 21.9, 22.0, 22.5, 23.1, 23.4, 23.4, 23.5, 24.0, 24.7,
    25.1, 25.4, 25.6, 26.3, 26.5, 26.6, 27.0, 27.1, 27.6, 27.6, 27.7, 27.9, 28.2, 28.4, 29.0, 30.1, 30.2, 30.7, 31.3,
    31.4, 32.1, 32.3, 32.8, 33.4, 34.8, 35.8, 39.6, 39.6, 40.2, 42.4, 46.6, 46.6, 49.5, 50.4, 55.0, 55.3, 55.4, 56.4,
    57.4, 58.4, 63.0, 73.1, 85.9, 96.1, 103.7,
)

# A chat's person reads and replies between turns; a rollout goes straight on.
CHAT_IDLE_S = (30.0, 90.0)


class Pace:
    """One session's pace, drawn from *rng*."""

    def __init__(self, workload: Workload, rng: random.Random | None = None) -> None:
        if workload not in WORKLOADS:
            raise ValueError(f"the workload is rollout or chat, not {workload!r}")
        self.workload = workload
        self.rng = rng if rng is not None else random.Random()

    def edits_in_turn(self) -> int:
        return self.rng.choice(RECORDED_EDITS)

    def gap_s(self) -> float:
        """The time before the turn's next edit."""
        return self.rng.choice(RECORDED_GAPS)

    def idle_s(self) -> float:
        """The time between one turn's save and the next turn's first edit."""
        return self.rng.uniform(*CHAT_IDLE_S) if self.workload == "chat" else 0.0
