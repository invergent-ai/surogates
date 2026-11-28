import asyncio
from types import SimpleNamespace

import pytest

from eogbench import runner
from eogbench.dataset import parse_record
from test_dataset import _task_json


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['session', 'verify', 'cleanup', 'cancel', None])
async def test_rollout_tears_down_and_retains_infrastructure_errors(monkeypatch, failure):
    task = parse_record(_task_json(), 'csm', 'a')
    calls = []
    proxy = SimpleNamespace(configure_task=lambda *a, **kw: calls.append('scope'), set_database_id=lambda value: calls.append(('deactivate', value)))
    registrar = SimpleNamespace(register=lambda *a: calls.append('register') or 'server', remove=lambda *a: calls.append('remove'))
    monkeypatch.setattr(runner, 'seed_database', lambda task: 'db')

    async def session(*args):
        if failure == 'cancel':
            raise asyncio.CancelledError()
        if failure == 'session':
            raise RuntimeError('session unavailable')
        return 'session', [], 'completed', None

    async def verify(*args, **kw):
        calls.append('verify')
        if failure == 'verify':
            raise RuntimeError('verifier unavailable')
        return [{'passed': True}]

    def drop(*args):
        calls.append('drop')
        if failure == 'cleanup':
            raise RuntimeError('delete failed')

    monkeypatch.setattr(runner, 'run_session', session)
    monkeypatch.setattr(runner, 'verify', verify)
    monkeypatch.setattr(runner, 'drop_database', drop)
    if failure == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            await runner.run_task(None, registrar, 'http://localhost', proxy, task)
        assert 'verify' not in calls
    else:
        result = await runner.run_task(None, registrar, 'http://localhost', proxy, task)
        assert bool(result.verify_error) == (failure == 'verify')
        assert bool(result.error) == (failure in ('session', 'cleanup'))
    assert calls[:2] == ['scope', 'register']
    assert ('deactivate', None) in calls and 'remove' in calls
    assert calls[-1] == 'drop'


def test_owned_tools_first_gym_wins_like_upstream():
    from test_dataset import _hybrid_json
    task = parse_record(_hybrid_json(selected_tools=['find_user', 'create_event', 'list_users'],
                                     restricted_tools=['create_event']), 'hybrid', 'h')
    listed = {'sn-csm-server': ['find_user', 'list_users', 'other'],
              'gym-calendar': ['create_event', 'list_users', 'get_calendar_list']}
    owned = runner.owned_tools(task, listed)
    # list_users is in both gyms: the first gym in config order keeps it; create_event is restricted
    assert owned == {'sn-csm-server': frozenset({'find_user', 'list_users'}), 'gym-calendar': frozenset()}


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['session', 'verify', 'cleanup', 'seed2', None])
async def test_multi_gym_rollout_registers_each_gym_and_tears_down(monkeypatch, failure):
    from test_dataset import _hybrid_json
    task = parse_record(_hybrid_json(selected_tools=['update_entitlement', 'create_event']), 'hybrid', 'h')
    calls = []

    def proxy(name):
        return SimpleNamespace(configure_task=lambda db, tools, *a, **kw: calls.append(('scope', name, db, tuple(tools), kw['context'])),
                               set_database_id=lambda value: calls.append(('deactivate', name, value)))
    gym_proxies = {'sn-csm-server': ('http://p1', proxy('csm')), 'gym-calendar': ('http://p2', proxy('cal'))}
    registrar = SimpleNamespace(register=lambda key, url: calls.append(('register', key, url)) or 'server',
                                remove=lambda key: calls.append(('remove', key)))

    def seed(gym):
        if failure == 'seed2' and gym.name == 'gym-calendar':
            raise RuntimeError('seed failed')
        return 'db-' + gym.name

    async def tools(gym, db, endpoint):
        return {'sn-csm-server': ['update_entitlement'], 'gym-calendar': ['create_event']}[gym.name]

    async def session(*args):
        if failure == 'session':
            raise RuntimeError('session unavailable')
        return 'session', [], 'completed', None

    async def verify(task, dbs, scoring='upstream'):
        calls.append(('verify', dict(dbs)))
        if failure == 'verify':
            raise RuntimeError('verifier unavailable')
        return [{'passed': True}, {'passed': True}]

    def drop(gym, db):
        calls.append(('drop', gym.name, db))
        if failure == 'cleanup':
            raise RuntimeError('delete failed')

    monkeypatch.setattr(runner, 'seed_gym', seed)
    monkeypatch.setattr(runner, 'gym_tool_names', tools)
    monkeypatch.setattr(runner, 'run_session', session)
    monkeypatch.setattr(runner, 'verify_gyms', verify)
    monkeypatch.setattr(runner, 'drop_gym', drop)
    with pytest.raises(ValueError, match='one proxy per gym'):
        await runner.run_task(None, registrar, 'http://x', None, task)
    result = await runner.run_task(None, registrar, 'http://x', None, task, gym_proxies=gym_proxies)
    if failure == 'seed2':
        assert result.verify_error and result.passed is None
        assert [c for c in calls if c[0] in ('register', 'verify')] == []
        assert calls[-1] == ('drop', 'sn-csm-server', 'db-sn-csm-server')
        return
    keys = [c[1] for c in calls if c[0] == 'register']
    assert len(keys) == 2 and len(set(keys)) == 2 and [c[2] for c in calls if c[0] == 'register'] == ['http://p1/mcp', 'http://p2/mcp']
    assert ('scope', 'cal', 'db-gym-calendar', ('create_event',), {'x-access-token': 'tok'}) in calls
    assert ('scope', 'csm', 'db-sn-csm-server', ('update_entitlement',), {}) in calls
    assert sorted(c[1] for c in calls if c[0] == 'remove') == sorted(keys)
    assert ('verify', {'sn-csm-server': 'db-sn-csm-server', 'gym-calendar': 'db-gym-calendar'}) in calls
    assert [c[1] for c in calls if c[0] == 'drop'] == ['sn-csm-server', 'gym-calendar']
    assert result.database_ids == {'sn-csm-server': 'db-sn-csm-server', 'gym-calendar': 'db-gym-calendar'}
    assert bool(result.verify_error) == (failure == 'verify')
    assert bool(result.error) == (failure in ('session', 'cleanup'))
    assert result.passed is (None if failure == 'verify' else True)


def test_verdict_upstream_keeps_the_last_result_per_name():
    rows = [{'name': 'a', 'passed': False}, {'name': 'b', 'passed': True}, {'name': 'a', 'passed': True}]
    assert runner.verdict(rows) is True                      # upstream: dict by name, last 'a' wins
    assert runner.verdict(rows, 'all-verifiers') is False
    assert runner.verdict([{'name': 'a', 'passed': True}, {'name': 'a', 'passed': False}]) is False
    # unnamed verifiers are keyed by position (verifier_<i+1>), so they never collapse
    assert runner.verdict([{'name': None, 'passed': True}, {'name': None, 'passed': False}]) is False
    assert runner.verdict([]) is None
    with pytest.raises(ValueError):
        runner.verdict(rows, 'other')


def test_query_rejected_only_for_sql_errors():
    assert runner.query_rejected({'error': 'SQL query failed: SQL API call failed (HTTP 400): SQL execution error: no such table: incident'})
    assert runner.query_rejected({'error': 'SQL query failed: SQL API call failed (HTTP 400): (sqlite3.OperationalError) no such column: x'})
    assert not runner.query_rejected({'error': 'SQL query failed: SQL API call failed (HTTP 500): boom'})
    assert not runner.query_rejected({'error': 'SQL query failed: timed out'})
    assert not runner.query_rejected({'passed': False})


@pytest.mark.asyncio
@pytest.mark.parametrize('scoring, passed', [('upstream', True), ('all-verifiers', False)])
async def test_run_task_scores_with_the_chosen_rule(monkeypatch, scoring, passed):
    task = parse_record(_task_json(), 'csm', 'a')
    proxy = SimpleNamespace(configure_task=lambda *a, **kw: None, set_database_id=lambda value: None)
    registrar = SimpleNamespace(register=lambda *a: 'server', remove=lambda *a: None)
    monkeypatch.setattr(runner, 'seed_database', lambda task: 'db')
    monkeypatch.setattr(runner, 'drop_database', lambda *a: None)

    async def session(*args):
        return 'session', [], 'completed', None

    async def verify(task, db, scoring='upstream'):
        return [{'name': 'x', 'passed': False}, {'name': 'x', 'passed': True}]
    monkeypatch.setattr(runner, 'run_session', session)
    monkeypatch.setattr(runner, 'verify', verify)
    result = await runner.run_task(None, registrar, 'http://localhost', proxy, task, scoring=scoring)
    assert result.passed is passed and result.scoring == scoring
