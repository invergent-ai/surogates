"""Task JSON parsing against a fixture mirroring the upstream schema."""
import json

import pytest

from eogbench.dataset import _parse, load_tasks


def _task_json(**overrides):
    task = {
        "mcp_endpoint": "/mcp",
        "number_of_runs": 1,
        "reset_database_between_runs": True,
        "gym_servers_config": [{
            "mcp_server_name": "sn-csm-server",
            "mcp_server_url": "http://localhost:8001",
            "seed_database_file": "dbs/csm/db_1.sql",
        }],
        "system_prompt": "CSM Agent Policy...",
        "user_prompt": "Update the entitlement.",
        "selected_tools": ["update_entitlement", "find_user"],
        "restricted_tools": [],
        "verifiers": [{
            "verifier_type": "database_state",
            "name": "update_entitlement",
            "gym_name": "sn-csm-server",
            "validation_config": {"query": "SELECT ..."},
        }],
    }
    task.update(overrides)
    return task


def test_parse_maps_all_fields(tmp_path):
    path = tmp_path / "task_x.json"
    path.write_text(json.dumps(_task_json()))
    task = _parse(path, "csm")
    assert task.task_id == "csm/x"
    assert task.gym_name == "sn-csm-server"
    assert task.gym_url == "http://localhost:8001"
    assert task.seed_database_file == "dbs/csm/db_1.sql"
    assert task.selected_tools == ("update_entitlement", "find_user")
    assert len(task.verifiers) == 1
    assert task.reset_database is True


def _hybrid_json(**overrides):
    multi = _task_json()
    multi["gym_servers_config"].append({
        "mcp_server_name": "gym-calendar",
        "mcp_server_url": "http://localhost:8003",
        "seed_database_file": "dbs/hybrid/db_2.sql",
        "context": {"x-access-token": "tok"},
    })
    multi["verifiers"].append({
        "verifier_type": "database_state", "name": "event", "gym_name": "gym-calendar",
        "validation_config": {"query": "SELECT ..."},
    })
    multi.update(overrides)
    return multi


def test_parse_multi_gym_tasks(tmp_path):
    path = tmp_path / "task_h.json"
    path.write_text(json.dumps(_hybrid_json()))
    task = _parse(path, "hybrid")
    assert [g.name for g in task.gyms] == ["sn-csm-server", "gym-calendar"]
    assert task.gyms[1].context == {"x-access-token": "tok"}
    assert task.gyms[1].seed_database_file == "dbs/hybrid/db_2.sql"
    # the single-gym fields repeat the first gym
    assert (task.gym_name, task.gym_url) == ("sn-csm-server", "http://localhost:8001")


def test_single_gym_tasks_have_no_gym_list(tmp_path):
    path = tmp_path / "task_x.json"
    path.write_text(json.dumps(_task_json()))
    assert _parse(path, "csm").gyms == ()


@pytest.mark.parametrize("change, message", [
    (lambda t: t["verifiers"][1].update(gym_name="gym-email-mcp"), "outside the task"),
    (lambda t: t["verifiers"][1].pop("gym_name"), "outside the task"),
    (lambda t: t["gym_servers_config"][1].update(mcp_server_name="sn-csm-server"), "distinct"),
    (lambda t: t["gym_servers_config"][1].update(seed_database_file="../x.sql"), "relative path"),
])
def test_parse_rejects_bad_multi_gym_tasks(tmp_path, change, message):
    task = _hybrid_json()
    change(task)
    path = tmp_path / "task_h.json"
    path.write_text(json.dumps(task))
    with pytest.raises(ValueError, match=message):
        _parse(path, "hybrid")


def test_load_tasks_walks_domain_dirs(tmp_path, monkeypatch):
    (tmp_path / "csm").mkdir()
    (tmp_path / "itsm").mkdir()
    (tmp_path / "csm" / "task_a.json").write_text(json.dumps(_task_json()))
    (tmp_path / "itsm" / "task_b.json").write_text(json.dumps(_task_json()))
    monkeypatch.setenv("EOG_TASKS_DIR", str(tmp_path))

    tasks = load_tasks()
    assert [t.task_id for t in tasks] == ["csm/a", "itsm/b"]
    assert load_tasks(("itsm",))[0].domain == "itsm"


def test_load_tasks_empty_dir_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setenv("EOG_TASKS_DIR", str(tmp_path))
    with pytest.raises(SystemExit, match="no task_"):
        load_tasks()


def test_an_empty_user_info_string_is_absent():
    # Rows of the public dataset carry user_info == "" (JSON-encoded columns): absent, not a parse error.
    raw = _task_json()
    raw["gym_servers_config"][0]["user_info"] = ""
    raw["gym_servers_config"] = json.dumps(raw["gym_servers_config"])
    from eogbench.dataset import parse_record

    assert parse_record(raw, "csm", "task_a").user_info == {}
