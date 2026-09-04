"""Deterministic episode scoring from collected artifacts. No judge.

Preferred source is ``final_state.json`` (the agent ran ``finalize``).
When it is missing but ``sim_state.pkl`` was collected, the settlement
is recomputed locally against the pinned vendored code -- upstream's
``snapshot_final_state`` is idempotent and runs the same deferred
returns pipeline, so an agent that forgot to finalize is scored
identically, not zeroed.
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import sys
from dataclasses import dataclass, field


@dataclass
class EpisodeOutcome:
    episode: int
    final_assets: float | None
    initial_balance: float
    days_completed: int
    max_days: int
    done: bool
    source: str  # final_state | recomputed | none
    terminal_status: str = ""
    error: str | None = None
    tool_batches: int = 0
    wall_clock_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def multiple(self) -> float | None:
        if self.final_assets is None or not self.initial_balance:
            return None
        return round(self.final_assets / self.initial_balance, 3)


def settle_from_pickle(pkl_path: str) -> dict:
    """Recompute the final settlement from a collected sim_state.pkl."""
    import pickle

    from ecbench import vendor

    root = vendor.home()
    added = [str(root), str(root / "agent")]
    for p in added:
        sys.path.insert(0, p)
    cwd = os.getcwd()
    os.chdir(root)  # upstream resolves data/ relative to its root
    try:
        with open(pkl_path, "rb") as fh:
            state = pickle.load(fh)
        env, job = state["env"], state["job"]

        from ecommerce_tool_manager import EcommerceToolManager  # type: ignore
        from tools import ecommerce_tool_map

        manager = EcommerceToolManager.__new__(EcommerceToolManager)
        manager.env = env
        manager._ecommerce_tool_map = ecommerce_tool_map
        manager._output_log_fp = None
        manager._balance_log_fp = None
        manager._messages_log_fp = None
        manager._seen_balance_dates = set()
        final_assets = manager.snapshot_final_state(job)
        return {
            "final_assets": final_assets,
            "initial_balance": job.get("agent_info", {}).get(
                "initial_balance", 0.0
            ),
            "day_count": getattr(env, "day_count", 0),
            "final_day": job.get("final_day", 0),
            "max_day": getattr(env, "max_day", 0),
            "done": bool(getattr(env, "is_done", False)),
        }
    finally:
        os.chdir(cwd)
        for p in added:
            with contextlib.suppress(ValueError):
                sys.path.remove(p)


def _count_batches(path: pathlib.Path) -> int:
    if not path.exists():
        return 0
    with open(path, encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


def score_episode(episode_dir: str) -> EpisodeOutcome:
    d = pathlib.Path(episode_dir)
    with open(d / "meta.json", encoding="utf-8") as fh:
        meta = json.load(fh)

    notes = list(meta.get("collect_notes") or [])
    batches = _count_batches(d / "calls.jsonl")
    base = dict(
        episode=int(meta.get("episode") or int(d.name)),
        terminal_status=str(meta.get("terminal_status") or ""),
        error=meta.get("error"),
        tool_batches=batches,
        wall_clock_s=float(meta.get("wall_clock_s") or 0.0),
    )

    final_path = d / "final_state.json"
    if final_path.exists():
        with open(final_path, encoding="utf-8") as fh:
            summary = json.load(fh)
        return EpisodeOutcome(
            final_assets=summary.get("final_assets"),
            initial_balance=float(summary.get("initial_balance") or 0.0),
            days_completed=int(summary.get("final_day")
                               or summary.get("day_count") or 0),
            max_days=int(summary.get("max_day") or 0),
            done=bool(summary.get("done")),
            source="final_state",
            notes=notes,
            **base,
        )

    pkl_path = d / "sim_state.pkl"
    if pkl_path.exists():
        try:
            summary = settle_from_pickle(str(pkl_path))
            notes.append("agent did not finalize; settled locally from pickle")
            return EpisodeOutcome(
                final_assets=summary["final_assets"],
                initial_balance=float(summary["initial_balance"] or 0.0),
                days_completed=int(summary["final_day"]
                                   or summary["day_count"] or 0),
                max_days=int(summary["max_day"] or 0),
                done=bool(summary["done"]),
                source="recomputed",
                notes=notes,
                **base,
            )
        except Exception as exc:  # noqa: BLE001 - scored as unscoreable below
            notes.append(f"local settle failed: {type(exc).__name__}: {exc}")

    notes.append("no scoreable state collected")
    return EpisodeOutcome(
        final_assets=None,
        initial_balance=0.0,
        days_completed=0,
        max_days=0,
        done=False,
        source="none",
        notes=notes,
        **base,
    )
