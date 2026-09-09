import json

import pytest

from eogbench.dataset import load_tasks, parse_record
from eogbench.import_tasks import import_rows
from test_dataset import _task_json


def test_import_preserves_identity_and_inventories_unsupported_rows(tmp_path, monkeypatch):
    raw = _task_json(domain='csm', task_id='task_a')
    raw['gym_servers_config'][0].update(context={'user_id': 'u'}, user_info={'name': 'User'})
    for field in ('gym_servers_config', 'verifiers', 'selected_tools'):
        raw[field] = json.dumps(raw[field])
    unsupported = _task_json(domain='hybrid', task_id='task_b', verifiers=[{'verifier_type': 'response_check'}])
    output = tmp_path / 'imported'
    manifest = import_rows([raw, unsupported], output, revision='a' * 40, mode='oracle')
    assert manifest['rows'] == 2
    assert manifest['accepted'][0]['task_id'] == 'csm/a'
    assert manifest['rejected'][0]['task_id'] == 'hybrid/b'
    monkeypatch.setenv('EOG_TASKS_DIR', str(output))
    task = load_tasks()[0]
    assert task.context == {'user_id': 'u'} and task.user_info == {'name': 'User'}
    catalog = (output / 'catalog.json').read_text()
    assert 'SELECT' not in catalog and 'user_prompt' not in catalog
    assert json.loads(catalog)[0]['group'] == 'enterpriseops_gym:csm/a'
    with pytest.raises(ValueError, match='already exists'):
        import_rows([raw], output, revision='a' * 40, mode='oracle')


def test_unsafe_or_duplicate_identifiers_publish_nothing(tmp_path):
    raw = _task_json(domain='csm', task_id='task_a')
    for rows in ([raw, raw], [{**raw, 'task_id': '../a'}]):
        with pytest.raises(ValueError):
            import_rows(rows, tmp_path / 'bad', revision='a' * 40, mode='oracle')
        assert not (tmp_path / 'bad').exists()


def test_selected_tasks_do_not_parse_unrequested_unsupported_rows(tmp_path, monkeypatch):
    domain = tmp_path / 'csm'
    domain.mkdir()
    (domain / 'task_a.json').write_text(json.dumps(_task_json()))
    (domain / 'task_b.json').write_text(json.dumps(_task_json(verifiers=[])))
    monkeypatch.setenv('EOG_TASKS_DIR', str(tmp_path))
    assert [t.task_id for t in load_tasks(task_ids=('csm/a',))] == ['csm/a']
    with pytest.raises(ValueError, match='database_state'):
        parse_record(_task_json(verifiers=[]), 'csm', 'b')
