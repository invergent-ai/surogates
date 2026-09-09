"""Cross-benchmark boundaries: native parsers, grading, private inputs and splits."""
import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

from harness_evolution import benchmark_driver
from harness_evolution.benchmarks import artifact_name, check_reservations
from harness_evolution.config import read_json, validate_tasks, write_json
from harness_evolution.data import freeze_data, tree_hash
from harness_evolution.scoring import InvalidEvaluation, normalize
from test_adapters import parser_from_benchmark


@pytest.mark.parametrize('benchmark,package,field', [
    ('enterpriseops_gym', 'eogbench', 'passed'), ('dabstep', 'dabbench', 'correct'), ('gaia', 'gaia_bench', 'strict_pass'),
])
def test_new_drivers_use_native_cli_and_bound_resources(tmp_path, monkeypatch, benchmark, package, field):
    tid = 'csm/a' if benchmark == 'enterpriseops_gym' else '1'
    output = tmp_path / 'run'
    calls = []
    cli = types.ModuleType(package + '.cli')
    cli.build_parser = parser_from_benchmark(benchmark, package)
    outcome = {'task_id': tid, field: True, 'verifiers_total': 2, 'verifiers_passed': 2}
    prefix = {'enterpriseops_gym': 'EOG', 'dabstep': 'DABSTEP', 'gaia': 'GAIA'}[benchmark]
    import os
    monkeypatch.setattr(os, 'environ', dict(os.environ))
    key_path = tmp_path / 'key.json'
    write_json(key_path, {'entries': {'1': {'answer': 'private-answer', 'source': 'fixture'}}})

    @dataclass
    class Task:
        domain: str = 'csm'
        gym_url: str = 'http://stale.example.test'

    cli.load_tasks = lambda *args, **kwargs: [Task()]

    def save():
        write_json(output / 'outcomes.json', [outcome])

    async def run(args):
        calls.append(args.command)
        assert args.tasks == tid and args.wall_clock_cap == 30
        assert os.environ[prefix + '_AGENT_ID'] == 'profile-standard'
        assert os.environ[prefix + '_DATASET_REVISION'] == 'a' * 40
        if benchmark == 'enterpriseops_gym':
            assert args.domains == 'csm'
            assert cli.load_tasks()[0].gym_url == 'http://127.0.0.1:8100'
            assert os.environ['EOG_TASKS_DIR'] == str(tmp_path / 'tasks')
            assert os.environ['EOG_SEED_ROOT'] == str(tmp_path / 'seeds')
            assert 'EOG_GYM_URL' not in os.environ
        else:
            assert args.split == 'dev' and args.concurrency == 1
        task_dir = output / 'tasks' / artifact_name(benchmark, tid)
        write_json(task_dir / 'meta.json', {'terminal_status': 'completed', 'wall_clock_s': 3, 'session_id': 'session'})
        (task_dir / 'events.jsonl').write_text(json.dumps({'type': 'llm.request', 'data': {'model': 'served-standard'}}) + '\n')
        if benchmark != 'dabstep':
            save()
        return 0

    def score(args):
        calls.append(args.command)
        assert cli.load_key() == read_json(key_path)
        save()
        return 0

    cli._cmd_run, cli._cmd_score = run, score
    module = types.ModuleType(package)
    module.cli = cli
    monkeypatch.setitem(sys.modules, package, module)
    monkeypatch.setitem(sys.modules, package + '.cli', cli)
    monkeypatch.setattr(sys, 'path', list(sys.path))
    monkeypatch.setenv('EOG_GYM_URL', 'http://stale.example.test')
    request = {'benchmark': benchmark, 'tier': 'standard', 'benchmark_root': str(tmp_path),
               'output_dir': str(output), 'tasks': [{'benchmark': benchmark, 'task_id': tid}],
               'dataset_revision': 'a' * 40, 'task_timeout_s': 30,
               'benchmark_data': {'mode': 'oracle', 'tasks_dir': str(tmp_path / 'tasks'), 'seed_root': str(tmp_path / 'seeds'), 'answer_key': str(key_path)},
               'runtime': {'base_url': 'http://localhost:8000', 'ops_base_url': 'http://localhost:8888', 'project_id': 'project',
                           'gym_urls': {'csm': 'http://127.0.0.1:8100'},
                           'agents': {'standard': 'generic-standard'}, 'benchmark_agents': {benchmark: {'standard': 'profile-standard'}},
                           'models': {'standard': {'model': 'canonical', 'served_model': 'served-standard'}}}}
    rows = benchmark_driver.run(request)
    assert rows[0]['passed'] and rows[0]['score'] == 1 and rows[0]['seconds'] == 3
    assert calls == (['run', 'score'] if benchmark == 'dabstep' else ['run'])
    if benchmark == 'enterpriseops_gym':
        assert read_json(output / 'run-config.json')['mode'] == 'oracle'


@pytest.mark.parametrize('benchmark,field', [('dabstep', 'correct'), ('gaia', 'strict_pass'), ('enterpriseops_gym', 'passed')])
def test_native_grades_and_missing_grades(benchmark, field):
    outcome = {'task_id': '1', field: False, 'verifiers_total': 2, 'verifiers_passed': 1}
    meta = {'terminal_status': 'completed', 'wall_clock_s': 4}
    assert normalize(benchmark, outcome, meta)['score'] == 0
    with pytest.raises(InvalidEvaluation, match='grade'):
        normalize(benchmark, {**outcome, field: None}, meta)
    with pytest.raises(InvalidEvaluation, match='Rollout'):
        normalize(benchmark, {**outcome, 'verify_error': 'gym unavailable'}, meta)


def test_inconsistent_verifier_counts_and_unsupported_capability_are_invalid():
    with pytest.raises(InvalidEvaluation, match='Inconsistent'):
        normalize('enterpriseops_gym', {'task_id': 'csm/a', 'passed': True, 'verifiers_total': 2, 'verifiers_passed': 1}, {'terminal_status': 'completed'})
    with pytest.raises(InvalidEvaluation, match='unsupported'):
        normalize('gaia', {'task_id': '1', 'strict_pass': False, 'flags': ['unsupported_capability']}, {'terminal_status': 'completed'})


@pytest.mark.parametrize('benchmark,tid', [('dabstep', '3'), ('gaia', 'gaia-holdout'), ('workspace_bench', '4')])
def test_existing_reservations_cannot_enter_search(repository, benchmark, tid):
    task = {'benchmark': benchmark, 'task_id': tid, 'split': 'search', 'upstream_split': 'dev'}
    with pytest.raises(ValueError, match='holdout'):
        check_reservations(repository, [task])
    check_reservations(repository, [{**task, 'split': 'holdout', 'upstream_split': 'holdout'}])


def test_selection_only_general_capability_suite_requires_guards(tasks):
    guard = {'benchmark': 'gaia', 'task_id': 'gaia-dev-a', 'split': 'selection', 'family': 'research', 'guard': True}
    validate_tasks([*tasks, guard])
    with pytest.raises(ValueError, match='guards'):
        validate_tasks([*tasks, {**guard, 'guard': False}])


def test_private_dataset_and_key_are_copied_and_hashed(tmp_path, config):
    import hashlib
    task_root, seed_root = tmp_path / 'input-tasks', tmp_path / 'input-seeds'
    write_json(task_root / 'csm/task_a.json', {'gym_servers_config': [{'seed_database_file': 'dbs/seed.sql'}]})
    seed = seed_root / 'dbs/seed.sql'
    seed.parent.mkdir(parents=True)
    seed.write_text('PRIVATE SEED')
    task = {'task_id': 'csm/a', 'file': 'csm/task_a.json', 'sha256': hashlib.sha256((task_root / 'csm/task_a.json').read_bytes()).hexdigest()}
    write_json(task_root / 'import.json', {'revision': 'a' * 40, 'mode': 'oracle', 'accepted': [task]})
    key_path = tmp_path / 'key.json'
    write_json(key_path, {'entries': {'1': {'answer': 'SECRET', 'source': 'reviewed'}}})
    config['task_manifest'] += [{'benchmark': 'enterpriseops_gym', 'task_id': 'csm/a'}, {'benchmark': 'dabstep', 'task_id': '1'}]
    config['benchmark_data'] = {'enterpriseops_gym': {'tasks_dir': str(task_root), 'seed_root': str(seed_root)}, 'dabstep': {'answer_key': str(key_path)}}
    config['dataset_revisions'] = {'enterpriseops_gym': 'a' * 40}
    private = tmp_path / 'private'
    bindings = freeze_data(config, private)
    frozen = tree_hash(private)
    seed.write_text('EDITED INPUT')
    key_path.unlink()
    assert tree_hash(private) == frozen
    assert read_json(Path(bindings['dabstep']['answer_key']))['entries']['1']['answer'] == 'SECRET'
    (Path(bindings['enterpriseops_gym']['seed_root']) / 'dbs/seed.sql').write_text('TAMPERED')
    assert tree_hash(private) != frozen
    (task_root / 'csm/task_a.json').write_text('{}')
    with pytest.raises(ValueError, match='modified'):
        freeze_data(config, tmp_path / 'another-private')


def test_private_key_must_cover_every_requested_task(tmp_path, config):
    key = tmp_path / 'key.json'
    write_json(key, {'entries': {}})
    config['benchmark_data'] = {'dabstep': {'answer_key': str(key)}}
    config['task_manifest'].append({'benchmark': 'dabstep', 'task_id': '1'})
    with pytest.raises(ValueError, match='cover'):
        freeze_data(config, tmp_path / 'private')


def test_resume_rejects_changed_private_bindings(config, tmp_path):
    from harness_evolution.experiment import initialize, validate_resume
    root = tmp_path / 'experiment'
    root.mkdir()
    initialize(config, root)
    validate_resume(config, root)
    write_json(root / 'private_data/bindings.json', {'dabstep': {'answer_key': 'replacement'}})
    with pytest.raises(ValueError, match='Frozen task data'):
        validate_resume(config, root)


def test_catalog_reads_native_enterprise_artifact_names(tmp_path, repository):
    from harness_evolution.cli import catalog
    root = tmp_path / 'run'
    write_json(root / 'run-config.json', {'mode': 'plus_5_tools'})
    write_json(root / 'outcomes.json', [{'task_id': 'csm/a', 'domain': 'csm', 'passed': True,
                                      'verifiers_total': 1, 'verifiers_passed': 1, 'terminal_status': 'completed'}])
    write_json(root / 'tasks/csm__a/meta.json', {'wall_clock_s': 1})
    row, = catalog([f'enterpriseops_gym={root}'], repository)
    assert row['upstream_split'] == 'plus_5_tools'
    assert row['family'] == 'csm'
    assert row['group'] == 'enterpriseops_gym:csm/a'
    assert set(row).isdisjoint({'verifiers', 'answer', 'failed_verifiers'})
