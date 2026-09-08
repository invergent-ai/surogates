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


def test_parse_rejects_multi_gym_tasks(tmp_path):
    path = tmp_path / "task_h.json"
    multi = _task_json()
    multi["gym_servers_config"].append(dict(multi["gym_servers_config"][0]))
    path.write_text(json.dumps(multi))
    with pytest.raises(ValueError, match="exactly one gym server"):
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
