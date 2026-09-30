"""Tests for file read tracker dedup and memory bounds."""

from __future__ import annotations

import os
from pathlib import Path

from surogates.tools.builtin import file_ops


def test_update_read_timestamp_invalidates_dedup_for_written_path(tmp_path: Path) -> None:
    file_path = tmp_path / "notes.txt"
    file_path.write_text("before", encoding="utf-8")
    task_id = "dedup-invalidation"
    resolved = str(file_path.resolve())

    file_ops.clear_read_tracker(task_id)
    task_data = file_ops._init_task_data(task_id)
    task_data["dedup"][(resolved, 1, 500)] = os.path.getmtime(resolved)
    task_data["dedup"][(resolved, 501, 500)] = os.path.getmtime(resolved)
    task_data["dedup"][(str((tmp_path / "other.txt").resolve()), 1, 500)] = 1.0

    file_path.write_text("after", encoding="utf-8")
    file_ops._update_read_timestamp(str(file_path), task_id)

    remaining_paths = {key[0] for key in task_data["dedup"]}
    assert resolved not in remaining_paths


def test_update_read_timestamp_caps_tracker_state(tmp_path: Path) -> None:
    file_path = tmp_path / "current.txt"
    file_path.write_text("current", encoding="utf-8")
    task_id = "tracker-cap"
    resolved = str(file_path.resolve())

    file_ops.clear_read_tracker(task_id)
    task_data = file_ops._init_task_data(task_id)
    for i in range(1300):
        old_path = str((tmp_path / f"old-{i}.txt").resolve())
        task_data["read_history"].add((old_path, 1, 500))
        task_data["dedup"][(old_path, 1, 500)] = float(i)
        task_data["read_timestamps"][old_path] = float(i)

    file_ops._update_read_timestamp(str(file_path), task_id)

    assert len(task_data["read_history"]) < 1300
    assert len(task_data["dedup"]) < 1300
    assert len(task_data["read_timestamps"]) < 1301
    assert task_data["read_timestamps"][resolved] == os.path.getmtime(resolved)
