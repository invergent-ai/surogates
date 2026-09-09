from pathlib import Path

import pytest

from harness_evolution.cli import main, preflight


def test_example_plan_remains_offline_with_unselected_environment_and_models(monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        raise AssertionError("Offline planning must not start an experiment")

    monkeypatch.setattr("harness_evolution.cli.os.environ", {})
    monkeypatch.setattr("harness_evolution.cli.run", unexpected)
    monkeypatch.setattr("harness_evolution.cli.final_test", unexpected)
    example = Path(__file__).resolve().parents[1] / "examples/pilot.json"
    assert main(["plan", str(example)]) == 0
    output = capsys.readouterr().out
    assert "162 task rollouts" in output
    assert "models.pro.served_model" in output
    assert "models.standard.served_model" in output
    assert "dataset_revisions.workspace_bench" in output
    assert "EVOLVE_COMPOSE_FILE" in output


def test_live_run_refuses_placeholder_configuration_before_creating_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr("harness_evolution.cli.os.environ", {})
    example = Path(__file__).resolve().parents[1] / "examples/pilot.json"
    root = tmp_path / "experiment"
    with pytest.raises(SystemExit) as error:
        main(["run", str(example), "--run-dir", str(root)])
    assert error.value.code == 2
    assert not root.exists()


def test_final_test_does_not_require_proposer_credentials(config, monkeypatch):
    monkeypatch.setattr("harness_evolution.cli.os.environ", {})
    config["proposer"] = {"model": "REPLACE_MODEL", "base_url_env": "PROPOSER_URL", "api_key_env": "PROPOSER_KEY"}
    assert any("proposer" in problem for problem in preflight(config))
    assert not any("proposer" in problem for problem in preflight(config, needs_proposer=False))


def test_enterprise_example_plans_all_five_suites_without_live_bindings(monkeypatch, capsys):
    monkeypatch.setattr('harness_evolution.cli.os.environ', {})
    example = Path(__file__).resolve().parents[1] / 'examples/enterprise.json'
    assert main(['plan', str(example)]) == 0
    output = capsys.readouterr().out
    assert '708 task rollouts' in output
    assert 'EVOLVE_EOG_SERVICES_JSON' in output
    assert 'benchmark_data.dabstep.answer_key' in output
    assert 'dataset_revisions.gaia' in output
