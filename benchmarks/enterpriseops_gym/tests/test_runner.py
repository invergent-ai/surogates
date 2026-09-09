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

    async def verify(*args):
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
