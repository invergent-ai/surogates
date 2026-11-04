"""Which mission tasks the judge waits for."""
from __future__ import annotations

from uuid import uuid4

from surogates.missions.store import any_task_in_flight

A, B, C, D = uuid4(), uuid4(), uuid4(), uuid4()


def test_no_tasks_means_nothing_to_wait_for() -> None:
    assert any_task_in_flight({}, {}) is False


def test_running_and_ready_are_in_flight() -> None:
    assert any_task_in_flight({A: "running"}, {}) is True
    assert any_task_in_flight({A: "ready"}, {}) is True


def test_finished_tasks_are_not() -> None:
    assert any_task_in_flight({A: "done", B: "failed", C: "cancelled"}, {}) is False


def test_todo_with_a_parent_still_running_is_in_flight() -> None:
    assert any_task_in_flight({A: "running", B: "todo"}, {B: {A}}) is True


def test_todo_whose_parents_are_done_waits_for_the_tick_to_promote_it() -> None:
    """The gap between the last parent finishing and the 5-second tick."""
    assert any_task_in_flight({A: "done", B: "todo"}, {B: {A}}) is True


def test_a_failed_parent_never_releases_its_child() -> None:
    assert any_task_in_flight({A: "failed", B: "todo"}, {B: {A}}) is False
    assert any_task_in_flight({A: "cancelled", B: "todo"}, {B: {A}}) is False


def test_a_blocked_parent_waits_on_a_person() -> None:
    assert any_task_in_flight({A: "blocked", B: "todo"}, {B: {A}}) is False


def test_one_stuck_parent_is_enough_even_if_another_runs_the_child_is_not_coming() -> None:
    statuses = {A: "failed", B: "done", C: "todo"}
    assert any_task_in_flight(statuses, {C: {A, B}}) is False


def test_a_stuck_chain_is_stuck_all_the_way_down() -> None:
    """The writer's parent failed, so the verifier under the writer waits too."""
    statuses = {A: "failed", B: "todo", C: "todo"}
    assert any_task_in_flight(statuses, {B: {A}, C: {B}}) is False


def test_a_live_branch_keeps_the_mission_in_flight_beside_a_stuck_one() -> None:
    statuses = {A: "failed", B: "todo", C: "running", D: "todo"}
    assert any_task_in_flight(statuses, {B: {A}, D: {C}}) is True


def test_a_parent_outside_the_snapshot_is_not_assumed_done() -> None:
    assert any_task_in_flight({B: "todo"}, {B: {A}}) is False
